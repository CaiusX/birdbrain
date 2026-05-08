from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from africam.audio import AudioSource, RtspSource, YouTubeSource
from africam.clips import save_chunk_wav
from africam.config import AppConfig, OcrConfig, SourceConfig
from africam.detector import BirdNetDetector
from africam.logging import get_logger
from africam.site_ocr import SiteOcrWatcher
from africam.site_resolver import SiteResolver
from africam.sites import Site, load_sites
from africam.storage import Database, RuntimeSourceRow

log = get_logger(__name__)

# How often the supervisor reconciles desired sources (toml + DB) with running threads.
SUPERVISOR_INTERVAL = 15.0


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
    stop_event: threading.Event,
) -> None:
    """Pull chunks from one source and write detections, restarting on errors.

    Wrapped in a retry loop so that a transient failure doesn't kill the
    worker. Exits cleanly when ``stop_event`` is set.
    """
    source = build_source(cfg, app)
    resolver = _build_resolver(cfg, source, sites, db)
    slog = log.bind(source=cfg.name, kind=cfg.kind, multisite=cfg.multisite)
    slog.info("pipeline.start")

    backoff = 5.0
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            _consume_stream(source, resolver, cfg, app, detector, db, slog, stop_event)
            if stop_event.is_set():
                break
            slog.warning("source.eof_reconnect", sleep_s=backoff)
        except Exception as e:
            slog.warning("source.error_reconnect", error=str(e)[:300], sleep_s=backoff)
        if time.monotonic() - started > 60:
            backoff = 5.0
        else:
            backoff = min(backoff * 2, 60.0)
        # Use the event so we wake up immediately on stop instead of sleeping out.
        if stop_event.wait(backoff):
            break
    slog.info("pipeline.stopped")


def _consume_stream(
    source: AudioSource,
    resolver,
    cfg: SourceConfig,
    app: AppConfig,
    detector: BirdNetDetector,
    db: Database,
    slog,
    stop_event: threading.Event,
) -> None:
    for chunk in source.stream(stop_event=stop_event):
        if stop_event.is_set():
            return
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


# --- Supervisor: spawns/stops worker threads as the desired source set changes ---


@dataclass
class _Worker:
    cfg: SourceConfig
    thread: threading.Thread
    stop_event: threading.Event


def _runtime_row_to_cfg(row: RuntimeSourceRow) -> SourceConfig:
    return SourceConfig(
        name=row.name,
        kind=row.kind,  # type: ignore[arg-type]
        url=row.url,
        lat=row.lat,
        lon=row.lon,
        min_confidence=row.min_confidence,
        multisite=row.multisite,
        cookies_from_browser=row.cookies_from_browser,
        cookies_file=row.cookies_file,
        ocr=OcrConfig(),
    )


def _desired_sources(
    static_sources: Iterable[SourceConfig],
    db: Database,
) -> dict[str, SourceConfig]:
    """Merge file-based and runtime sources by name. Runtime wins on conflict
    so the user can override a static source via the UI without editing TOML."""
    out: dict[str, SourceConfig] = {s.name: s for s in static_sources}
    for row in db.list_runtime_sources():
        out[row.name] = _runtime_row_to_cfg(row)
    return out


def run_all(sources: Iterable[SourceConfig], app: AppConfig) -> None:
    """Start a supervisor loop that maintains one worker per desired source.

    "Desired" = sources from sources.toml plus undeleted runtime sources from
    the DB. The supervisor wakes every SUPERVISOR_INTERVAL seconds, diffs
    desired vs running, and starts/stops workers accordingly.
    """
    detector = BirdNetDetector()
    db = Database(app.db_url)
    sites = load_sites(app.sites_file)
    log.info("sites.loaded", count=len(sites), path=str(app.sites_file))

    static_sources = list(sources)
    workers: dict[str, _Worker] = {}

    def reconcile() -> None:
        desired = _desired_sources(static_sources, db)
        # Stop workers whose source has been removed.
        for name in list(workers):
            if name not in desired:
                log.info("supervisor.stopping", source=name)
                workers[name].stop_event.set()
                # Don't join here — joining can block if ffmpeg is mid-read.
                # The thread will exit once it notices the event.
                del workers[name]
        # Start workers for newly-desired sources.
        for name, cfg in desired.items():
            if name in workers and workers[name].thread.is_alive():
                continue
            stop_event = threading.Event()
            t = threading.Thread(
                target=run_source,
                args=(cfg, app, detector, db, sites, stop_event),
                name=f"src-{name}",
                daemon=True,
            )
            t.start()
            workers[name] = _Worker(cfg=cfg, thread=t, stop_event=stop_event)
            log.info("supervisor.started", source=name, kind=cfg.kind)

    reconcile()
    log.info("pipeline.workers_started", count=len(workers))

    try:
        while True:
            time.sleep(SUPERVISOR_INTERVAL)
            reconcile()
    except KeyboardInterrupt:
        log.info("pipeline.interrupt")
        for w in workers.values():
            w.stop_event.set()
