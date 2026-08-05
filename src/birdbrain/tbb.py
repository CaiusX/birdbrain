"""TinyBirdBrain (TBB) capture-unit runtime.

A TBB is a single USB mic feeding BirdNET, plus a clip-retention sweep to
protect the SD card. It runs its own capture loop (:mod:`birdbrain.tbb_capture`)
rather than central's ``run_source``: that one is shaped by central's needs and
importing it pulled in AI commentary, weather, OCR, highlight watching, site
resolution and sandbox mode — seven modules a microphone in a field cannot use.
The schema, detector and clip writer are still shared verbatim.

This module is the `tbb-pipeline` entrypoint. Sync to central (Phase 2) and the
minimal local web UI (Phase 1b) live elsewhere; this is just the detector loop.
"""
from __future__ import annotations

import contextlib
import signal
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from birdbrain.audio.mic import MicSource
from birdbrain.config import AppConfig, SourceConfig
from birdbrain.detector import BirdNetDetector
from birdbrain.logging import get_logger
from birdbrain.storage import Database, DetectionRow
from birdbrain.tbb_capture import run_capture

log = get_logger(__name__)


def tbb_source_config(app: AppConfig) -> SourceConfig:
    """Build the single mic ``SourceConfig`` a unit runs from its tbb_* settings."""
    return SourceConfig(
        name=app.tbb_unit_id,
        kind="mic",
        # MicSource reads ALSA, not a URL — this is a descriptive placeholder so
        # the required `url` field is populated; `device` is what actually matters.
        url=f"alsa:{app.tbb_mic_device}",
        device=app.tbb_mic_device,
        lat=app.tbb_lat,
        lon=app.tbb_lon,
        min_confidence=app.tbb_min_confidence,
        timezone=app.tbb_timezone,
        multisite=False,
        # A consumer unit shows birds, not Engine/Dog/Siren noise classes.
        exclude_non_bird=True,
    )


def prune_clips(db: Database, retention_days: int) -> int:
    """Delete saved clip files older than ``retention_days`` and NULL their
    ``clip_path`` (DB rows are kept). Also removes cached spectrogram PNGs.
    Returns the number of clip files deleted. Mirrors the central `prune` CLI
    command but unconditional — a unit has no audition/labelling to preserve."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    with db.session() as s:
        rows = list(
            s.execute(
                select(DetectionRow.id, DetectionRow.clip_path)
                .where(DetectionRow.started_at < cutoff)
                .where(DetectionRow.clip_path.is_not(None))
            )
        )
    if not rows:
        return 0

    by_clip: dict[str, list[int]] = {}
    for row in rows:
        by_clip.setdefault(row.clip_path, []).append(row.id)

    deleted = 0
    with db.session() as s, s.begin():
        for clip, ids in by_clip.items():
            p = Path(clip)
            if p.is_file():
                p.unlink()
                deleted += 1
                # Glob rather than name the variants: the old code looked for
                # "<stem>.png" and "<stem>.large.png" while the web app wrote
                # "<stem>.fire.png", so every cached spectrogram outlived the
                # clip it described and the cache grew without bound. Units no
                # longer render any, but a glob also cleans up whatever a future
                # (or older) build leaves behind.
                for png in p.parent.glob(f"{p.stem}*.png"):
                    png.unlink(missing_ok=True)
            for det_id in ids:
                det = s.get(DetectionRow, det_id)
                if det is not None:
                    det.clip_path = None
    return deleted


def sweep_orphan_spectrograms(clips_dir: Path) -> tuple[int, int]:
    """Delete every cached spectrogram PNG under ``clips_dir``. Returns
    (files, bytes).

    A unit renders no spectrograms — central regenerates them from the synced
    clip — so any PNG here is left over from a build that did. They cannot be
    reached by :func:`prune_clips`, which only touches files belonging to a row
    it is expiring: once a clip was pruned its ``clip_path`` went NULL, so its
    orphaned PNG was unreferenced and immortal. Runs once at startup rather than
    every tick, since after the first pass there is nothing left to find.
    """
    files = size = 0
    for png in clips_dir.rglob("*.png"):
        try:
            size += png.stat().st_size
            png.unlink()
            files += 1
        except OSError:
            continue
    return files, size


def _prune_loop(db: Database, app: AppConfig, stop_event: threading.Event) -> None:
    """Background retention sweep: prune once on start, then every tick until stop."""
    try:
        files, freed = sweep_orphan_spectrograms(app.clips_dir)
        if files:
            log.info("tbb.spectrograms_swept", files=files, bytes_freed=freed)
    except Exception:
        log.exception("tbb.spectrogram_sweep_failed")
    while True:
        try:
            n = prune_clips(db, app.tbb_clip_retention_days)
            if n:
                log.info("tbb.pruned", files=n, retention_days=app.tbb_clip_retention_days)
        except Exception:
            log.exception("tbb.prune_failed")
        if stop_event.wait(app.tbb_prune_tick_seconds):
            return


def run_tbb(app: AppConfig) -> None:
    """Run the unit's mic → detector → SQLite + clips loop until signalled.

    Uses the unit's own capture loop (``tbb_capture``) rather than central's
    ``run_source``. Importing the latter dragged in seven central-only modules —
    AI commentary, weather, OCR, highlight watching, site resolution, sandbox —
    for a box that can use none of them. Stops cleanly on SIGINT/SIGTERM so
    systemd can supervise it."""
    cfg = tbb_source_config(app)
    detector = BirdNetDetector()
    db = Database(app.db_url)
    stop_event = threading.Event()

    def _handle_stop(signum, _frame) -> None:
        log.info("tbb.signal", signal=signum)
        stop_event.set()

    # SIGTERM is what systemd sends on `stop`; SIGINT is Ctrl-C at the console.
    # signal.signal raises off the main thread (e.g. under pytest) — fine to
    # skip there; the caller owns shutdown in that case.
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handle_stop)

    threading.Thread(
        target=_prune_loop, args=(db, app, stop_event), name="tbb-prune", daemon=True
    ).start()

    log.info(
        "tbb.pipeline_start",
        unit=app.tbb_unit_id,
        device=app.tbb_mic_device,
        retention_days=app.tbb_clip_retention_days,
    )
    try:
        # run_capture loops with retry/backoff and exits when stop_event is set.
        mic = MicSource(
            name=cfg.name,
            device=cfg.device,
            sample_rate=app.sample_rate,
            chunk_seconds=app.chunk_seconds,
        )
        run_capture(cfg, app, detector, db, mic, stop_event)
    finally:
        stop_event.set()
        log.info("tbb.pipeline_stopped", unit=app.tbb_unit_id)
