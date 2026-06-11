from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

import numpy as np

from africam.audio import AudioSource, RtspSource, YouTubeSource
from africam.audio.source import AudioChunk
from africam.audio_hash import clip_hash
from africam.clips import save_chunk
from africam.config import AppConfig, OcrConfig, SourceConfig
from africam.detector import BirdNetDetector
from africam.logging import get_logger
from africam.notes import start_notes_worker
from africam.highlight_watcher import HighlightWatcher
from africam.site_ocr import SiteOcrWatcher
from africam.site_resolver import SiteResolver
from africam.sites import Site, load_sites
from africam.storage import Database, RuntimeSourceRow
from africam.weather_worker import start_weather_worker

log = get_logger(__name__)

# How often the supervisor reconciles desired sources (toml + DB) with running threads.
SUPERVISOR_INTERVAL = 15.0

# A worker that hasn't heartbeat in this long is presumed stuck (e.g. ffmpeg
# blocked on a dead HLS stream). The watchdog kicks it and lets reconcile()
# spawn a replacement. Generous vs. the 15 s heartbeat cadence so a slow
# chunk or DB blip doesn't trigger a false restart.
STALE_HEARTBEAT_S = 180.0

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

    # Optional highlight-reel gate (opt-in per source via the app_settings key
    # ``gate_highlights:<name>``). When the source is showing a replayed
    # highlight montage, its audio isn't live, so the watcher tells the consume
    # loop to skip logging detections. YouTube only — that's where the banner is.
    highlight_watcher: HighlightWatcher | None = None
    try:
        if cfg.kind == "youtube" and db.get_setting(f"gate_highlights:{cfg.name}"):
            highlight_watcher = HighlightWatcher(
                source_name=cfg.name,
                url=cfg.url,
                cookies_file=str(cfg.cookies_file) if cfg.cookies_file else None,
            )
            highlight_watcher.start()
    except Exception:
        slog.exception("highlight.start_failed")
        highlight_watcher = None

    backoff = NORMAL_BACKOFF_INITIAL
    while not stop_event.is_set():
        started = time.monotonic()
        last_err = ""
        try:
            _consume_stream(
                source, resolver, cfg, app, detector, db, slog, stop_event,
                highlight_watcher,
            )
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
    if highlight_watcher is not None:
        highlight_watcher.stop()
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
    highlight_watcher: HighlightWatcher | None = None,
) -> None:
    last_hb = 0.0
    last_species_floor_refresh = 0.0
    species_floor: dict[str, float] = {}
    # Site-wide detection floor set from the admin UI. None = no override, so
    # each source uses its own cfg.min_confidence. Polled on the same cadence
    # as the per-species floors below.
    global_min: float | None = None
    # Per-source detection-floor override for THIS source (admin UI). None =
    # use cfg.min_confidence. Polled on the same cadence.
    source_min: float | None = None
    # How often to refresh per-species threshold overrides. Cheap query, but
    # we don't need millisecond freshness — once a minute is plenty given the
    # UI tweaks land seconds-to-minutes after the user wants them.
    SPECIES_FLOOR_REFRESH_S = 60.0
    # Rolling pre-roll buffer: keep the previous chunk's PCM samples per
    # worker so saved clips include the 3 s before the BirdNET window. Calls
    # that straddle a chunk boundary used to be cut at the start (we'd save
    # only the half BirdNET happened to fire on); with pre-roll, the full
    # call lives in seconds 3-6 of the saved clip. Memory cost: one chunk
    # per worker (~580 KB at 48 kHz mono float32, 3 s).
    prev_samples: np.ndarray | None = None
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
        # Skip chunks while the source is showing a replayed highlight reel:
        # its audio isn't live, so any detection would be bogus. Heartbeat
        # above still fires, so the worker stays healthy during the montage.
        if highlight_watcher is not None and highlight_watcher.is_active():
            prev_samples = None  # don't bleed highlight audio into live pre-roll
            continue
        if now - last_species_floor_refresh > SPECIES_FLOOR_REFRESH_S:
            try:
                species_floor = db.species_min_confidence_map()
                global_min = db.global_min_confidence()
                source_min = db.source_min_confidence(cfg.name)
            except Exception:
                slog.exception("worker.species_floor_refresh_failed")
            last_species_floor_refresh = now
        # Cutoff tiers, highest priority first: a site-wide floor overrides
        # everything; else a per-source override; else the source's own value.
        base_min = source_min if source_min is not None else cfg.min_confidence
        effective_min = global_min if global_min is not None else base_min
        resolved = resolver.current()
        try:
            detections = detector.analyze(
                chunk,
                lat=resolved.latitude,
                lon=resolved.longitude,
                week=cfg.week,
                min_confidence=effective_min,
            )
        except Exception:
            slog.exception("detect.failed")
            continue

        # Apply per-species overrides: a species' own floor can be ABOVE the
        # effective source floor (to suppress loud common species like Egyptian
        # Goose / Hadada that otherwise drown out everything quieter).
        if species_floor and detections:
            detections = [
                d for d in detections
                if d.confidence >= species_floor.get(d.scientific_name, effective_min)
            ]

        if not detections:
            # Even when no detections fire, advance the pre-roll buffer so
            # the NEXT chunk (if it has detections) carries this one's audio
            # as its pre-roll context.
            prev_samples = chunk.samples
            continue

        clip_path = None
        if app.save_clips:
            # Prepend the previous chunk's samples so the saved clip is 6 s:
            # 3 s pre-roll + 3 s BirdNET window. Filename's started_at gets
            # shifted back so the on-disk file uniquely names its real start;
            # DB-level Detection.started_at stays at the BirdNET window's
            # actual start (set by the detector).
            if prev_samples is not None and len(prev_samples) > 0:
                merged = np.concatenate([prev_samples, chunk.samples])
                pre_s = float(len(prev_samples)) / float(chunk.sample_rate)
                extended = AudioChunk(
                    samples=merged,
                    sample_rate=chunk.sample_rate,
                    started_at=chunk.started_at - timedelta(seconds=pre_s),
                    source_name=chunk.source_name,
                )
                clip_path = str(save_chunk(extended, app.clips_dir, fmt=app.clip_format))
            else:
                clip_path = str(save_chunk(chunk, app.clips_dir, fmt=app.clip_format))
        # Buffer this chunk for next iteration's pre-roll.
        prev_samples = chunk.samples

        # Perceptual fingerprint of the saved clip, used by the replay
        # filter to hide YouTube ad/highlight loops. All detections from one
        # chunk share the same clip and therefore the same hash. Failures
        # are non-fatal — we'd rather lose a row's dedup info than skip
        # writing the detection itself.
        audio_hash: str | None = None
        if clip_path:
            try:
                audio_hash = clip_hash(clip_path)
            except Exception as e:
                slog.warning("audio_hash.failed", clip=clip_path, error=str(e)[:200])

        db.insert_detections(
            detections,
            clip_path=clip_path,
            site=resolved.site,
            latitude=resolved.latitude,
            longitude=resolved.longitude,
            audio_hash=audio_hash,
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
    so the user can override a static source via the UI without editing TOML.
    Sources flagged via the /admin disable toggle are dropped from the desired
    set so the supervisor stops their workers on the next tick."""
    disabled = db.list_disabled_source_names()
    out: dict[str, SourceConfig] = {
        s.name: s for s in static_sources if s.name not in disabled
    }
    for row in db.list_runtime_sources():
        if row.name in disabled:
            continue
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

    def kick_stale() -> None:
        """Drop stuck workers from the registry so reconcile() respawns them.

        A worker can heartbeat-silently if its chunk iterator is blocked
        (most common: ffmpeg reading a stalled HLS segment that never EOFs).
        The thread can't be joined safely — it's wedged in a kernel read —
        but we can signal stop_event, mark the slot 'stalled' for the admin
        UI, and let the next reconcile() launch a fresh worker. The wedged
        thread will eventually die when ffmpeg times out or the process is
        cleaned up.
        """
        for name in db.stale_workers(STALE_HEARTBEAT_S):
            w = workers.get(name)
            if w is None:
                continue
            log.warning(
                "supervisor.stalled",
                source=name,
                stale_threshold_s=STALE_HEARTBEAT_S,
            )
            try:
                db.worker_stalled(name)
            except Exception:
                log.exception("supervisor.worker_stalled_write_failed", source=name)
            w.stop_event.set()
            del workers[name]

    reconcile()
    log.info("pipeline.workers_started", count=len(workers))

    # Optional background commentary generator. Idempotent — stays dormant
    # if the API key isn't set or anthropic isn't installed; never raises
    # back into the supervisor loop. Sources passed through so the site-note
    # tick knows which source names are valid candidates.
    start_notes_worker(db, app, static_sources)

    # Background hourly weather archiver. Fills weather_observations for
    # every (source ∪ site) coord so dashboard reads stay local.
    start_weather_worker(db, app, static_sources, sites)

    try:
        while True:
            time.sleep(SUPERVISOR_INTERVAL)
            kick_stale()
            reconcile()
    except KeyboardInterrupt:
        log.info("pipeline.interrupt")
        for w in workers.values():
            w.stop_event.set()
