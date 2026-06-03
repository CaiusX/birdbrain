"""Open-Meteo client + small summary helpers.

Single source of truth for weather lookups across the app. The web's
diurnal popup uses ``fetch_open_meteo_hourly`` + ``weather_at_hour``; the
notes worker uses ``daily_weather_summary`` (per-date, for the daily
brief) and ``recent_weather_summary`` (rolling window, for the site note).

Open-Meteo is free, no key needed, generous free-tier quota
(10K calls/day for non-commercial use). Cached at module scope so all
callers share the same TTL window — important because the notes worker
and web requests can both hit the same coords on the same minute.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# (lat3, lon3, past_days, tz) → (expiry_monotonic, raw_json)
_om_cache: dict[tuple, tuple[float, dict]] = {}
OM_TTL_SECONDS = 3600


def wmo_summary(code: int | None) -> tuple[str, str]:
    """WMO weather code → (icon key, human label). The icon key is one
    of clear/partly/overcast/fog/rain/thunder; the diurnal popup template
    renders an inline SVG per key. ('', '') when the code is unknown."""
    if code is None:
        return ("", "")
    if code == 0:
        return ("clear", "clear")
    if code in (1, 2):
        return ("partly", "partly cloudy")
    if code == 3:
        return ("overcast", "overcast")
    if code in (45, 48):
        return ("fog", "fog")
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return ("rain", "rain")
    if code in (95, 96, 99):
        return ("thunder", "thunderstorm")
    if code in (71, 73, 75, 77, 85, 86):
        return ("overcast", "wintry")
    return ("overcast", "")


def fetch_open_meteo_hourly(
    lat: float, lon: float, past_days: int, tz_name: str
) -> dict | None:
    """Fetch hourly weather from Open-Meteo with module-scoped TTL caching.
    Returns the raw JSON, or None on any network/JSON error."""
    key = (round(lat, 3), round(lon, 3), past_days, tz_name)
    now = time.monotonic()
    cached = _om_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    params = urllib.parse.urlencode(
        {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "past_days": past_days,
            "forecast_days": 1,
            "hourly": (
                "temperature_2m,relative_humidity_2m,"
                "precipitation,wind_speed_10m,cloud_cover,weather_code"
            ),
            "daily": "sunrise,sunset",
            "timezone": tz_name,
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "africam-bird/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            data = json.load(resp)
    except Exception:
        return None
    _om_cache[key] = (now + OM_TTL_SECONDS, data)
    return data


def current_weather_at(lat: float, lon: float, tz_name: str) -> dict | None:
    """Current local-hour weather for the site header.

    Reuses the cached Open-Meteo hourly fetch (past_days=0, forecast_days=1)
    and picks the row whose timestamp matches the current local hour. Returns
    ``{temp, humidity, wind, cloud_cover, code, icon, label, local_time, tz}``
    or None when the upstream call fails / produces no match.
    """
    data = fetch_open_meteo_hourly(lat, lon, 0, tz_name)
    if not data:
        return None
    hourly = (data.get("hourly") or {})
    times = hourly.get("time") or []
    if not times:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    now_local = datetime.now(tz)
    needle = now_local.strftime("%Y-%m-%dT%H:00")
    try:
        idx = times.index(needle)
    except ValueError:
        # Fall back to the most recent past hour in the array.
        idx = None
        for i, t in enumerate(times):
            if isinstance(t, str) and t <= needle:
                idx = i
        if idx is None:
            return None

    def _pick(name: str):
        arr = hourly.get(name) or []
        return arr[idx] if idx < len(arr) else None

    code_raw = _pick("weather_code")
    code = int(code_raw) if code_raw is not None else None
    icon, label = wmo_summary(code)
    return {
        "temp": _pick("temperature_2m"),
        "humidity": _pick("relative_humidity_2m"),
        "wind": _pick("wind_speed_10m"),
        "cloud_cover": _pick("cloud_cover"),
        "code": code,
        "icon": icon,
        "label": label,
        "local_time": now_local.strftime("%H:%M"),
        "tz": tz_name,
    }


def weather_at_hour(data: dict, hour: int) -> dict:
    """Aggregate hourly weather values for one hour-of-day across the
    Open-Meteo response window. Returns ``{}`` if no samples landed on
    the requested hour. Used by the web's diurnal popup."""
    hourly = (data or {}).get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        return {}
    idxs = []
    for i, t in enumerate(times):
        if isinstance(t, str) and len(t) >= 13 and t[10] == "T":
            try:
                if int(t[11:13]) == hour:
                    idxs.append(i)
            except ValueError:
                continue
    if not idxs:
        return {}

    def _gather(name: str) -> list[float]:
        arr = hourly.get(name) or []
        return [arr[i] for i in idxs if i < len(arr) and arr[i] is not None]

    temps = _gather("temperature_2m")
    codes = [int(c) for c in _gather("weather_code") if c is not None]
    wmo_modal = None
    if codes:
        counts: dict[int, int] = {}
        for c in codes:
            counts[c] = counts.get(c, 0) + 1
        wmo_modal = max(counts, key=counts.get)
    icon, label = wmo_summary(wmo_modal)
    return {
        "n_samples": len(idxs),
        "temp_mean": (sum(temps) / len(temps)) if temps else None,
        "wmo_code": wmo_modal,
        "icon": icon,
        "label": label,
    }


# --- Per-prompt summary helpers (used by the notes worker) ---


def _today_local(tz_name: str) -> date:
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def _filter_window(
    hourly: dict, predicate
) -> tuple[list[int], dict[str, list]]:
    """Return (indexes, per-field column lists) for hourly samples whose
    ISO timestamp string passes ``predicate``."""
    times = hourly.get("time") or []
    idxs = [i for i, t in enumerate(times) if isinstance(t, str) and predicate(t)]
    cols: dict[str, list] = {}
    for name in (
        "temperature_2m",
        "relative_humidity_2m",
        "precipitation",
        "wind_speed_10m",
        "cloud_cover",
        "weather_code",
    ):
        arr = hourly.get(name) or []
        cols[name] = [arr[i] for i in idxs if i < len(arr) and arr[i] is not None]
    return idxs, cols


def _modal_wmo(codes: list[float]) -> int | None:
    if not codes:
        return None
    counts: dict[int, int] = {}
    for c in codes:
        c_int = int(c)
        counts[c_int] = counts.get(c_int, 0) + 1
    return max(counts, key=counts.get)


def daily_weather_summary(
    lat: float, lon: float, date_utc: date, tz_name: str
) -> dict | None:
    """Aggregate one local date's weather into a small prompt-ready dict.

    ``date_utc`` is the calendar date keyed by UTC (the daily-brief's
    primary key), but we filter against the local-timezone day from
    Open-Meteo's response so dawn/dusk align with bird activity. For
    most of Southern Africa (UTC+2) the date drift is small but matters
    around midnight.

    Returns None when Open-Meteo doesn't respond or has no rows for the
    date. Caller should handle by omitting the weather field rather
    than failing the whole note.
    """
    today_local = _today_local(tz_name)
    days_back = max(0, (today_local - date_utc).days)
    # Need past_days = days_back + 1 to ensure the target day's local hours
    # are included even if the API returns hours by local clock.
    data = fetch_open_meteo_hourly(
        lat=lat, lon=lon, past_days=days_back + 1, tz_name=tz_name
    )
    if not data:
        return None
    hourly = data.get("hourly") or {}
    iso_prefix = date_utc.isoformat()
    _, cols = _filter_window(hourly, lambda t: t.startswith(iso_prefix))
    if not cols["temperature_2m"] and not cols["precipitation"]:
        return None

    temps = cols["temperature_2m"]
    precip = cols["precipitation"]
    wind = cols["wind_speed_10m"]
    humidity = cols["relative_humidity_2m"]
    cloud = cols["cloud_cover"]
    wmo = _modal_wmo(cols["weather_code"])
    _, label = wmo_summary(wmo)

    # Pick out sunrise/sunset from the daily block for this date.
    daily = data.get("daily") or {}
    sunrise_local = sunset_local = None
    for i, d in enumerate(daily.get("time") or []):
        if d == iso_prefix:
            sr = (daily.get("sunrise") or [None])[i] if i < len(daily.get("sunrise") or []) else None
            ss = (daily.get("sunset") or [None])[i] if i < len(daily.get("sunset") or []) else None
            # Open-Meteo returns "2026-05-24T06:34" — take HH:MM.
            if isinstance(sr, str) and "T" in sr:
                sunrise_local = sr.split("T", 1)[1][:5]
            if isinstance(ss, str) and "T" in ss:
                sunset_local = ss.split("T", 1)[1][:5]
            break

    return {
        "date_local": iso_prefix,
        "timezone": tz_name,
        "temp_min_c": round(min(temps), 1) if temps else None,
        "temp_max_c": round(max(temps), 1) if temps else None,
        "temp_mean_c": round(sum(temps) / len(temps), 1) if temps else None,
        "precipitation_total_mm": round(sum(precip), 1) if precip else 0.0,
        "precipitation_hours": int(sum(1 for p in precip if p > 0.1)),
        "humidity_mean_pct": round(sum(humidity) / len(humidity)) if humidity else None,
        "wind_max_kph": round(max(wind), 1) if wind else None,
        "wind_mean_kph": round(sum(wind) / len(wind), 1) if wind else None,
        "cloud_cover_mean_pct": round(sum(cloud) / len(cloud)) if cloud else None,
        "conditions_modal": label or None,
        "sunrise_local": sunrise_local,
        "sunset_local": sunset_local,
    }


def recent_weather_summary(
    lat: float, lon: float, tz_name: str, lookback_days: int = 7
) -> dict | None:
    """Summarize the last ``lookback_days`` days of weather at this site —
    used for the site-note prompt to give Claude the prevailing conditions
    rather than a single day's snapshot.

    Returns None on fetch failure.
    """
    data = fetch_open_meteo_hourly(
        lat=lat, lon=lon, past_days=lookback_days, tz_name=tz_name
    )
    if not data:
        return None
    hourly = data.get("hourly") or {}
    # Keep everything Open-Meteo returned (it covers past_days + today).
    _, cols = _filter_window(hourly, lambda _t: True)
    if not cols["temperature_2m"]:
        return None

    temps = cols["temperature_2m"]
    precip = cols["precipitation"]
    wind = cols["wind_speed_10m"]
    cloud = cols["cloud_cover"]
    wmo = _modal_wmo(cols["weather_code"])
    _, label = wmo_summary(wmo)

    # Days with measurable rain: count distinct dates whose hourly precip > 0.1mm.
    times = hourly.get("time") or []
    rain_days: set[str] = set()
    for t, p in zip(times, hourly.get("precipitation") or [], strict=False):
        if isinstance(t, str) and p is not None and p > 0.1:
            rain_days.add(t[:10])

    return {
        "window_days": lookback_days,
        "timezone": tz_name,
        "temp_min_c": round(min(temps), 1),
        "temp_max_c": round(max(temps), 1),
        "temp_mean_c": round(sum(temps) / len(temps), 1),
        "total_precipitation_mm": round(sum(precip), 1) if precip else 0.0,
        "days_with_rain": len(rain_days),
        "wind_mean_kph": round(sum(wind) / len(wind), 1) if wind else None,
        "wind_max_kph": round(max(wind), 1) if wind else None,
        "cloud_cover_mean_pct": round(sum(cloud) / len(cloud)) if cloud else None,
        "dominant_condition": label or None,
        "sample_hours": len(temps),
    }
