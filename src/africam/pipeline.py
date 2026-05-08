from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from africam.audio import AudioSource, RtspSource, YouTubeSource
from africam.clips import save_chunk_wav
from africam.config import AppConfig, SourceConfig
from africam.detector import BirdNetDetector
from africam.logging import get_logger
from africam.site_ocr import SiteOcrWatcher
from africam.site_resolver import SiteResolver
from africam.sites import Site, load_sites
from africam.storage import Database

log = get_logger(__name__)


def build_source(cfg: SourceConfig, app: AppConfig) -> AudioSource:
    common = {
        "name": cfg.name,
        "sample_rate": app.sample_rate,
        "chunk_seconds": app.chunk_seconds,
    }
    if cfg.kind == "youtube":
        return YouTubeSource(
            url=cfg.url,
            cookies_from_browser=cfg.cookies_from_browser,
            cookies_file=str(cfg.cookies_file) if cfg.cookies_file else None,
            **common,
        )
    if cfg.kind == "rtsp":
        return RtspSource(url=cfg.url, **common)
    raise ValueError(f"Unknown source kind: {cfg.kind!r}")


def _build_resolver(
    cfg: SourceConfig,
    source: AudioSource,
    sites: dict[str, Site],
    db: Database,
) -> SiteResolver:
    ocr: SiteOcrWatcher | None = None
    if cfg.multisite and cfg.ocr.enabled and sites:
        ocr = SiteOcrWatcher(
            source=cfg,
            sites=sites,
            ocr_cfg=cfg.ocr,
            resolve_stream_url=source.current_url,
        )
        ocr.start()
    elif cfg.multisite and not sites:
        log.warning("multisite.no_sites_loaded", source=cfg.name)
    return SiteResolver(source=cfg, sites=sites, db=db, ocr=ocr)


def run_source(
    cfg: SourceConfig,
    app: AppConfig,
    detector: BirdNetDetector,
    db: Database,
    sites: dict[str, Site],
) -> None:
    """Pull chunks from one source and write detections, restarting on errors.

    Wrapped in a retry loop so that a transient failure (yt-dlp bot-check,
    ffmpeg disconnection, network blip) doesn't kill the worker — we sleep
    with bounded exponential backoff and try again from scratch.
    """
    source = build_source(cfg, app)
    resolver = _build_resolver(cfg, source, sites, db)
    slog = log.bind(source=cfg.name, kind=cfg.kind, multisite=cfg.multisite)
    slog.info("pipeline.start")

    backoff = 5.0
    while True:
        started = time.monotonic()
        try:
            _consume_stream(source, resolver, cfg, app, detector, db, slog)
            slog.warning("source.eof_reconnect", sleep_s=backoff)
        except Exception as e:
            slog.warning("source.error_reconnect", error=str(e)[:300], sleep_s=backoff)
        # If we were streaming successfully for a while, treat the failure as
        # fresh and reset the backoff. Otherwise keep doubling, capped at 60s
        # so the pipeline recovers promptly once the underlying issue is fixed.
        if time.monotonic() - started > 60:
            backoff = 5.0
        else:
            backoff = min(backoff * 2, 60.0)
        time.sleep(backoff)


def _consume_stream(
    source: AudioSource,
    resolver,  # SiteResolver
    cfg: SourceConfig,
    app: AppConfig,
    detector: BirdNetDetector,
    db: Database,
    slog,
) -> None:
    for chunk in source.stream():
        resolved = resolver.current()
        try:
            detections = detector.analyze(
                chunk,
                lat=resolved.latitude,
                lon=resolved.longitude,
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

        db.insert_detections(
            detections,
            clip_path=clip_path,
            site=resolved.site,
            latitude=resolved.latitude,
            longitude=resolved.longitude,
        )
        for d in detections:
            slog.info(
                "detection",
                species=d.common_name,
                scientific=d.scientific_name,
                confidence=round(d.confidence, 3),
                at=d.started_at.isoformat(),
                site=resolved.site,
                site_by=resolved.detected_by,
                clip=clip_path,
            )


def run_all(sources: Iterable[SourceConfig], app: AppConfig) -> None:
    """Run every configured source in its own thread, sharing one detector + DB."""
    detector = BirdNetDetector()
    db = Database(app.db_url)
    sites = load_sites(app.sites_file)
    log.info("sites.loaded", count=len(sites), path=str(app.sites_file))

    threads: list[threading.Thread] = []
    for cfg in sources:
        t = threading.Thread(
            target=run_source,
            args=(cfg, app, detector, db, sites),
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
