"""TinyBirdBrain (TBB) capture-unit runtime.

A TBB runs the *same* `birdbrain` pipeline as central, but as a single USB-mic
source with the central-only workers (notes, weather, media sweeper, anomalies,
OCR site-resolver) left off, plus a local clip-retention sweep to protect the SD
card. Everything heavy is reused verbatim: ``MicSource`` feeds the existing
``run_source`` worker (heartbeat, backoff, pre-roll clip writing) which writes to
the same SQLite schema.

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

from birdbrain.config import AppConfig, SourceConfig
from birdbrain.detector import BirdNetDetector
from birdbrain.logging import get_logger
from birdbrain.pipeline import run_source
from birdbrain.storage import Database, DetectionRow

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
                for png in (p.with_suffix(".png"), p.parent / f"{p.stem}.large.png"):
                    if png.is_file():
                        png.unlink()
            for det_id in ids:
                det = s.get(DetectionRow, det_id)
                if det is not None:
                    det.clip_path = None
    return deleted


def _prune_loop(db: Database, app: AppConfig, stop_event: threading.Event) -> None:
    """Background retention sweep: prune once on start, then every tick until stop."""
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

    Reuses the central ``run_source`` worker (so we get heartbeats, restart
    backoff, and pre-roll clips for free) for a single non-multisite mic source.
    None of the central-only background workers are started. Stops cleanly on
    SIGINT/SIGTERM so systemd can supervise it."""
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
        # Empty sites dict + non-multisite cfg → SiteResolver returns the static
        # lat/lon with no DB/OCR work. run_source loops with retry/backoff and
        # exits when stop_event is set.
        run_source(cfg, app, detector, db, sites={}, stop_event=stop_event)
    finally:
        stop_event.set()
        log.info("tbb.pipeline_stopped", unit=app.tbb_unit_id)
