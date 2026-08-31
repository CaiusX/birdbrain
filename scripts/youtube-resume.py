#!/usr/bin/env python3
"""Bring YouTube sources back one at a time after an IP bot-block.

YouTube blocks the whole IP, not an account or a cookie. When it does, every
source fails with "Sign in to confirm you're not a bot" — and because each
failure is retried, a paused-then-restored fleet can walk straight back into
the block it just served. Two rules keep that from happening:

  * Probe before touching anything, unauthenticated, against one well-known
    video. If that cannot resolve, nothing else will, and every extra request
    deepens the block.
  * Re-enable one source at a time, verify it actually resolves, and stop the
    moment one fails. Seventeen simultaneous resolves is the burst that trips
    the block in the first place (see the comment in audio/youtube.py).

Usage:
    uv run python scripts/youtube-resume.py --check       # probe only
    uv run python scripts/youtube-resume.py --resume      # staged restore
    uv run python scripts/youtube-resume.py --resume --gap 300

Sources are read back from the pause record written when they were disabled, so
this only ever re-enables what was paused — never something switched off on
purpose.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from birdbrain.audio.youtube import YouTubeSource
from birdbrain.config import AppConfig, load_sources
from birdbrain.storage import Database

# "Me at the zoo" — the oldest video on YouTube. Resolves for anyone, anywhere,
# with no authentication, so a failure here is about the IP and nothing else.
PROBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _yt_dlp() -> str:
    local = Path(sys.executable).parent / "yt-dlp"
    return str(local) if local.exists() else "yt-dlp"


def probe() -> tuple[bool, str]:
    """Can this machine resolve YouTube at all? (clear, detail)"""
    try:
        r = subprocess.run(
            [_yt_dlp(), "-f", "bestaudio/best", "-g", "--no-warnings", PROBE_URL],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)[:200]
    if r.returncode == 0 and r.stdout.startswith("http"):
        return True, "resolved"
    tail = (r.stderr or r.stdout or "").strip().splitlines()
    return False, (tail[-1] if tail else "no output")[:200]


def resolves(db: Database, cfg: AppConfig, name: str) -> bool:
    """Does this specific source resolve right now?"""
    try:
        static = {s.name: s for s in load_sources(cfg.sources_file)}
    except FileNotFoundError:
        static = {}
    src = static.get(name)
    if src is None:
        row = next((r for r in db.list_runtime_sources() if r.name == name), None)
        if row is None:
            return False
        url, cookies = row.url, row.cookies_file
    else:
        url = src.url
        cookies = str(src.cookies_file) if src.cookies_file else None
    try:
        YouTubeSource(name=name, url=url, cookies_file=cookies).current_url()
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="probe only, change nothing")
    ap.add_argument("--resume", action="store_true", help="re-enable, one at a time")
    ap.add_argument("--gap", type=float, default=180.0,
                    help="seconds between sources (default 180)")
    ap.add_argument("--record", default="paused_youtube.json",
                    help="pause record listing which sources to restore")
    args = ap.parse_args()

    clear, detail = probe()
    print(f"probe: {'CLEAR' if clear else 'BLOCKED'} — {detail}")
    if args.check or not args.resume:
        return 0 if clear else 1
    if not clear:
        print("refusing to resume: every request while blocked makes it worse")
        return 1

    rec = Path(args.record)
    if not rec.exists():
        print(f"no pause record at {rec}; refusing to guess what to re-enable")
        return 1
    names = json.loads(rec.read_text())

    cfg = AppConfig()
    db = Database(cfg.db_url)
    restored: list[str] = []
    for i, name in enumerate(names):
        db.set_source_disabled(name, False)
        print(f"[{i+1}/{len(names)}] enabled {name!r} — verifying…", flush=True)
        # Give the supervisor a tick to pick it up, then confirm it can resolve.
        time.sleep(20)
        if resolves(db, cfg, name):
            restored.append(name)
            print(f"    ok ({len(restored)} live)")
        else:
            db.set_source_disabled(name, True)
            print("    FAILED to resolve — re-paused, stopping here.")
            print(f"    restored {len(restored)}; {len(names)-len(restored)} still paused")
            rec.write_text(json.dumps([n for n in names if n not in restored], indent=1))
            return 1
        if i + 1 < len(names):
            time.sleep(args.gap)

    rec.write_text(json.dumps([], indent=1))
    print(f"all {len(restored)} sources restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
