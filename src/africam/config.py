from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OcrConfig(BaseModel):
    """Per-source OCR settings for site auto-detection."""

    enabled: bool = False
    # How often to grab a frame and run OCR.
    every_seconds: int = 30
    # Optional [x, y, w, h] crop in pixels of the frame, where the camera caption
    # appears. Tighter crops are faster and more accurate. None = OCR full frame.
    crop: tuple[int, int, int, int] | None = None
    # Minimum number of consecutive matches against the same site before the
    # resolver promotes it to "current". Guards against transient OCR misreads.
    confirm_count: int = 1


class SourceConfig(BaseModel):
    name: str
    # "device" = pi's AlsaSource, "mic" = TBB's MicSource — both capture local
    # audio (kept separate during the tbb->pi merge; unify later). url is unused
    # by these but stays required so the youtube/rtsp path is unchanged.
    kind: Literal["youtube", "rtsp", "device", "mic"]
    url: str
    # ALSA capture device for the "mic" kind (e.g. "plughw:1,0"). Ignored by the
    # youtube/rtsp kinds. Lives here so a MicSource's config travels with it.
    device: str = "plughw:1,0"
    # Drop BirdNET's non-bird noise classes (Engine, Dog, Siren, …). Off by
    # default so central is unchanged; the TBB profile turns it on.
    exclude_non_bird: bool = False
    lat: float | None = None
    lon: float | None = None
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # Week of year (1-48) used by BirdNET's location/time filter. None = current week.
    week: int | None = None
    # If true, the source rotates between cameras and the pipeline should
    # consult the SiteResolver per-detection to override lat/lon and record
    # the active site name. False = static lat/lon, no resolver.
    multisite: bool = False
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    # YouTube only. Passed through to yt-dlp's --cookies-from-browser; needed
    # when YouTube's anti-bot checks demand auth ("Sign in to confirm you're
    # not a bot"). Common values: "chrome", "firefox", "edge", "brave".
    # Closing the browser before the pipeline starts avoids cookie-db locks.
    # NOTE: Chrome 127+ uses App-Bound (DPAPI) cookie encryption which yt-dlp
    # may fail to decrypt — use cookies_file in that case.
    cookies_from_browser: str | None = None
    # Path to a Netscape-format cookies.txt. Export once with a browser
    # extension like "Get cookies.txt LOCALLY" (filter to youtube.com) and
    # point at the file. More robust than cookies_from_browser on Windows.
    cookies_file: Path | None = None
    # IANA timezone for display (e.g. "Africa/Johannesburg"). The DB always
    # stores UTC; the dashboard converts using this when rendering rows.
    timezone: str = "UTC"


class AppConfig(BaseSettings):
    """Top-level config. Loaded from env (AFRICAM_*) and an optional .env file."""

    model_config = SettingsConfigDict(
        env_prefix="AFRICAM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sources_file: Path = Path("sources.toml")
    sites_file: Path = Path("sites.toml")
    db_url: str = "sqlite:///data/africam.sqlite"
    clips_dir: Path = Path("data/clips")
    save_clips: bool = True
    # Container/codec for saved detection clips. OGG Vorbis is ~10× smaller
    # than WAV for negligible perceptual loss in audition. Use 'flac' for
    # lossless compression, or 'wav' to revert to uncompressed PCM.
    clip_format: Literal["ogg", "wav", "flac"] = "ogg"
    sample_rate: int = 48_000
    chunk_seconds: float = 3.0
    # Measured per-chunk BirdNET inference time (ms), used only to estimate the
    # serialized-detector saturation shown on the health panel. All workers
    # share one locked detector, so capacity ≈ chunk_seconds*1000 / this.
    # Re-measure (BirdNetDetector.analyze on a 3s chunk) if the hardware changes.
    # ~100ms measured on the Pi 5 (2026-06, median 99 / mean 102 under load).
    inference_ms_estimate: float = 100.0
    log_level: str = "INFO"
    # Xeno-Canto API v3 requires a key (free, per-account). When set, the
    # audition modal shows reference recordings inline. When unset, it falls
    # back to a plain "search XC" link that needs no auth.
    xeno_canto_key: str | None = None

    # Tester accounts. ``secret_key`` signs the session cookie; if unset, a
    # stable key is generated once and persisted in app_settings (so logins
    # survive restarts). ``invite_code`` gates self-serve signup over the
    # public site; if neither this nor the app_settings ``invite_code`` is set,
    # signups are disabled. Env: AFRICAM_SECRET_KEY, AFRICAM_INVITE_CODE.
    secret_key: str | None = None
    invite_code: str | None = None

    # Background species-note generator. Picks one stale species every
    # notes_tick_seconds and asks Claude to write a 1–2 paragraph commentary
    # grounded in our detection footprint. Dormant unless ANTHROPIC_API_KEY
    # is present in the process env.
    notes_enabled: bool = True
    # Background sweeper (web process) that pre-fetches Wikipedia photo + range
    # maps for every detected species and caches them on the species row, so
    # the header media loads instantly and survives restarts. Needs only
    # network access to Wikipedia (no API key).
    media_cache_enabled: bool = True
    notes_model: str = "claude-haiku-4-5"
    notes_tick_seconds: int = 300
    notes_stale_days: int = 7
    notes_min_detections: int = 3
    # Per-(species, site) note threshold. Higher than notes_min_detections so
    # we don't burn budget on one-off visitors — but low enough to include
    # interesting rarities at a site.
    notes_species_site_min_detections: int = 30
    # Multiplier on detection_count_at_gen that retriggers regeneration even
    # before the age cutoff hits — i.e. "data roughly doubled, old note is
    # stale even at 2 days old."
    notes_regen_count_factor: float = 2.0
    # Lookback window for the daily-brief worker. Each tick scans this
    # many past UTC days (excluding today) for missing briefs and fills
    # the gaps oldest-first. Keep modest — re-fills after multi-day
    # outages but doesn't ask Claude to write briefs about ancient history.
    notes_brief_lookback_days: int = 3

    # Background hourly weather archiver. Walks the union of live sources
    # and sites.toml every weather_tick_seconds, pulls hourly observations
    # from Open-Meteo, and upserts them into weather_observations. A fresh
    # coord gets a backfill (weather_backfill_days, capped by Open-Meteo's
    # 92-day forecast-endpoint window); subsequent ticks revise only the
    # last ~48 h. Lets the dashboard read weather as a pure local SQL
    # lookup instead of blocking UI requests on the upstream API.
    weather_archive_enabled: bool = True
    weather_tick_seconds: int = 3600
    weather_backfill_days: int = 92

    # --- TinyBirdBrain (TBB) capture-unit profile ---
    # These are inert on the central deploy. The `tbb-pipeline` / `tbb-web`
    # entrypoints read them to run a single USB-mic source with the
    # central-only workers (notes, weather, media, anomalies, OCR) left off.
    # Override via AFRICAM_TBB_* env or the unit's .env.
    tbb_unit_id: str = "tbb-dev"
    # ALSA capture device of the USB mic; pick from `arecord -l` on the unit.
    tbb_mic_device: str = "plughw:1,0"
    # Static location for BirdNET's locality filter (set at enrollment). None
    # leaves the filter off (global model).
    tbb_lat: float | None = None
    tbb_lon: float | None = None
    tbb_min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    tbb_timezone: str = "UTC"
    # Local clip retention: prune saved clips older than this many days to
    # protect the SD card. The pipeline runs the prune sweep itself.
    tbb_clip_retention_days: int = 14
    tbb_prune_tick_seconds: int = 3600
    # Sync to central (birdbrain.co.za). Off by default; enable per unit once a
    # device token is issued. The agent runs as a background task in tbb-web.
    tbb_sync_enabled: bool = False
    tbb_central_url: str | None = None  # e.g. https://birdbrain.co.za
    tbb_device_token: str | None = None
    tbb_sync_interval_seconds: int = 45
    tbb_sync_batch_size: int = 200
    # High-water-mark store (last synced detection id). A plain JSON file so the
    # unit's DB schema stays identical to central's.
    tbb_sync_state_file: Path = Path("data/tbb_sync_state.json")

    # --- BirdNET-Cloud bridge (PixCams) ---
    # Optional second sync target: push detections to birdnetcloud.com instead
    # of running their edge agent, which would be a whole second BirdNET
    # pipeline competing for the same mic. Off unless a token is present, so
    # units that track origin/tbb without one are entirely unaffected.
    birdnetcloud_enabled: bool = False
    birdnetcloud_endpoint: str = "https://api.birdnetcloud.com"
    birdnetcloud_token: str | None = None
    # Alternative to the inline token: read it from a 0600 file, keeping the
    # secret out of the unit's .env.
    birdnetcloud_token_file: Path | None = None
    # Their edge agent defaults to 0.7 and it matches our own trust floor —
    # below this the dashboard fills with noise.
    birdnetcloud_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    birdnetcloud_interval_seconds: int = 60
    # Heartbeat on its own clock so a metered/field unit can poll for detections
    # often while rarely spending a request just to say "still alive".
    birdnetcloud_heartbeat_seconds: int = 60
    # Clips are ~60KB each and dominate a unit's uplink (~19MB/day at the
    # observed detection rate). Turn off for field/metered deployments — you keep
    # every detection, you lose playable audio in their dashboard.
    birdnetcloud_upload_clips: bool = True
    # Their API takes one detection per request, so a tick is capped rather
    # than draining an unbounded backlog in one go.
    birdnetcloud_max_per_tick: int = 60
    birdnetcloud_state_file: Path = Path("data/birdnetcloud_sync_state.json")


def load_sources(path: Path) -> list[SourceConfig]:
    if not path.exists():
        raise FileNotFoundError(
            f"Sources file not found at {path}. Copy sources.example.toml to sources.toml."
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)
    entries = raw.get("source", [])
    return [SourceConfig.model_validate(e) for e in entries]
