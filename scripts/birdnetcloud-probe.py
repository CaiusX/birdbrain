#!/usr/bin/env python3
"""Phase-1 probe for the BirdNET-Cloud ingest API.

Answers the questions we cannot answer by reading their code, using the
smallest number of real writes to someone else's service:

  1. Does a real birdbrain detection satisfy POST /api/v1/detections?
  2. How is `detected_at` interpreted? We store naive UTC; their edge agent
     sends a bare `datetime.isoformat()`. If the server assumes local time,
     every backfilled row lands hours off and the error is invisible until
     you compare against the dashboard.
  3. Does the media endpoint accept our .ogg clips, or must we transcode to
     the .wav/audio/wav their uploader hardcodes?

Contract (from their edge agent, uploader.py / analyzer.py, v1.4.60):
    POST {ENDPOINT}/api/v1/detections            Bearer <device token> -> 201 {"id"}
    POST {ENDPOINT}/api/v1/detections/{id}/media/{spectrogram|audio}   multipart
    POST {ENDPOINT}/api/v1/devices/heartbeat

Dry-run by default: prints exactly what it would send and exits. Nothing is
written to the cloud without --send.

    export BIRDNET_TOKEN=...        # from dashboard: My stations -> Add a station
    scripts/birdnetcloud-probe.py                 # show the payload, send nothing
    scripts/birdnetcloud-probe.py --send          # post ONE detection
    scripts/birdnetcloud-probe.py --send --media  # ...and try the .ogg clip
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

import requests

ENDPOINT = os.environ.get("BIRDNET_CLOUD_ENDPOINT", "https://api.birdnetcloud.com")
DB = os.environ.get("AFRICAM_DB", "data/africam.sqlite")
SOURCE = os.environ.get("PROBE_SOURCE", "tbb-test")
TOKEN_FILE = os.path.expanduser(
    os.environ.get("BIRDNET_TOKEN_FILE", "~/.config/birdnetcloud/token")
)


def read_token() -> str:
    """Env first, then a 0600 file — so the token need never be typed into a
    shell command (and thus into scrollback, history, or a transcript)."""
    tok = os.environ.get("BIRDNET_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(TOKEN_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def pick_detection(db_path: str, source: str) -> dict:
    """Most recent high-confidence detection with a clip on disk."""
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    for row in c.execute(
        """SELECT id, started_at, scientific_name, common_name, confidence,
                  clip_path, latitude, longitude
             FROM detections
            WHERE source_name = ? AND confidence >= 0.7
              AND clip_path IS NOT NULL AND clip_path <> ''
            ORDER BY started_at DESC LIMIT 25""",
        (source,),
    ):
        if row["clip_path"] and os.path.exists(row["clip_path"]):
            return dict(row)
    raise SystemExit(f"no detection with an on-disk clip found for source={source!r}")


def build_payload(det: dict, naive: bool) -> dict:
    """Map a birdbrain row onto their detection shape.

    started_at is naive UTC. We send an explicit +00:00 offset so a
    timezone-aware server has no room to guess; --naive reproduces what their
    own agent sends, for comparison.
    """
    ts = datetime.fromisoformat(str(det["started_at"]))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    detected_at = ts.replace(tzinfo=None).isoformat() if naive else ts.isoformat()

    payload = {
        "common_name": det["common_name"],
        "scientific_name": det["scientific_name"],
        "confidence": float(det["confidence"]),
        "detected_at": detected_at,
        "kind": "bird",
    }
    if det["latitude"] is not None and det["longitude"] is not None:
        payload["latitude"] = float(det["latitude"])
        payload["longitude"] = float(det["longitude"])
    return payload


def describe_time(det: dict) -> None:
    ts = datetime.fromisoformat(str(det["started_at"])).replace(tzinfo=UTC)
    age = datetime.now(UTC) - ts
    hrs = age / timedelta(hours=1)
    print("  TIMEZONE CHECK — after sending, open the dashboard and confirm:")
    print(f"    true instant : {ts.isoformat()}  (UTC)")
    print(f"    local (SAST) : {(ts + timedelta(hours=2)).replace(tzinfo=None).isoformat()}  (UTC+2)")
    print(f"    should read as roughly {hrs:.1f} hours ago")
    print("    if it reads ~2h off, the server is treating our UTC as local time.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually POST (default: dry run)")
    ap.add_argument("--media", action="store_true", help="also try uploading the .ogg clip")
    ap.add_argument("--heartbeat", action="store_true", help="also send one heartbeat")
    ap.add_argument("--naive", action="store_true", help="send a naive timestamp like their agent")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--source", default=SOURCE)
    args = ap.parse_args()

    token = read_token()
    det = pick_detection(args.db, args.source)
    payload = build_payload(det, args.naive)

    print(f"endpoint : {ENDPOINT}")
    print(f"source   : {args.source}   (birdbrain detection id {det['id']})")
    print(f"clip     : {det['clip_path']}")
    print("payload  :")
    print("\n".join("  " + ln for ln in json.dumps(payload, indent=2).splitlines()))
    describe_time(det)

    if not args.send:
        print("\nDRY RUN — nothing sent. Re-run with --send once BIRDNET_TOKEN is set.")
        return 0
    if not token:
        print(
            f"\nNo token (checked $BIRDNET_TOKEN and {TOKEN_FILE}); refusing to send.",
            file=sys.stderr,
        )
        return 2

    auth = {"Authorization": f"Bearer {token}"}
    print(f"\nPOST {ENDPOINT}/api/v1/detections")
    r = requests.post(f"{ENDPOINT}/api/v1/detections", json=payload, headers=auth, timeout=20)
    print(f"  http={r.status_code}  body={r.text[:400]}")
    if r.status_code != 201:
        print("  -> not a 201; contract differs from the agent's expectation. Stopping.")
        return 1

    det_id = r.json().get("id")
    print(f"  -> created detection id={det_id}")

    if args.media:
        path = det["clip_path"]
        # Their uploader hardcodes audio/wav; we hold .ogg. If this is rejected
        # the backfill needs a transcode step, so find out now on one file.
        ctype = "audio/ogg" if path.endswith(".ogg") else "audio/wav"
        print(f"\nPOST /api/v1/detections/{det_id}/media/audio  ({ctype})")
        with open(path, "rb") as fh:
            m = requests.post(
                f"{ENDPOINT}/api/v1/detections/{det_id}/media/audio",
                files={"file": (os.path.basename(path), fh, ctype)},
                headers=auth,
                timeout=60,
            )
        print(f"  http={m.status_code}  body={m.text[:300]}")
        print("  -> ogg accepted" if m.status_code == 200
              else "  -> ogg rejected; backfill will need to transcode to wav")

    if args.heartbeat:
        hb = {"version": "birdbrain-bridge-probe", "queue_depth": 0}
        print(f"\nPOST {ENDPOINT}/api/v1/devices/heartbeat")
        h = requests.post(f"{ENDPOINT}/api/v1/devices/heartbeat", json=hb, headers=auth, timeout=20)
        print(f"  http={h.status_code}  body={h.text[:300]}")

    print("\nNow check the dashboard against the TIMEZONE CHECK above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
