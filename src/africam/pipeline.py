from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from africam.audio import AudioSource, RtspSource, YouTubeSource
from africam.clips import save_chunk
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

# Normal-failure backoff: fast retries for transient blips (ffmpeg disconnect,
# network glitch). 5 → 10 → 20 → 40 → 60 capped.
NORMAL_BACKOFF_INITIAL = 5.0
NORMAL_BACKOFF_MAX = 60.0

# Auth-failure backoff: YouTube rate-limits aggressively when many yt-dlp
# calls come from one IP — we back off hard so a single failing source can't
# poison auth for the rest. 5 min → 10 → 20 → 30 capped.
AUTH_BACKOFF_INITIAL = 300.0
AUTH_BACKOFF_MAX = 1800.0

# Substrings that indicate a YouTube auth/bot-check failure — these warrant
# the auth backoff schedule rather than the normal one.
_AUTH_ERR_MARKERS = (
    "Sign in to confirm",
    "Use --cookies",
    "cookies-from-browser",
    "Failed to decrypt",
)


def _is_auth_error(err: str) -> bool:
    return any(m in err for m in _AUTH_ERR_MARKERS)


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
    try:
        db.worker_started(cfg.name)
    except Exception:
        slog.exception("worker.heartbeat_started_failed")

    backoff = NORMAL_BACKOFF_INITIAL
    while not stop_event.is_set():
        started = time.monotonic()
        last_err = ""
        try:
            _consume_stream(source, resolver, cfg, app, detector, db, slog, stop_event)
            if stop_event.is_set():
                break
            slog.warning("source.eof_reconnect", sleep_s=backoff)
            last_err = "stream EOF / reconnect"
        except Exception as e:
            last_err = str(e)
            slog.warning(
                "source.error_reconnect",
                error=last_err[:300],
                sleep_s=backoff,
                auth=_is_auth_error(last_err),
            )
        try:
            db.worker_backoff(cfg.name, last_err)
        except Exception:
            slog.exception("worker.heartbeat_backoff_failed")

        ran_for = time.monotonic() - started
        if ran_for > 60:
            # Successful stretch — reset to fast retries.
            backoff = NORMAL_BACKOFF_INITIAL
        elif _is_auth_error(last_err):
            # YouTube rate-limits aggressively when many yt-dlp calls come from
            # one IP. Back off hard so a single failing source can't poison auth
            # for other workers; gives the IP cooldown time to lift.
            if backoff < AUTH_BACKOFF_INITIAL:
                backoff = AUTH_BACKOFF_INITIAL
            else:
                backoff = min(backoff * 2, AUTH_BACKOFF_MAX)
        else:
            # Transient (ffmpeg blip, brief network drop) — keep the fast schedule.
            backoff = min(backoff * 2, NORMAL_BACKOFF_MAX)

        # Use the event so the worker wakes immediately on stop_event rather than sleeping it out.
        if stop_event.wait(backoff):
            break
    slog.info("pipeline.stopped")
    try:
        db.worker_stopped(cfg.name)
    except Exception:
        slog.exception("worker.heartbeat_stopped_failed")


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
    last_hb = 0.0
    last_species_floor_refresh = 0.0
    species_floor: dict[str, float] = {}
    # How often to refresh per-species threshold overrides. Cheap query, but
    # we don't need millisecond freshness — once a minute is plenty given the
    # UI tweaks land seconds-to-minutes after the user wants them.
    SPECIES_FLOOR_REFRESH_S = 60.0
    for chunk in source.stream(stop_event=stop_event):
        if stop_event.is_set():
            return
        now = time.monotonic()
        if now - last_hb > 15.0:
            try:
                db.worker_heartbeat(cfg.name)
            except Exception:
                slog.exception("worker.heartbeat_update_failed")
            last_hb = now
        if now - last_species_floor_refresh > SPECIES_FLOOR_REFRESH_S:
            try:
                species_floor = db.species_min_confidence_map()
            except Exception:
                slog.exception("worker.species_floor_refresh_failed")
            last_species_floor_refresh = now
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

        # Apply per-species overrides: a species' own floor can be ABOVE the
        # source default (to suppress loud common species like Egyptian Goose
        # / Hadada that otherwise drown out everything quieter).
        if species_floor and detections:
            detections = [
                d for d in detections
                if d.confidence >= species_floor.get(d.scientific_name, cfg.min_confidence)
            ]

        if not detections:
            continue

        clip_path = None
        if app.save_clips:
            clip_path = str(save_chunk(chunk, app.clips_dir, fmt=app.clip_format))

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
        timezone=row.timezone or "UTC",
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
