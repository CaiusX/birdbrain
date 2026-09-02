#!/usr/bin/env python3
"""Bulk-fetch a TBB unit's clips that central never materialised.

Central pulls a unit's audio lazily — only when someone auditions that
detection — so for a synced unit almost every row has a NULL clip_path even
though the unit recorded the audio fine. That is the right default for a
dashboard and the wrong one for any analysis that needs the waveform.

    scripts/tbb-fetch-clips.py --unit tbb-test --species 'Hadada Ibis'
    scripts/tbb-fetch-clips.py --unit tbb-test --species 'Cape Robin-Chat' \
        --paired-with 'JHB - Hyde Park' --limit 4000

TWO THINGS WILL COST YOU CLIPS IF YOU IGNORE THEM.

Retention. A unit prunes its own clips (tbb_clip_retention_days, default 14),
so anything older than that window is already gone and asking for it just
burns requests against a Pi Zero. --days defaults to 13 to stay inside it.

Reachability. The unit's address comes from the app_setting
``live_audio_url:<unit>``. That pointed at an mDNS name (tbb-bench.local) which
stopped resolving on 2026-09-02 while the unit itself was perfectly healthy —
serving on its IP, SSH up, detections flowing. Nothing surfaced the breakage:
clip fetching and /admin audition both just failed. If this script cannot reach
the unit, check that setting resolves before assuming the unit is down.

Pacing is deliberate. The unit is a Pi Zero 2 W running BirdNET at the same
time; --pause 0.35 keeps the fetch well clear of disturbing detection, and this
is not a job worth rushing.

Safe to re-run: existing files are skipped and only NULL clip_paths are filled.
"""
from __future__ import annotations

import argparse
import bisect
import pathlib
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import requests


def unit_base(db: sqlite3.Connection, unit: str) -> str | None:
    row = db.execute(
        "select value from app_settings where key=?", (f"live_audio_url:{unit}",)
    ).fetchone()
    if not row or not row[0]:
        return None
    m = re.match(r"(https?://[^/]+)", row[0])
    return m.group(1) if m else None


def select_targets(db: sqlite3.Connection, args) -> list[tuple]:
    """Detections needing a clip: inside retention, and optionally only those
    that co-occur at a second source (the cross-mic pairs)."""
    cut = datetime.now(tz=None) - timedelta(days=args.days)
    rows = db.execute(
        "select started_at, id, client_id from detections "
        "where source_name=? and common_name=? and clip_path is null order by 1",
        (args.unit, args.species)).fetchall()
    want = [(datetime.fromisoformat(t), i, c) for t, i, c in rows if c]
    want = [w for w in want if w[0] >= cut]
    if not args.paired_with:
        return want
    other = [datetime.fromisoformat(t) for (t,) in db.execute(
        "select started_at from detections where source_name=? and common_name=? "
        "and clip_path is not null order by 1", (args.paired_with, args.species))]
    keep = []
    for t, det_id, cid in want:
        i = bisect.bisect_left(other, t)
        if any(0 <= j < len(other)
               and abs((other[j] - t).total_seconds()) <= args.window
               for j in (i - 1, i)):
            keep.append((t, det_id, cid))
    return keep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unit", default="tbb-test", help="synced unit's source_name")
    ap.add_argument("--species", required=True, help="common_name to fetch")
    ap.add_argument(
        "--paired-with",
        help="only fetch detections that co-occur at this other source "
        "(cross-mic pairs); omit to fetch all of the species",
    )
    ap.add_argument("--window", type=float, default=6.0,
                    help="co-occurrence window in seconds (default 6)")
    ap.add_argument("--days", type=int, default=13,
                    help="skip anything older than this; the unit has pruned it")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--pause", type=float, default=0.35,
                    help="seconds between requests — be kind to the Zero")
    ap.add_argument("--db", default="data/birdbrain.sqlite")
    args = ap.parse_args()

    db = sqlite3.connect(args.db, timeout=60)
    db.execute("pragma busy_timeout=60000")
    base = unit_base(db, args.unit)
    if not base:
        print(f"no live_audio_url set for {args.unit!r} — cannot locate the unit")
        return 1

    want = select_targets(db, args)[: args.limit]
    print(f"{args.species}: {len(want)} clips to fetch from {base}")

    dest_dir = pathlib.Path("data/clips/_units") / re.sub(r"[^A-Za-z0-9_.-]", "_", args.unit)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ok = pruned = failed = 0
    session = requests.Session()
    for n, (_t, det_id, cid) in enumerate(want, 1):
        local = cid.rsplit(":", 1)[-1]
        dest = dest_dir / f"{re.sub(r'[^0-9A-Za-z_.-]', '_', local)}.ogg"
        if not dest.exists():
            try:
                r = session.get(f"{base}/clips/{local}", timeout=30)
            except requests.RequestException:
                failed += 1
                continue
            if r.status_code == 404:
                pruned += 1          # gone from the unit; nothing to be done
                continue
            if r.status_code != 200 or len(r.content) < 500:
                failed += 1
                continue
            dest.write_bytes(r.content)
        db.execute("update detections set clip_path=? where id=? and clip_path is null",
                   (str(dest), det_id))
        ok += 1
        if n % 100 == 0:
            db.commit()
            print(f"  {n}/{len(want)} fetched={ok} pruned={pruned} failed={failed}", flush=True)
        time.sleep(args.pause)
    db.commit()
    print(f"done: fetched {ok}, already-pruned {pruned}, failed {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
