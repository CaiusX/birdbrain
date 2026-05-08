from __future__ import annotations

import threading
from collections.abc import Iterable

from africam.audio import AudioSource, RtspSource, YouTubeSource
from africam.clips import save_chunk_wav
from africam.config import AppConfig, SourceConfig
from africam.detector import BirdNetDetector
from africam.logging import get_logger
from africam.storage import Database

log = get_logger(__name__)


def build_source(cfg: SourceConfig, app: AppConfig) -> AudioSource:
    common = {
        "name": cfg.name,
        "sample_rate": app.sample_rate,
        "chunk_seconds": app.chunk_seconds,
    }
    if cfg.kind == "youtube":
        return YouTubeSource(url=cfg.url, **common)
    if cfg.kind == "rtsp":
        return RtspSource(url=cfg.url, **common)
    raise ValueError(f"Unknown source kind: {cfg.kind!r}")


def run_source(
    cfg: SourceConfig,
    app: AppConfig,
    detector: BirdNetDetector,
    db: Database,
) -> None:
    """Block, pulling chunks from one source and writing detections."""
    source = build_source(cfg, app)
    slog = log.bind(source=cfg.name, kind=cfg.kind)
    slog.info("pipeline.start")

    for chunk in source.stream():
        try:
            detections = detector.analyze(
                chunk,
                lat=cfg.lat,
                lon=cfg.lon,
                week=cfg.week,
                min_confidence=cfg.min_confidence,
            )
        except Exception:
            slog.exception("detect.failed")
            continue

        if not detections:
            continue

        clip_path = None
        if app.save_clips:
            clip_path = str(save_chunk_wav(chunk, app.clips_dir))

        db.insert_detections(detections, clip_path=clip_path)
        for d in detections:
            slog.info(
                "detection",
                species=d.common_name,
                scientific=d.scientific_name,
                confidence=round(d.confidence, 3),
                at=d.started_at.isoformat(),
                clip=clip_path,
            )


def run_all(sources: Iterable[SourceConfig], app: AppConfig) -> None:
    """Run every configured source in its own thread, sharing one detector + DB."""
    detector = BirdNetDetector()
    db = Database(app.db_url)

    threads: list[threading.Thread] = []
    for cfg in sources:
        t = threading.Thread(
            target=run_source,
            args=(cfg, app, detector, db),
            name=f"src-{cfg.name}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    log.info("pipeline.workers_started", count=len(threads))
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log.info("pipeline.interrupt")
