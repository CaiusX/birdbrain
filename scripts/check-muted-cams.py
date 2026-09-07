#!/usr/bin/env python3
"""Re-enable a muted cam if its audio ever comes back.

Some live cams carry a real audio track that is pure digital silence — a
sanctuary that mutes for staff privacy, a feed re-encoded without sound. The
pipeline cannot tell that from a dead mic until it measures it, and once it
has, the cam is worth nothing: it holds a worker slot, scores 0 on audio
quality, and sits on the health pane as a problem that never clears. So we
disable it.

But "muted" is a decision someone made at the far end, and they can undo it.
This checks the muted list on a timer, samples each cam's audio directly, and
puts one back on the roster the moment it carries sound again. Nothing to
watch and nothing to remember: the cam simply reappears.

Only names in the ``muted_cams`` setting are ever touched. That matters —
cams are also disabled for entirely different reasons (a YouTube IP block, see
``youtube-resume.py``), and re-enabling one of those on an audio check would
fight the tool that paused it.

Usage:
    uv run python scripts/check-muted-cams.py            # check and re-enable
    uv run python scripts/check-muted-cams.py --dry-run  # measure only
    uv run python scripts/check-muted-cams.py --add "Cam name"     # mute + disable
    uv run python scripts/check-muted-cams.py --list
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time

from birdbrain.audio.youtube import YouTubeSource
from birdbrain.config import AppConfig, load_sources
from birdbrain.logging import configure as configure_logging
from birdbrain.logging import get_logger
from birdbrain.storage import Database

log = get_logger("check_muted_cams")

#: app_settings key holding the JSON list of muted cam names.
MUTED_KEY = "muted_cams"

#: Mean level above which a feed is carrying real sound. ffmpeg reports about
#: -91 dB for 16-bit digital silence and "-inf" for a truly empty buffer; the
#: quietest cam in the roster that still yields detections sits near -66 dB.
#: -85 sits in the gap, well clear of both.
AUDIO_FLOOR_DBFS = -85.0

SAMPLE_SECONDS = 20

#: Resolves fail intermittently on healthy cams ("No video formats found!"),
#: often enough that a single attempt would leave most weekly runs with no
#: measurement at all. A few spaced tries turn that into a rare miss without
#: becoming the request burst that trips YouTube's bot gate.
RESOLVE_ATTEMPTS = 3
RESOLVE_GAP_S = 45.0


def muted_list(db: Database) -> list[str]:
    raw = db.get_setting(MUTED_KEY)
    if not raw:
        return []
    try:
        names = json.loads(raw)
    except ValueError:
        log.warning("muted_cams.unparseable", value=raw[:200])
        return []
    return [str(n) for n in names] if isinstance(names, list) else []


def save_muted(db: Database, names: list[str]) -> None:
    db.set_setting(MUTED_KEY, json.dumps(sorted(set(names)), indent=1))


def source_url(cfg: AppConfig, db: Database, name: str) -> tuple[str, str | None] | None:
    """(url, cookies_file) for ``name`` from the roster, or None if unknown."""
    try:
        static = {s.name: s for s in load_sources(cfg.sources_file)}
    except FileNotFoundError:
        static = {}
    if name in static:
        s = static[name]
        return s.url, (str(s.cookies_file) if s.cookies_file else None)
    row = next((r for r in db.list_runtime_sources() if r.name == name), None)
    return (row.url, row.cookies_file) if row is not None else None


def resolve(name: str, url: str, cookies: str | None,
            attempts: int = RESOLVE_ATTEMPTS, gap_s: float = RESOLVE_GAP_S) -> str | None:
    """The cam's current stream URL, retrying a transient failure. None when
    every attempt failed — which says nothing about the audio, so the caller
    must leave the cam exactly as it found it."""
    for i in range(attempts):
        try:
            return YouTubeSource(name=name, url=url, cookies_file=cookies).current_url()
        except Exception as e:
            last = str(e)[:120]
            if i + 1 < attempts:
                time.sleep(gap_s)
    log.info("muted_cams.resolve_failed", source=name, attempts=attempts, error=last)
    return None


def mean_dbfs(url: str, seconds: int = SAMPLE_SECONDS) -> float | None:
    """Mean level of ``seconds`` of the stream, or None if it could not be
    sampled. ``-inf`` from ffmpeg (an all-zero buffer) reads as the floor."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-t", str(seconds), "-i", url,
             "-vn", "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=seconds + 90, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("muted_cams.sample_failed", error=str(e)[:200])
        return None
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB", r.stderr)
    if not m:
        return None
    return float("-inf") if m.group(1) == "-inf" else float(m.group(1))


def check(db: Database, cfg: AppConfig, *, dry_run: bool) -> int:
    names = muted_list(db)
    if not names:
        print("no muted cams recorded; nothing to check.")
        return 0
    restored: list[str] = []
    for name in names:
        found = source_url(cfg, db, name)
        if found is None:
            print(f"{name}: not in the roster any more — dropping from the muted list")
            restored.append(name)  # drop it; there is nothing left to re-enable
            continue
        url, cookies = found
        stream = resolve(name, url, cookies)
        if stream is None:
            # A resolve failure says nothing about the audio. Leave the cam
            # muted and try again next run rather than guessing.
            print(f"{name}: could not resolve after {RESOLVE_ATTEMPTS} tries — leaving muted")
            continue
        level = mean_dbfs(stream)
        if level is None:
            print(f"{name}: could not measure — leaving muted")
            continue
        if level > AUDIO_FLOOR_DBFS:
            print(f"{name}: AUDIO IS BACK ({level:.1f} dBFS) — re-enabling")
            log.warning("muted_cams.audio_restored", source=name, mean_dbfs=level)
            if not dry_run:
                db.set_source_disabled(name, False)
            restored.append(name)
        else:
            print(f"{name}: still silent ({level:.1f} dBFS)")
    if restored and not dry_run:
        save_muted(db, [n for n in names if n not in restored])
        print(f"re-enabled {len(restored)}; restart the pipeline to pick them up.")
    return 0


def add(db: Database, names: list[str], *, dry_run: bool) -> int:
    current = muted_list(db)
    for name in names:
        if not dry_run:
            db.set_source_disabled(name, True)
        print(f"{name}: disabled and added to the muted list")
    if not dry_run:
        save_muted(db, current + names)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", action="append", default=[], metavar="NAME",
                    help="Disable this cam and record it as muted (repeatable).")
    ap.add_argument("--list", action="store_true", help="Show the muted list and exit.")
    ap.add_argument("--dry-run", action="store_true", help="Measure and report; change nothing.")
    args = ap.parse_args()

    configure_logging("INFO")
    cfg = AppConfig()
    db = Database(cfg.db_url)

    if args.list:
        for name in muted_list(db):
            print(name)
        return 0
    if args.add:
        return add(db, args.add, dry_run=args.dry_run)
    return check(db, cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
