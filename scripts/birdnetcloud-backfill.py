#!/usr/bin/env python3
"""Backfill tbb-test's detection history into BirdNET-Cloud.

Runs on the CENTRAL Pi, not the unit. Central mirrors every tbb-test row plus
the clips, and has the CPU and mains power to sit in an hour-long loop — driving
11k HTTP requests from a 415MB Pi Zero would starve the recorder it is supposed
to be feeding.

Why not their own backfill.py: it reads BirdNET-Pi's `Date`/`Time` columns.
birdbrain stores a single `started_at`, so both lookups miss, `fromisoformat`
throws, and it falls back to `datetime.utcnow()` — silently stamping the entire
history with the import time. The mapping here is deliberate instead.

Their API has no batch endpoint and no idempotency key, so:

  * one POST per detection, rate-limited (default 3/s) — this is a free service
    five days old, run by a small outfit; do not hammer it;
  * a resumable high-water mark is the ONLY thing preventing duplication, so it
    is written after every single row, not at the end;
  * rows already sent by other means are excluded explicitly.

Boundary: the live bridge on tbb-test owns local ids > its seed (18798) and has
already forwarded them. This backfill owns ids <= that. Central's `id` column is
central's own; the unit's id is the numeric suffix of `client_id`
("tbb-test:<local id>"), which is what both sides key on.

    scripts/birdnetcloud-backfill.py                  # dry run: counts + sample
    scripts/birdnetcloud-backfill.py --send --limit 20  # small live batch first
    scripts/birdnetcloud-backfill.py --send            # the rest, resumable
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

ENDPOINT = os.environ.get("BIRDNET_CLOUD_ENDPOINT", "https://api.birdnetcloud.com")
TOKEN_FILE = Path(
    os.environ.get("BIRDNET_TOKEN_FILE", "~/.config/birdnetcloud/token")
).expanduser()
DB = os.environ.get("BIRDBRAIN_DB", "data/birdbrain.sqlite")
STATE = Path(os.environ.get("BACKFILL_STATE", "data/birdnetcloud_backfill_state.json"))

# Local ids already in the cloud by another route: the two probe detections sent
# by hand from central on 2026-07-29 while verifying the API contract.
ALREADY_SENT = {18767, 18771}

# Upper bound of this backfill = the live bridge's seed point on the unit.
HISTORY_MAX_LOCAL_ID = 18798

LOCAL_ID_SQL = "CAST(substr(client_id, instr(client_id,':')+1) AS INTEGER)"

_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True
    print("\n[interrupt] finishing current row, then stopping (state is saved)…")


def read_token() -> str:
    tok = os.environ.get("BIRDNET_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_state() -> int:
    try:
        return int(json.loads(STATE.read_text(encoding="utf-8"))["last_local_id"])
    except (OSError, ValueError, KeyError):
        return 0


def save_state(local_id: int) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"last_local_id": local_id}), encoding="utf-8")


def rows_after(db_path: str, source: str, after_local_id: int, min_conf: float, limit: int | None):
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    sql = f"""SELECT {LOCAL_ID_SQL} AS local_id, started_at, scientific_name,
                     common_name, confidence, clip_path, latitude, longitude
                FROM detections
               WHERE source_name = ?
                 AND {LOCAL_ID_SQL} > ?
                 AND {LOCAL_ID_SQL} <= ?
                 AND confidence >= ?
               ORDER BY local_id ASC"""
    params = [source, after_local_id, HISTORY_MAX_LOCAL_ID, min_conf]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return c.execute(sql, params).fetchall()


def payload_for(row: sqlite3.Row) -> dict:
    """Must match birdnetcloud_sync.detection_payload on the tbb branch.

    The explicit +00:00 is the whole point: we store naive UTC, their server is
    timezone-aware, and a naive timestamp from a UTC+2 station lands two hours
    off with nothing anywhere to warn you.
    """
    ts = datetime.fromisoformat(str(row["started_at"]))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    p = {
        "common_name": row["common_name"],
        "scientific_name": row["scientific_name"],
        "confidence": float(row["confidence"]),
        "detected_at": ts.isoformat(),
        "kind": "bird",
    }
    if row["latitude"] is not None and row["longitude"] is not None:
        p["latitude"] = float(row["latitude"])
        p["longitude"] = float(row["longitude"])
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually POST (default: dry run)")
    ap.add_argument("--source", default="tbb-test")
    ap.add_argument("--min-confidence", type=float, default=0.7)
    ap.add_argument("--rate", type=float, default=3.0, help="requests/second (default 3)")
    ap.add_argument("--limit", type=int, default=None, help="stop after N rows")
    ap.add_argument("--no-clips", action="store_true")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--restart", action="store_true", help="ignore saved state (DANGEROUS: duplicates)")
    args = ap.parse_args()

    resume_from = 0 if args.restart else load_state()
    rows = rows_after(args.db, args.source, resume_from, args.min_confidence, args.limit)
    eligible = [r for r in rows if r["local_id"] not in ALREADY_SENT]
    excluded = len(rows) - len(eligible)
    with_clips = sum(1 for r in eligible if r["clip_path"] and Path(r["clip_path"]).exists())

    print(f"endpoint      : {ENDPOINT}")
    print(f"source        : {args.source}")
    print(f"history bound : local_id <= {HISTORY_MAX_LOCAL_ID} (live bridge owns above)")
    print(f"resume from   : local_id > {resume_from}")
    print(f"min confidence: {args.min_confidence}")
    print(f"to send       : {len(eligible)}  (excluded as already-sent: {excluded})")
    print(f"clips on disk : {with_clips}" + ("  (--no-clips set)" if args.no_clips else ""))
    if eligible:
        est = len(eligible) / max(args.rate, 0.1) / 60
        print(f"rate          : {args.rate}/s  ->  ~{est:.0f} min for detections alone")
        f, l = eligible[0], eligible[-1]
        print(f"first         : local {f['local_id']} {f['started_at']} {f['common_name']}")
        print(f"last          : local {l['local_id']} {l['started_at']} {l['common_name']}")
        print("sample payload:")
        print("\n".join("  " + x for x in json.dumps(payload_for(f), indent=2).splitlines()))

    if not args.send:
        print("\nDRY RUN — nothing sent. Add --send (consider --limit 20 first).")
        return 0
    if not eligible:
        print("\nNothing to do.")
        return 0

    token = read_token()
    if not token:
        print(f"No token (checked $BIRDNET_TOKEN and {TOKEN_FILE}).", file=sys.stderr)
        return 2

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    auth = {"Authorization": f"Bearer {token}"}
    # One Session for the whole run. Without keep-alive every POST pays a fresh
    # TLS handshake — measured at 1.4s of a 1.6s request, which turned a
    # nominal 3/s into an actual 0.5/s and a one-hour job into six.
    session = requests.Session()
    session.headers.update(auth)
    interval = 1.0 / max(args.rate, 0.1)
    sent = clips = failed = 0
    consecutive_fail = 0
    started = time.time()

    print(f"\nsending {len(eligible)} detections at ~{args.rate}/s …")
    for i, row in enumerate(eligible, 1):
        if _stop:
            break
        t0 = time.time()
        try:
            r = session.post(
                f"{ENDPOINT}/api/v1/detections", json=payload_for(row), timeout=20
            )
        except requests.RequestException as e:
            failed += 1
            consecutive_fail += 1
            print(f"  [{i}] local {row['local_id']} network error: {str(e)[:80]}")
            r = None

        if r is not None:
            if r.status_code in (401, 403):
                print(f"\nAUTH REJECTED (http {r.status_code}) — stopping. Fix the token and re-run;")
                print("state is saved, so it resumes without duplicating.")
                return 1
            if r.status_code == 201:
                consecutive_fail = 0
                sent += 1
                det_id = (r.json() or {}).get("id")
                if det_id and not args.no_clips and row["clip_path"]:
                    p = Path(row["clip_path"])
                    if p.exists():
                        ctype = "audio/ogg" if p.suffix.lower() == ".ogg" else "audio/wav"
                        try:
                            with p.open("rb") as fh:
                                m = session.post(
                                    f"{ENDPOINT}/api/v1/detections/{det_id}/media/audio",
                                    files={"file": (p.name, fh, ctype)},
                                    timeout=60,
                                )
                            if m.status_code == 200:
                                clips += 1
                        except (requests.RequestException, OSError):
                            pass  # non-fatal: the detection is already in
                # Advance only on a confirmed create. Written every row because
                # this mark is the only defence against re-sending.
                save_state(row["local_id"])
            else:
                failed += 1
                consecutive_fail += 1
                print(f"  [{i}] local {row['local_id']} rejected http={r.status_code} {r.text[:100]}")

        if consecutive_fail >= 10:
            print("\n10 consecutive failures — stopping rather than grinding on.")
            print("State is saved; investigate, then re-run to resume.")
            break

        if i % 100 == 0 or i == len(eligible):
            el = time.time() - started
            rate = sent / el if el else 0
            print(f"  {i}/{len(eligible)}  sent={sent} clips={clips} failed={failed}"
                  f"  {rate:.1f}/s  elapsed={el/60:.1f}m")

        sleep = interval - (time.time() - t0)
        if sleep > 0:
            time.sleep(sleep)

    el = (time.time() - started) / 60
    print(f"\ndone: sent={sent} clips={clips} failed={failed} in {el:.1f} min")
    print(f"resume mark: local_id {load_state()}")
    if _stop:
        print("stopped early by signal — re-run to continue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
