"""Background hourly weather archiver.

Single daemon thread. Every ``weather_tick_seconds`` it walks the union of
(active sources with lat/lon) ∪ (sites.toml entries), dedups by 3dp
coordinate, and pulls hourly Open-Meteo observations into the
``weather_observations`` table. A fresh coord gets a ~92-day backfill on
its first scan; steady-state ticks request only the last ~2 days so very
recent hours can be revised as observed values stabilize.

The motivation: the dashboard used to block hour-drill clicks on a live
Open-Meteo call (5–15 s per click) — see commit 01f33af. Persisting the
data locally means UI reads are pure SQL, and weather becomes a
queryable dimension alongside detections (e.g. "what called when it
rained?") rather than a fleeting cache-hit on the API.

Stays dormant if ``weather_archive_enabled`` is False; failures inside
the tick are logged and swallowed so a bad upstream response never bubbles
back into the supervisor loop.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from africam.config import AppConfig, SourceConfig
from africam.logging import get_logger
from africam.sites import Site
from africam.storage import Database
from africam.weather import fetch_open_meteo_hourly

log = get_logger(__name__)


def _coverage_coords(
    static_sources: Iterable[SourceConfig],
    sites: dict[str, Site],
    db: Database,
) -> list[tuple[float, float, str, str]]:
    """Return [(lat, lon, tz_name, label)] deduped by 3dp coord.

    ``label`` is purely for logging — when the same coord is shared by a
    source and a site we keep the source name (it's the more operationally
    meaningful identifier). Picks the source's tz over UTC when both exist.
    """
    seen: dict[tuple[int, int], tuple[float, float, str, str]] = {}
    disabled = db.list_disabled_source_names()

    def _add(lat: float, lon: float, tz_name: str, label: str) -> None:
        key = (int(round(lat * 1000)), int(round(lon * 1000)))
        existing = seen.get(key)
        if existing is None:
            seen[key] = (lat, lon, tz_name, label)
        elif existing[2] == "UTC" and tz_name != "UTC":
            # Prefer a real tz over UTC if a later entry has one.
            seen[key] = (existing[0], existing[1], tz_name, existing[3])

    for cfg in static_sources:
        if cfg.name in disabled:
            continue
        if cfg.lat is None or cfg.lon is None:
            continue
        _add(cfg.lat, cfg.lon, cfg.timezone or "UTC", f"src:{cfg.name}")

    for row in db.list_runtime_sources():
        if row.deleted_at is not None or row.name in disabled:
            continue
        if row.lat is None or row.lon is None:
            continue
        _add(row.lat, row.lon, row.timezone or "UTC", f"src:{row.name}")

    for site in sites.values():
        _add(site.lat, site.lon, "UTC", f"site:{site.name}")

    return list(seen.values())


def _rows_from_response(data: dict) -> list[dict]:
    """Convert a single Open-Meteo response into per-hour rows suitable for
    ``Database.upsert_weather_observations``. Each row's timestamp is
    converted to UTC using the response's ``utc_offset_seconds`` so storage
    is timezone-agnostic.
    """
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return []
    offset = timedelta(seconds=int(data.get("utc_offset_seconds") or 0))
    temp = hourly.get("temperature_2m") or []
    humidity = hourly.get("relative_humidity_2m") or []
    precip = hourly.get("precipitation") or []
    wind = hourly.get("wind_speed_10m") or []
    cloud = hourly.get("cloud_cover") or []
    code = hourly.get("weather_code") or []

    def _at(arr: list, i: int):
        return arr[i] if i < len(arr) else None

    out: list[dict] = []
    for i, t_str in enumerate(times):
        if not isinstance(t_str, str) or "T" not in t_str:
            continue
        try:
            local_dt = datetime.fromisoformat(t_str)
        except ValueError:
            continue
        utc_dt = (local_dt - offset).replace(tzinfo=UTC)
        c_raw = _at(code, i)
        out.append({
            "observed_at_utc": utc_dt,
            "temp_c": _at(temp, i),
            "humidity_pct": _at(humidity, i),
            "precipitation_mm": _at(precip, i),
            "wind_kph": _at(wind, i),
            "cloud_cover_pct": _at(cloud, i),
            "wmo_code": int(c_raw) if c_raw is not None else None,
        })
    return out


def _tick_once(
    db: Database,
    cfg: AppConfig,
    coords: list[tuple[float, float, str, str]],
) -> None:
    """One pass over every coord — backfill if empty, otherwise revise the
    last couple of days. Logs per-coord so a single bad fetch is visible."""
    for lat, lon, tz_name, label in coords:
        latest = db.latest_weather_observation_at(lat, lon)
        if latest is None:
            past_days = cfg.weather_backfill_days
            mode = "backfill"
        else:
            past_days = 2
            mode = "revise"

        data = fetch_open_meteo_hourly(lat, lon, past_days, tz_name)
        if not data:
            log.warning(
                "weather.fetch_failed",
                label=label, lat=lat, lon=lon, past_days=past_days,
            )
            continue

        rows = _rows_from_response(data)
        if not rows:
            log.warning("weather.empty_response", label=label, lat=lat, lon=lon)
            continue

        inserted, revised = db.upsert_weather_observations(lat, lon, rows)
        log.info(
            "weather.tick",
            label=label,
            mode=mode,
            past_days=past_days,
            n_hours=len(rows),
            inserted=inserted,
            revised=revised,
        )


def _worker_loop(
    db: Database,
    cfg: AppConfig,
    static_sources: list[SourceConfig],
    sites: dict[str, Site],
) -> None:
    log.info(
        "weather.worker_started",
        tick_s=cfg.weather_tick_seconds,
        backfill_days=cfg.weather_backfill_days,
    )
    # Jitter so the worker doesn't fire the instant the pipeline boots —
    # mirrors the notes-worker behaviour and avoids a thundering herd of
    # Open-Meteo calls if multiple processes restart at once.
    time.sleep(min(60, cfg.weather_tick_seconds))

    while True:
        try:
            coords = _coverage_coords(static_sources, sites, db)
            if coords:
                _tick_once(db, cfg, coords)
            else:
                log.info("weather.no_coords")
        except Exception as e:
            log.warning("weather.tick_failed", error=str(e)[:300])
        time.sleep(cfg.weather_tick_seconds)


def start_weather_worker(
    db: Database,
    cfg: AppConfig,
    static_sources: list[SourceConfig],
    sites: dict[str, Site],
) -> threading.Thread | None:
    """Spawn the weather archiver as a daemon thread. Returns the thread, or
    None when disabled by config. Safe to call when no sources have coords —
    the loop just logs and ticks."""
    if not cfg.weather_archive_enabled:
        log.info("weather.disabled", reason="cfg.weather_archive_enabled=False")
        return None
    t = threading.Thread(
        target=_worker_loop,
        args=(db, cfg, list(static_sources), sites),
        name="weather-worker",
        daemon=True,
    )
    t.start()
    return t
