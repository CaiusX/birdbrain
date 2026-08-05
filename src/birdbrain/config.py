"""Central's configuration: the unit's settings plus its own.

The unit-side settings live in :mod:`birdbrain.config_core`; ``AppConfig``
inherits them, so ``from birdbrain.config import AppConfig`` is unchanged and
every field is still on one object.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from birdbrain.config_core import UnitConfig


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


# Transitional: the pre-2026-07 name of this project. See
# settings_customise_sources below and resolve_db_url.
LEGACY_ENV_PREFIX = "AFRICAM_"
LEGACY_DB_PATH = Path("data/africam.sqlite")
DEFAULT_DB_URL = "sqlite:///data/birdbrain.sqlite"


def _sqlite_dir(db_url: str) -> Path | None:
    """Directory holding an on-disk SQLite database, or None for anything else
    (``:memory:``, postgres, …). Used to anchor sibling state files."""
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return None
    raw = db_url[len(prefix):]
    if not raw or raw.startswith(":memory:"):
        return None
    return Path(raw).expanduser().resolve().parent


def resolve_db_url(db_url: str) -> str:
    """Fall back to the pre-rename database if the new one is not there yet.

    Without this the rename's worst failure mode is silent: pointing at a
    filename that does not exist makes SQLite create a fresh empty database
    next to 592 MB of real detections, and the site comes up looking merely
    "quiet" rather than broken. Only applies to on-disk sqlite URLs.
    """
    prefix = "sqlite:///"
    if db_url != DEFAULT_DB_URL or not db_url.startswith(prefix):
        return db_url
    new_path = Path(db_url[len(prefix):])
    if new_path.exists() or not LEGACY_DB_PATH.exists():
        return db_url
    print(
        f"[config] {new_path} not found; using pre-rename database "
        f"{LEGACY_DB_PATH}. Rename it to complete the migration.",
        flush=True,
    )
    return f"{prefix}{LEGACY_DB_PATH}"



class AppConfig(UnitConfig):
    """Top-level config. Loaded from env (BIRDBRAIN_*) and an optional .env file."""

    sources_file: Path = Path("sources.toml")
    sites_file: Path = Path("sites.toml")
    # Perceptual clip fingerprinting (birdbrain.audio_hash) for the YouTube
    # ad/highlight replay filter. Worth its cost on central; turn OFF on a
    # capture unit, where it is both useless and expensive — see the skip in
    # pipeline.py for the measured numbers.
    audio_hash_enabled: bool = True
    # Measured per-chunk BirdNET inference time (ms), used only to estimate the
    # serialized-detector saturation shown on the health panel. All workers
    # share one locked detector, so capacity ≈ chunk_seconds*1000 / this.
    # Re-measure (BirdNetDetector.analyze on a 3s chunk) if the hardware changes.
    # ~100ms measured on the Pi 5 (2026-06, median 99 / mean 102 under load).
    inference_ms_estimate: float = 100.0
    # Xeno-Canto API v3 requires a key (free, per-account). When set, the
    # audition modal shows reference recordings inline. When unset, it falls
    # back to a plain "search XC" link that needs no auth.
    xeno_canto_key: str | None = None

    # Tester accounts. ``secret_key`` signs the session cookie; if unset, a
    # stable key is generated once and persisted in app_settings (so logins
    # survive restarts). ``invite_code`` gates self-serve signup over the
    # public site; if neither this nor the app_settings ``invite_code`` is set,
    # signups are disabled. Env: BIRDBRAIN_SECRET_KEY, BIRDBRAIN_INVITE_CODE.
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


def load_sources(path: Path) -> list[SourceConfig]:
    if not path.exists():
        raise FileNotFoundError(
            f"Sources file not found at {path}. Copy sources.example.toml to sources.toml."
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)
    entries = raw.get("source", [])
    return [SourceConfig.model_validate(e) for e in entries]
