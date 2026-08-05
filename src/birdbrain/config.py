"""Central's configuration: the unit's settings plus its own.

The unit-side settings live in :mod:`birdbrain.config_core`; ``AppConfig``
inherits them, so ``from birdbrain.config import AppConfig`` is unchanged and
every field is still on one object.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

# Shared model types live with the unit — SourceConfig is what a capture
# worker is handed, so it belongs on the side that must build standalone.
# Re-exported here because callers already import them from this module, and
# a second definition would give central a SourceConfig that is not the one
# the capture loop type-checks against.
from birdbrain.config_core import (
    DEFAULT_DB_URL,
    LEGACY_DB_PATH,
    OcrConfig,
    SourceConfig,
    UnitConfig,
    resolve_db_url,
)

__all__ = [
    "DEFAULT_DB_URL",
    "LEGACY_DB_PATH",
    "AppConfig",
    "OcrConfig",
    "SourceConfig",
    "UnitConfig",
    "load_sources",
    "resolve_db_url",
]


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
