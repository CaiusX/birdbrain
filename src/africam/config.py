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
    kind: Literal["youtube", "rtsp"]
    url: str
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
    log_level: str = "INFO"
    # Xeno-Canto API v3 requires a key (free, per-account). When set, the
    # audition modal shows reference recordings inline. When unset, it falls
    # back to a plain "search XC" link that needs no auth.
    xeno_canto_key: str | None = None

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
    # Multiplier on detection_count_at_gen that retriggers regeneration even
    # before the age cutoff hits — i.e. "data roughly doubled, old note is
    # stale even at 2 days old."
    notes_regen_count_factor: float = 2.0
    # Lookback window for the daily-brief worker. Each tick scans this
    # many past UTC days (excluding today) for missing briefs and fills
    # the gaps oldest-first. Keep modest — re-fills after multi-day
    # outages but doesn't ask Claude to write briefs about ancient history.
    notes_brief_lookback_days: int = 3


def load_sources(path: Path) -> list[SourceConfig]:
    if not path.exists():
        raise FileNotFoundError(
            f"Sources file not found at {path}. Copy sources.example.toml to sources.toml."
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)
    entries = raw.get("source", [])
    return [SourceConfig.model_validate(e) for e in entries]
