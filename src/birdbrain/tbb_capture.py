"""The capture loop a TinyBirdBrain unit runs: mic → BirdNET → SQLite + clips.

A deliberate, stripped fork of ``pipeline.run_source``. The shared version is
shaped by central's needs, and importing it costs a unit far more than the loop
itself: ``pipeline`` pulls in ``notes`` (1,285 lines of Anthropic commentary),
``weather`` and ``weather_worker``, ``site_ocr`` (pytesseract), ``highlight_watcher``
(PIL), ``site_resolver``, ``sites`` and ``sandbox`` — none of which a
single-microphone box in a field can use. Forking sheds all seven, and lets the
two loops diverge in the directions they are actually going: central toward
multi-site resolution and replay gating, a unit toward duty-cycling and power.

What it drops, and why each is safe here:

* **Highlight gate** — detects YouTube replaying a montage. A microphone cannot
  replay, so there is nothing to gate.
* **Site resolver** — a unit is one static location, given at enrollment. There
  is no camera panning between sites to OCR.
* **Sandbox mode** — an /admin feature the unit's UI cannot set.
* **High-pass relaunch** — only ever applied to ``AlsaSource``; ``MicSource`` is
  a sibling class, so this was already dead code on a unit.
* **Start-delay stagger** — one source, nothing to stagger against.
* **Auth backoff** — YouTube rate-limit handling; ALSA has no such failure.
* **Clip fingerprinting** — the replay filter's input, useless without replays.

What it keeps, faithfully: heartbeat, the confidence-floor tiers and per-species
suppressions, the audio-quality accumulator and its snapshot/sample/prune
cadences, 3 s pre-roll clip writing, and the reconnect backoff with its worker
state transitions.
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import numpy as np

from birdbrain.audio.quality import QualityAccumulator, chunk_features
from birdbrain.audio.source import AudioChunk
from birdbrain.clips import save_chunk
from birdbrain.config import AppConfig, SourceConfig
from birdbrain.detector import BirdNetDetector
from birdbrain.logging import get_logger
from birdbrain.storage import Database

log = get_logger(__name__)

# Transient failures only (ALSA hiccup, ffmpeg exit): 5 → 10 → 20 → 40 → 60.
BACKOFF_INITIAL = 5.0
BACKOFF_MAX = 60.0
# A stretch longer than this counts as healthy and resets the schedule.
HEALTHY_RUN_S = 60.0

# Live-tunable floors are re-read on this cadence. A read, so it stays quick —
# unlike the quality *write*, which has its own much slower cadence.
FLOOR_REFRESH_S = 60.0
QUALITY_SAMPLE_S = 600.0    # trend point for the sparkline
QUALITY_PRUNE_S = 3600.0    # drop samples older than a week
QUALITY_SAMPLE_RETENTION_DAYS = 7


def run_capture(
    cfg: SourceConfig,
    app: AppConfig,
    detector: BirdNetDetector,
    db: Database,
    source,
    stop_event: threading.Event,
) -> None:
    """Run one microphone until ``stop_event``, restarting on failure.

    ``source`` is injected rather than built here so the caller owns device
    selection and tests can drive a fake.
    """
    slog = log.bind(source=cfg.name, kind=cfg.kind)
    slog.info("capture.start", device=cfg.device)
    _safely(slog, "worker.started_failed", db.worker_started, cfg.name)

    backoff = BACKOFF_INITIAL
    while not stop_event.is_set():
        started = time.monotonic()
        last_err = ""
        try:
            _consume(source, cfg, app, detector, db, slog, stop_event)
            if stop_event.is_set():
                break
            last_err = "stream ended"
            slog.warning("capture.eof_reconnect", sleep_s=backoff)
        except Exception as e:
            last_err = str(e)
            slog.warning("capture.error_reconnect", error=last_err[:300], sleep_s=backoff)

        _safely(slog, "worker.backoff_failed", db.worker_backoff, cfg.name, last_err)
        ran_for = time.monotonic() - started
        backoff = (
            BACKOFF_INITIAL if ran_for > HEALTHY_RUN_S else min(backoff * 2, BACKOFF_MAX)
        )
        if stop_event.wait(backoff):
            break

    slog.info("capture.stopped")
    _safely(slog, "worker.stopped_failed", db.worker_stopped, cfg.name)


def _safely(slog, event: str, fn, *args) -> None:
    """Bookkeeping that must never take the capture loop down with it."""
    try:
        fn(*args)
    except Exception:
        slog.exception(event)


def _consume(
    source,
    cfg: SourceConfig,
    app: AppConfig,
    detector: BirdNetDetector,
    db: Database,
    slog,
    stop_event: threading.Event,
) -> None:
    """Pull chunks until the stream ends or ``stop_event`` is set."""
    last_hb = last_floors = last_flush = last_sample = last_prune = 0.0
    suppressed: set[str] = set()
    quality = QualityAccumulator()
    # One chunk of pre-roll so a call straddling a chunk boundary is saved whole
    # rather than clipped at the start. ~580 KB at 48 kHz mono float32.
    prev_samples: np.ndarray | None = None

    for chunk in source.stream(stop_event=stop_event):
        if stop_event.is_set():
            return
        now = time.monotonic()

        if now - last_hb > app.worker_heartbeat_seconds:
            _safely(slog, "worker.heartbeat_failed", db.worker_heartbeat, cfg.name)
            last_hb = now

        # Suppressions only. Central also polls three confidence floors out of
        # app_settings and species_notes here, live-tunable from /admin — a unit
        # has no /admin, sets its floor in .env as tbb_min_confidence, and was
        # therefore querying three tables a minute that it can never write. On
        # the bench unit after six weeks: species_notes had 70 rows and not one
        # with min_confidence set, and app_settings held nothing but debris.
        # Suppressions stay because there is no config equivalent — a unit
        # plagued by one phantom species has no other way to silence it.
        if now - last_floors > FLOOR_REFRESH_S:
            try:
                suppressed = db.species_suppressions_for(cfg.name)
            except Exception:
                slog.exception("suppressions.refresh_failed")
            last_floors = now

        # Accumulate quality from every live chunk, before detection — a silent
        # mic fires no detections, and that silence is exactly what the metric
        # needs to see.
        try:
            quality.update(chunk_features(chunk.samples))
        except Exception:
            slog.exception("quality.update_failed")

        last_flush, last_sample, last_prune = _quality_tick(
            db, cfg, app, quality, slog, now, last_flush, last_sample, last_prune
        )

        # One floor, from the unit's own config (BIRDBRAIN_TBB_MIN_CONFIDENCE).
        try:
            detections = detector.analyze(
                chunk,
                lat=cfg.lat,
                lon=cfg.lon,
                week=cfg.week,
                min_confidence=cfg.min_confidence,
                drop_non_bird=cfg.exclude_non_bird,
            )
        except Exception:
            slog.exception("detect.failed")
            continue

        if suppressed and detections:
            detections = [d for d in detections if d.scientific_name not in suppressed]

        if not detections:
            # Still advance the buffer: the NEXT chunk may detect, and this
            # chunk is its pre-roll.
            prev_samples = chunk.samples
            continue

        clip_path = _save_clip(chunk, prev_samples, app) if app.save_clips else None
        prev_samples = chunk.samples

        db.insert_detections(
            detections,
            clip_path=clip_path,
            site=None,
            latitude=cfg.lat,
            longitude=cfg.lon,
        )
        for d in detections:
            slog.info(
                "detection",
                species=d.common_name,
                scientific=d.scientific_name,
                confidence=round(d.confidence, 3),
                at=d.started_at.isoformat(),
                clip=clip_path,
            )


def _quality_tick(
    db: Database,
    cfg: SourceConfig,
    app: AppConfig,
    quality: QualityAccumulator,
    slog,
    now: float,
    last_flush: float,
    last_sample: float,
    last_prune: float,
) -> tuple[float, float, float]:
    """Three independent cadences on the quality metric, kept out of the main
    loop so it stays readable. Returns the updated timers.

    They are separate on purpose: the current snapshot is a write the UI reads
    (slow — see ``audio_quality_flush_seconds``), the trend sample feeds a 24 h
    sparkline (~10 min), and the prune only has to keep a week's worth.
    """
    if quality.ready and now - last_flush > app.audio_quality_flush_seconds:
        snap = _snapshot(db, cfg, quality, slog)
        if snap is not None:
            _safely(slog, "quality.flush_failed", db.upsert_audio_quality, cfg.name, snap)
        last_flush = now

    if quality.ready and now - last_sample > QUALITY_SAMPLE_S:
        snap = _snapshot(db, cfg, quality, slog)
        if snap is not None:
            _safely(
                slog, "quality.sample_failed", db.append_audio_quality_sample,
                cfg.name, snap["score"], snap["level_dbfs"], snap["structure_score"],
            )
        last_sample = now

    if now - last_prune > QUALITY_PRUNE_S:
        _safely(
            slog, "quality.prune_failed", db.prune_audio_quality_samples,
            datetime.now(UTC) - timedelta(days=QUALITY_SAMPLE_RETENTION_DAYS),
        )
        last_prune = now

    return last_flush, last_sample, last_prune


def _snapshot(db: Database, cfg: SourceConfig, quality: QualityAccumulator, slog):
    """Current metric, with the detection-yield term the masking signal needs:
    loud audio plus near-zero detections means something is drowning the birds."""
    try:
        det_6h = db.detection_count_since(cfg.name, datetime.now(UTC) - timedelta(hours=6))
        return quality.snapshot(detections_per_h=det_6h / 6.0)
    except Exception:
        slog.exception("quality.snapshot_failed")
        return None


def _save_clip(chunk: AudioChunk, prev_samples: np.ndarray | None, app: AppConfig) -> str:
    """Write a 6 s clip: the previous chunk as pre-roll plus this one.

    The filename's timestamp is shifted back by the pre-roll so the file names
    its real start, while the detection rows keep the BirdNET window's own
    start as the detector reported it.
    """
    if prev_samples is None or len(prev_samples) == 0:
        return str(save_chunk(chunk, app.clips_dir, fmt=app.clip_format))
    merged = np.concatenate([prev_samples, chunk.samples])
    pre_s = float(len(prev_samples)) / float(chunk.sample_rate)
    extended = AudioChunk(
        samples=merged,
        sample_rate=chunk.sample_rate,
        started_at=chunk.started_at - timedelta(seconds=pre_s),
        source_name=chunk.source_name,
    )
    return str(save_chunk(extended, app.clips_dir, fmt=app.clip_format))
