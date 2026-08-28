from __future__ import annotations

import asyncio
import functools
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from markupsafe import Markup, escape
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Non-blocking advisory file lock for single-leader election among uvicorn
# workers (only one runs the media-sweeper). fcntl is Unix-only and msvcrt is
# Windows-only, so import both defensively; if neither is present we assume a
# sole process and let it lead.
try:
    import fcntl  # Unix
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt  # Windows
except ImportError:  # pragma: no cover - Unix
    msvcrt = None


def _acquire_singleton_lock(fh) -> bool:
    """Take a process-exclusive, non-blocking advisory lock on open file ``fh``.

    Returns True if acquired, False if another process already holds it. On a
    platform exposing no lock primitive, assume a single process and return True.
    """
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


@functools.lru_cache(maxsize=64)
def _zone_info(tz_name: str) -> ZoneInfo:
    """Resolve an IANA timezone name to a ZoneInfo, falling back to UTC for
    unknown names. Cached because most call sites loop over many rows that
    share a small set of timezones (typically just Africa/Johannesburg)."""
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


@functools.lru_cache(maxsize=1)
def _birdnet_catalog() -> list[dict[str, str]]:
    """The full BirdNET global label set (~6.5k entries), read once from the
    birdnetlib labels file. Each line is ``Scientific_Common``. Returns
    ``{"scientific", "common"}`` dicts sorted by common name, deduped by common
    name (the suggestion box inserts the common name). Importing ``LABEL_PATH``
    does not load the TFLite model, so this is cheap. Empty list on any error
    so the caller degrades to the server-rendered detected-species options."""
    try:
        from birdnetlib.analyzer import LABEL_PATH
    except Exception:  # pragma: no cover - birdnetlib import/layout drift
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        with open(LABEL_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                sci, _, common = line.partition("_")
                common = common or sci
                key = common.casefold()
                if key in seen:
                    continue
                seen.add(key)
                out.append({"scientific": sci, "common": common})
    except OSError:
        return []
    out.sort(key=lambda r: r["common"].casefold())
    return out

import squarify
import structlog
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Integer, and_, case, desc, exists, func, or_, select, text
from starlette.middleware.sessions import SessionMiddleware

from birdbrain import sandbox
from birdbrain import weather as weather_module
from birdbrain.config import AppConfig, SourceConfig, load_sources
from birdbrain.enroll import EnrollBody, enroll
from birdbrain.host import host_metrics
from birdbrain.ingest import (
    SCHEMA_CONFLICT_STATUS,
    SUPPORTED_SCHEMAS,
    IngestBody,
    UnsupportedSchemaError,
    hash_token,
    ingest_batch,
)
from birdbrain.site_resolver import state_to_resolved
from birdbrain.sites import Site, load_sites
from birdbrain.storage.db import ALL_SITES_SENTINEL
from birdbrain.storage import (
    DailyBriefRow,
    Database,
    DetectionRow,
    DetectionScoreRow,
    SiteNoteRow,
    SpeciesNoteRow,
    UserRow,
    WorkerHeartbeatRow,
)
from birdbrain.web import auth as auth_mod

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _portable_strftime(dt: datetime, fmt: str) -> str:
    """strftime that honours glibc's ``%-`` (strip-leading-zero) codes on every
    platform. The Windows C runtime rejects ``%-d`` with "Invalid format string"
    and spells the same thing ``%#d``, so translate there. Templates and code are
    written for the Linux/Pi deploy target; this keeps them rendering on Windows."""
    if sys.platform == "win32":
        fmt = fmt.replace("%-", "%#")
    return dt.strftime(fmt)


def _localtime(dt: datetime, tz_name: str | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Render a UTC datetime in the given IANA timezone. SQLite hands us back
    naive datetimes (it strips tz on read), so re-attach UTC first."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    try:
        tz = ZoneInfo(tz_name) if tz_name else UTC
    except ZoneInfoNotFoundError:
        tz = UTC
    return _portable_strftime(dt.astimezone(tz), fmt)


def _tz_abbr(tz_name: str | None) -> str:
    """Short label like SAST/UTC/EST for a column header. Datetime-derived so
    it stays correct across DST."""
    if not tz_name:
        return "UTC"
    try:
        return datetime.now(ZoneInfo(tz_name)).tzname() or tz_name
    except ZoneInfoNotFoundError:
        return tz_name


def _salvage_truncated_json(raw: str) -> dict | None:
    """When the model's JSON output was cut off mid-array (max_tokens hit),
    walk back from the end to the last }, ] or "," and synthesise the closing
    brackets/braces. String-aware so a } inside a string literal doesn't
    confuse the counter. Returns the parsed dict on success, None otherwise."""
    n = len(raw)
    # Don't try harder than ~600 chars back; truncation is normally at the
    # very end of a long output.
    for end in range(n - 1, max(0, n - 600), -1):
        if raw[end] not in "},]":
            continue
        candidate = raw[: end + 1]
        depth_curly = depth_square = 0
        in_str = esc = False
        bad = False
        for c in candidate:
            if esc:
                esc = False
                continue
            if in_str:
                if c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth_curly += 1
            elif c == "}":
                depth_curly -= 1
            elif c == "[":
                depth_square += 1
            elif c == "]":
                depth_square -= 1
            if depth_curly < 0 or depth_square < 0:
                bad = True
                break
        if bad or in_str:
            continue
        # Trim a dangling comma that a truncated array almost always leaves.
        trimmed = candidate.rstrip()
        if trimmed.endswith(","):
            trimmed = trimmed[:-1]
        suffix = "]" * depth_square + "}" * depth_curly
        try:
            data = json.loads(trimmed + suffix)
        except (ValueError, TypeError):
            continue
        return data if isinstance(data, dict) else None
    return None


# Brief bullets that carry no real content — the model emits these for sites
# that were silent in the window. Matched at the start of a stripped bullet.
_NO_DATA_RE = re.compile(
    r"^(no data|no detections|no activity|none|n/?a|nothing|silent)\b",
    re.IGNORECASE,
)


def _parse_brief(text: str | None) -> dict:
    """Parse a daily brief into {overall, sites:[{site, bullets}]}. New briefs
    are JSON (optionally fenced); legacy briefs are a plain paragraph, returned
    as the overall text with no per-site sections. Truncated JSON (model hit
    max_tokens) is salvaged by closing trailing brackets."""
    if not text:
        return {"overall": "", "sites": []}
    raw = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.S | re.I)
    if fence:
        raw = fence.group(1).strip()
    elif raw.startswith("```"):
        # Opening fence with no closing one — truncation cut it off. Strip
        # the opening and proceed against the body.
        raw = re.sub(r"^```(?:json)?\s*", "", raw, count=1)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        data = _salvage_truncated_json(raw)
    if not isinstance(data, dict):
        return {"overall": text.strip(), "sites": []}
    sites: list[dict] = []
    seen: set[str] = set()
    for s in data.get("sites") or []:
        if not isinstance(s, dict):
            continue
        name = str(s.get("site") or "").strip()
        # Drop placeholder "no data" bullets the model emits for silent sites
        # (e.g. Stony Point's "No data provided") so an empty site doesn't
        # render a hollow card.
        bullets = [
            b
            for b in (str(x).strip() for x in (s.get("bullets") or []))
            if b and not _NO_DATA_RE.match(b)
        ]
        key = name.lower()
        # Dedupe by site name — the model occasionally lists the same site
        # twice (seen with Mara River).
        if name and bullets and key not in seen:
            seen.add(key)
            sites.append({"site": name, "bullets": bullets})
    return {"overall": str(data.get("overall") or "").strip(), "sites": sites}


TEMPLATES.env.filters["localtime"] = _localtime
TEMPLATES.env.filters["strftime"] = _portable_strftime
TEMPLATES.env.filters["tz_abbr"] = _tz_abbr
TEMPLATES.env.filters["parse_brief"] = _parse_brief


# Advisory-flock fds held for the process lifetime, so background singletons
# (the media sweeper) run in only one uvicorn worker when there are several.
_SINGLETON_LOCKS: list = []


# Per-source colour palette for the /species treemap. Hand-picked to evoke
# each site's biome rather than scraped from a logo (africam.com doesn't
# carry distinct per-lodge logos — checked). Any source not in this dict
# falls back to the template's default emerald.
SOURCE_COLORS: dict[str, str] = {
    "Tembe":               "#059669",  # KZN coastal sand forest — emerald-600
    "Olifants (Naledi)":   "#84cc16",  # Greater Kruger bushveld — lime-500
    "Timbavati":           "#b91c1c",  # Lowveld red soils       — red-700
    "Twin Pan":            "#a8a29e",  # Botswana pan / grass    — stone-400
    "Safarihoek":          "#ea580c",  # Etosha Heights, arid    — orange-600
    "Tau Game Lodge":      "#b45309",  # Madikwe bushveld        — amber-700
    "Tortilis Camp":       "#eab308",  # Amboseli golden grass   — yellow-500
    "Mara River":          "#0891b2",  # Mara Triangle, riverine — cyan-600
    "Mpala Watering Hole": "#4d7c0f",  # Laikipia acacia plateau — lime-700
    "Stony Point":         "#1d4ed8",  # Coastal Atlantic colony — blue-700
    "Elephant Pan":        "#7c2d12",  # Tuli rusty riparian     — orange-900
    "Kalahari":            "#92400e",  # Kalahari red dune sand  — amber-800
    "Namib Desert":        "#fdba74",  # Namib pale dune sand    — orange-300
    "Okaukuejo":           "#d6d3d1",  # Etosha white salt pan   — stone-300
    # Majete (Malawi, lower Shire) — six cams share one map point, so give each
    # a well-separated hue so they're tellable apart when the dot fans out.
    "Majete Cam 1":          "#14b8a6",  # teal-500
    "Majete Cam 2":          "#8b5cf6",  # violet-500
    "Majete Cam 3":          "#ec4899",  # pink-500
    "Majete Cam 4":          "#f59e0b",  # amber-500
    "Majete Cam 5":          "#0ea5e9",  # sky-500
    "Majete Thawale Lodge":  "#f43f5e",  # rose-500
}


# Short location + biome label per site, surfaced in the dashboard map's
# site-summary popup. Mirrors the SOURCE_COLORS biome notes above. Sites not
# listed (e.g. the garden mic) simply show no biome line.
SOURCE_BIOME: dict[str, str] = {
    "Tembe":               "KZN coastal sand forest",
    "Olifants (Naledi)":   "Greater Kruger bushveld",
    "Timbavati":           "Lowveld red soils",
    "Twin Pan":            "Botswana pan & grassland",
    "Safarihoek":          "Etosha Heights — arid",
    "Tau Game Lodge":      "Madikwe bushveld",
    "Tortilis Camp":       "Amboseli golden grass",
    "Mara River":          "Mara Triangle — riverine",
    "Mpala Watering Hole": "Laikipia acacia plateau",
    "Stony Point":         "Atlantic penguin colony",
    "Elephant Pan":        "Tuli rusty riparian",
    "Kalahari":            "Kalahari red dune sand",
    "Namib Desert":        "Namib pale dune sand",
    "Okaukuejo":           "Etosha white salt pan",
    "Majete Cam 1":          "Majete, Malawi — lower Shire miombo",
    "Majete Cam 2":          "Majete, Malawi — lower Shire miombo",
    "Majete Cam 3":          "Majete, Malawi — lower Shire miombo",
    "Majete Cam 4":          "Majete, Malawi — lower Shire miombo",
    "Majete Cam 5":          "Majete, Malawi — lower Shire miombo",
    "Majete Thawale Lodge":  "Majete, Malawi — Thawale camp waterhole",
}


def _site_color(name: str) -> str:
    """Per-site colour for inline text (clickable site names), echoing the map
    dots. Several palette hues are deliberately dark for the white-stroked map
    dots (Elephant Pan rust, Kalahari amber, Stony Point blue) and would be
    unreadable as text on the near-black UI, so dark hues are lightened toward
    white until legible — the hue stays, only the brightness lifts. Unknown
    sites (e.g. the garden mic) fall back to emerald-400."""
    hex_ = SOURCE_COLORS.get(name)
    if not hex_:
        return "#34d399"
    r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b  # 0..255
    if lum < 150:
        t = min(0.62, (150 - lum) / 255 * 1.3)  # darker → mix more toward white
        r, g, b = (round(c + (255 - c) * t) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


TEMPLATES.env.globals["site_color"] = _site_color


_SPECIES_LINK_TTL = 300.0  # seconds; the detected-species set changes slowly

# The front page's all-time and 30-day roll-ups: how many species each site has
# ever logged, when each first came online, and the 30-day species overlap that
# draws the map's connection web. Each is a full scan of `detections` (2.4s,
# 0.4s and 1.4s respectively on 1.58M rows) and every one was recomputed on
# every homepage load, which is most of why `/` took ~11s. They move on the
# scale of days — a site's all-time species count changes when something new
# turns up — so serving them a few minutes stale costs nothing a reader would
# notice. Per worker, so the real recompute rate is this divided by the worker
# count.
_FRONT_SLOW_TTL = 300.0

# "Running but deaf": a source whose worker is healthy and whose ffmpeg is
# streaming, but which has stopped producing detections. JHB - Hyde Park sat
# like that from 2026-08-25 to 08-28 — the USB mic stopped delivering samples,
# PipeWire still reported the node "running", the supervisor kept dutifully
# respawning a worker that had nothing to hear, and every panel stayed green.
#
# The threshold has to be per-source. Twin Pan's worst normal silence is ~34
# minutes; Hyde Park's is ~6.6h of overnight quiet. Any single number either
# cries wolf at every site each night or takes days to notice a dead one. So
# each source is judged against its own habits.
#
# The statistic is the *median across days of that day's longest silence*. The
# daily maximum absorbs the legitimate nightly lull; taking the median across
# days stops an outage from teaching the alert that outages are normal. That
# second half matters more than it sounds — Hyde Park's plain 14-day maximum
# gap was 58.5h, which is just its own failure folded back into its baseline.
_SILENCE_BASELINE_DAYS = 14
_SILENCE_BASELINE_TTL = 3600.0    # habits move on the scale of days
_SILENCE_WATCH_TTL = 60.0         # the live half; /admin re-polls every 30s
# Tuned by replaying the fleet's last 14 days at 6-hourly checkpoints, scoring
# how fast the real Hyde Park outage was caught against how often anything else
# fired. The knee is at 2.5 — a third of the noise for six more hours of
# latency, which is a good trade when the failure being caught lasts days:
#
#   factor   Hyde Park fires   other fires   caught the outage after
#     1.5          7               10                14h
#     2.0          7                6                14h
#     2.5          6                2                20h   <- chosen
#     3.0          5                2                26h
#     4.0          4                1                32h
#
# The "other fires" at 2.0 were mostly long nights on YouTube sites, not faults.
_SILENCE_FACTOR = 2.5             # multiple of a source's own worst normal day
_SILENCE_FLOOR_S = 3 * 3600.0     # never cry wolf below this, however chatty
_SILENCE_MIN_DAYS = 3             # too new to have habits worth judging


def _make_species_linkifier(db):
    """Build a Jinja filter that turns detected-species common names in note
    prose into neutral links to the species page. The matcher — one compiled
    alternation regex over the current species, longest-name-first so multi-word
    names beat their substrings — is built once and refreshed at most every few
    minutes, so per-render cost is a single regex pass over a short string.
    Only the first occurrence of each name is linked (avoids a wall of links),
    and only species we've actually detected (so the link lands on a real page).
    Output is safe markup: matched names become anchors, everything else is
    HTML-escaped."""
    cache: dict = {"ts": -1.0, "pattern": None, "lookup": {}}
    lock = threading.Lock()

    def _rebuild() -> None:
        lookup: dict[str, str] = {}
        names: list[str] = []
        for sci, common in db.list_detected_species():
            if not common:
                continue
            key = common.casefold()
            if key not in lookup:
                lookup[key] = sci
                names.append(common)
        names.sort(key=len, reverse=True)
        pattern = (
            re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
            if names else None
        )
        cache.update(pattern=pattern, lookup=lookup, ts=time.monotonic())

    def linkify(text):
        if not text:
            return text
        if cache["pattern"] is None or time.monotonic() - cache["ts"] > _SPECIES_LINK_TTL:
            with lock:
                if (cache["pattern"] is None
                        or time.monotonic() - cache["ts"] > _SPECIES_LINK_TTL):
                    _rebuild()
        pattern = cache["pattern"]
        if pattern is None:
            return text
        lookup = cache["lookup"]
        out: list = []
        seen: set[str] = set()
        last = 0
        for m in pattern.finditer(text):
            key = m.group(1).casefold()
            sci = lookup.get(key)
            if not sci or key in seen:
                continue
            seen.add(key)
            out.append(escape(text[last:m.start()]))
            href = "/species/" + urllib.parse.quote(sci)
            out.append(Markup(
                '<a href="{}" class="font-semibold underline decoration-dotted '
                'decoration-zinc-600 underline-offset-2 hover:text-zinc-100 '
                'hover:decoration-zinc-400">{}</a>'
            ).format(href, m.group(1)))
            last = m.end()
        out.append(escape(text[last:]))
        return Markup("").join(out)

    return linkify


def _solar_event_utc_hours(d: date, lat: float, lon: float,
                           altitude_deg: float, morning: bool) -> float | None:
    """Sunrise-equation solver. Returns the UTC fractional hour on date ``d``
    when the sun crosses ``altitude_deg`` (degrees above horizon; negative
    for twilight). ``morning=True`` for the ascending crossing, False for
    descending. Returns None if the crossing doesn't happen that day (polar
    summer/winter). Good to ~1 minute — plenty for hour-of-day shading."""
    n = (d - date(d.year, 1, 1)).days + 1
    lng_hour = lon / 15.0
    t = n + ((6.0 if morning else 18.0) - lng_hour) / 24.0
    M = 0.9856 * t - 3.289
    L = (M
         + 1.916 * math.sin(math.radians(M))
         + 0.020 * math.sin(math.radians(2 * M))
         + 282.634) % 360
    RA = math.degrees(math.atan(0.91764 * math.tan(math.radians(L)))) % 360
    # Force RA into the same quadrant as L.
    L_q = (L // 90) * 90
    RA_q = (RA // 90) * 90
    RA = (RA + (L_q - RA_q)) / 15.0  # hours
    sinDec = 0.39782 * math.sin(math.radians(L))
    cosDec = math.cos(math.asin(sinDec))
    cosH = (
        (math.sin(math.radians(altitude_deg)) - sinDec * math.sin(math.radians(lat)))
        / (cosDec * math.cos(math.radians(lat)))
    )
    if cosH > 1 or cosH < -1:
        return None
    H = (360 - math.degrees(math.acos(cosH))) if morning else math.degrees(math.acos(cosH))
    H = H / 15.0
    T = H + RA - 0.06571 * t - 6.622
    return (T - lng_hour) % 24


def _solar_local_hour(d: date, lat: float, lon: float, tz: ZoneInfo,
                      altitude_deg: float, morning: bool) -> float | None:
    """Convenience: ``_solar_event_utc_hours`` projected into ``tz`` as a
    fractional hour-of-day in 0–24."""
    ut = _solar_event_utc_hours(d, lat, lon, altitude_deg, morning)
    if ut is None:
        return None
    base = datetime(d.year, d.month, d.day, tzinfo=UTC)
    local = (base + timedelta(hours=ut)).astimezone(tz)
    return local.hour + local.minute / 60.0 + local.second / 3600.0


def _solar_bands(d: date, lat: float, lon: float, tz: ZoneInfo) -> list[dict]:
    """Six time-of-day bands as fractional hours in ``tz`` for the given date.
    The night band wraps midnight, so it's emitted as two rects (start-of-day
    + end-of-day). Returns ``[]`` if any solar event can't be computed."""
    sr = _solar_local_hour(d, lat, lon, tz, -0.833, morning=True)   # sunrise
    ss = _solar_local_hour(d, lat, lon, tz, -0.833, morning=False)  # sunset
    # Nautical twilight (sun at -12°) for the outer dawn/dusk edge — civil
    # (-6°) is only ~25 min wide in the tropics, which the glow band eats
    # almost entirely. Nautical gives the rose tint a visible footprint.
    nt_morning = _solar_local_hour(d, lat, lon, tz, -12.0, morning=True)
    nt_evening = _solar_local_hour(d, lat, lon, tz, -12.0, morning=False)
    if None in (sr, ss, nt_morning, nt_evening):
        return []
    # Sunrise/sunset bands sit on the *night side* of the sun event: the
    # orange horizon glow only happens while the sun is at/below the horizon.
    # Once the sun is up it's day, not "sunrise". Dawn/dusk fill the rest of
    # nautical twilight (sun -12° to glow band) with a cooler rose tint.
    glow = 0.33  # ~20 min — the narrow band where the horizon goes peak orange
    bands = [
        # night wraps: two rects
        {"name": "night",   "start": 0.0, "end": nt_morning},
        {"name": "dawn",    "start": nt_morning,    "end": sr - glow},
        {"name": "sunrise", "start": sr - glow,     "end": sr},
        {"name": "day",     "start": sr,            "end": ss},
        {"name": "sunset",  "start": ss,            "end": ss + glow},
        {"name": "dusk",    "start": ss + glow,     "end": nt_evening},
        {"name": "night",   "start": nt_evening, "end": 24.0},
    ]
    # Drop zero/negative-width bands. Near the equator the glow band can be
    # wider than the twilight period, which would push dawn/dusk negative.
    return [b for b in bands if b["end"] > b["start"]]

# Captures the 11-char YouTube video id from /watch?v=, youtu.be/ or /embed/ URLs.
_YT_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")

# Default duration of a manual site override before the resolver clears it.
DEFAULT_OVERRIDE_MINUTES = 60


def _youtube_video_id(url: str) -> str | None:
    m = _YT_VIDEO_ID.search(url)
    return m.group(1) if m else None


@dataclass
class LiveTile:
    name: str
    kind: str
    url: str
    video_id: str | None
    multisite: bool
    is_runtime: bool = False

    @property
    def embed_url(self) -> str | None:
        if self.kind == "youtube" and self.video_id:
            return (
                f"https://www.youtube.com/embed/{self.video_id}"
                "?autoplay=1&mute=1&controls=1&rel=0"
            )
        return None


def _build_tiles(sources: list[SourceConfig]) -> list[LiveTile]:
    return [
        LiveTile(
            name=s.name,
            kind=s.kind,
            url=s.url,
            video_id=_youtube_video_id(s.url) if s.kind == "youtube" else None,
            multisite=s.multisite,
        )
        for s in sources
    ]


# Module-level lazy detector for the /reanalyze endpoint. BirdNET's TF model
# takes ~5 s to load and ~150 MB of RAM, so don't pay that cost at web-app
# startup — only on first manual re-analysis request. The pipeline process
# has its own independent detector instance; this one only services the UI.
_reanalyze_lock = threading.Lock()
_reanalyze_state: dict = {"detector": None}

log = structlog.get_logger("birdbrain.web")


def _get_reanalyze_detector():
    if _reanalyze_state["detector"] is None:
        with _reanalyze_lock:
            if _reanalyze_state["detector"] is None:
                from birdbrain.detector.birdnet import BirdNetDetector
                _reanalyze_state["detector"] = BirdNetDetector()
    return _reanalyze_state["detector"]


# Crawl policy. Served from a route rather than a static file so it cannot
# silently 404 the way it did before (116 crawler probes/day answered with a
# 404, which leaves crawlers to make their own rules).
#
# Measured 2026-07-31 over 24h: 6007 requests, of which ~1900 were page renders
# and only 17 were real browser visits (/api/track fires from JS, so it counts
# humans). Roughly 99% of page traffic was automated, and it was pulling
# spectrogram images too — 503 of those in a day.
#
# Deliberately NOT blocking /species/<name>. Those are the site's actual content
# and the reason to be indexed at all; throttling real search engines to save CPU
# would trade away the whole point of a public site. Instead: slow everyone down,
# close off the paths that are expensive or private, and ban outright the SEO
# scrapers that generate load with no upstream benefit — DotBot and an
# Azure-hosted crawler were the two actually hammering us.
ROBOTS_TXT = """\
User-agent: *
Crawl-delay: 10
Disallow: /admin
Disallow: /login
Disallow: /api/
Disallow: /ingest/
Disallow: /partials/
Disallow: /clips/
Disallow: /spectrograms/

# Commercial SEO/AI scrapers: all cost, no readers.
User-agent: DotBot
Disallow: /

User-agent: SemrushBot
Disallow: /

User-agent: AhrefsBot
Disallow: /

User-agent: MJ12bot
Disallow: /

User-agent: DataForSeoBot
Disallow: /

User-agent: PetalBot
Disallow: /

User-agent: Bytespider
Disallow: /

User-agent: GPTBot
Disallow: /

User-agent: CCBot
Disallow: /
"""


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or AppConfig()
    db = Database(cfg.db_url)
    clips_root = cfg.clips_dir.resolve()

    # One-time migration: the legacy single global label per detection becomes
    # the "operator" user's score (per-user model). Idempotent; guarded by an
    # app_settings flag so it runs once. The operator account is created here
    # with a password from BIRDBRAIN_OPERATOR_PASSWORD, else an unusable hash that
    # must be set via `birdbrain set-password operator`.
    if db.get_setting("scores_backfilled") != "1":
        if not db.operator_exists():
            _op_pw = os.environ.get("BIRDBRAIN_OPERATOR_PASSWORD")
            db.set_user_password(
                "operator",
                auth_mod.hash_password(_op_pw) if _op_pw else auth_mod.UNUSABLE_PASSWORD,
                create_role="operator",
            )
            if not _op_pw:
                log.warning(
                    "operator_account_created_without_password",
                    hint="run: birdbrain set-password operator",
                )
        n = db.migrate_global_labels_to_scores("operator")
        db.set_setting("scores_backfilled", "1")
        log.info("scores_backfilled", migrated=n)

    # Linkify species names in note/brief prose → species pages (cached matcher).
    TEMPLATES.env.filters["linkify_species"] = _make_species_linkifier(db)

    try:
        static_sources = load_sources(cfg.sources_file)
    except FileNotFoundError:
        static_sources = []
    sites: dict[str, Site] = load_sites(cfg.sites_file)

    app = FastAPI(title="BirdBrain", version="0.1.0")
    app.state.db = db
    app.state.clips_root = clips_root
    app.state.sites = sites
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    # Public-tunnel gate: Cloudflare attaches CF-Connecting-IP on every proxied
    # request, so its presence identifies traffic that came in via the public
    # Cloudflare tunnel — host-agnostic, so any public hostname (birdbrain.co.za,
    # birds.vcexl.com, …) is gated the same way, vs LAN/localhost on the Pi. For
    # public visitors we hide /admin and refuse all mutating verbs — keeps the
    # dashboard read-only without putting a login in front of the whole site.
    _PUBLIC_BLOCKED_PREFIXES = ("/admin",)
    _PUBLIC_BLOCKED_METHODS = {"POST", "PUT", "DELETE", "PATCH"}
    # Auth flows that must work over the public tunnel so testers can register
    # / sign in (signup itself is gated by the invite code in the handler).
    _AUTH_ALLOWED_PATHS = ("/login", "/signup", "/auth/signup", "/auth/login", "/auth/logout")
    # TBB ingest + enroll are mutating routes allowed over the public tunnel for
    # anonymous units — ingest enforces per-unit bearer-token auth, enroll a
    # one-time claim code (tbb-architecture.md §8). Both rate-limited + capped.
    _PUBLIC_ALLOWED_PREFIXES = ("/ingest/", "/enroll", "/api/track")

    @app.middleware("http")
    async def no_store_html(request: Request, call_next):
        """Stop browsers caching page HTML. Without this, a template change
        deployed to the Pi wouldn't show until the user hard-reloaded — the
        browser happily served a stale copy (this bit us repeatedly on /admin).
        Only HTML is affected; spectrograms/clips/static stay cacheable."""
        response = await call_next(request)
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def restrict_public(request: Request, call_next):
        is_public = request.headers.get("cf-connecting-ip") is not None
        request.state.is_public = is_public
        # Resolve the logged-in tester from the signed session cookie (set by
        # SessionMiddleware, which is outermost). One indexed lookup, only when
        # a session exists — anonymous public traffic pays nothing.
        uid = request.session.get("uid")
        request.state.user = db.get_user_by_id(uid) if uid else None
        if is_public:
            path = request.url.path
            if path.startswith(_PUBLIC_BLOCKED_PREFIXES):
                return Response(status_code=404)          # /admin → LAN only, always
            if any(path.startswith(p) for p in _PUBLIC_ALLOWED_PREFIXES):
                pass                                       # TBB ingest/enroll (token/code-gated)
            elif path in _AUTH_ALLOWED_PATHS or path.startswith("/auth/"):
                pass                                       # sign up / log in over the tunnel
            elif (
                request.method in _PUBLIC_BLOCKED_METHODS
                and request.state.user is None
            ):
                return Response(status_code=404)           # anon public stays read-only
        return await call_next(request)

    # SessionMiddleware must wrap the above so request.session is populated
    # before restrict_public reads it. add_middleware inserts at the front of
    # the stack, so adding it AFTER the @app.middleware decorators makes it the
    # outermost layer. https_only=False so the cookie works on both the https
    # tunnel and plain-http LAN; Lax is fine for our top-level form posts.
    _secret = cfg.secret_key or auth_mod.get_or_create_secret_key(db)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_secret,
        session_cookie="bb_session",
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 24 * 30,
    )

    def _resolve_invite_code() -> str | None:
        """Invite code from config/env, else the app_settings fallback."""
        return cfg.invite_code or db.get_setting("invite_code")

    def require_user(request: Request) -> UserRow:
        """Return the logged-in user or raise 401. Use on scoring endpoints."""
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(401, "login required")
        return user

    # --- Tester accounts: login / signup / logout ---
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        if getattr(request.state, "user", None) is not None:
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.get("/signup", response_class=HTMLResponse)
    def signup_page(request: Request) -> Response:
        if getattr(request.state, "user", None) is not None:
            return RedirectResponse("/", status_code=303)
        return TEMPLATES.TemplateResponse(
            request, "signup.html",
            {"error": None, "signups_open": _resolve_invite_code() is not None},
        )

    @app.post("/auth/signup")
    async def auth_signup(request: Request) -> Response:
        form = await request.form()
        username = auth_mod.normalize_username(form.get("username") or "")
        password = form.get("password") or ""
        invite = (form.get("invite_code") or "").strip()
        expected = _resolve_invite_code()

        def _fail(msg: str) -> Response:
            return TEMPLATES.TemplateResponse(
                request, "signup.html",
                {"error": msg, "signups_open": expected is not None},
                status_code=400,
            )

        if expected is None:
            return _fail("Sign-ups are disabled. Ask the operator for access.")
        if not auth_mod.constant_time_eq(invite, expected):
            return _fail("Invalid invite code.")
        if not auth_mod.valid_username(username):
            return _fail("Username must be 3–64 characters: letters, digits, . _ -")
        pw_err = auth_mod.validate_password_rules(password)
        if pw_err:
            return _fail(pw_err)
        user = db.create_user(username, auth_mod.hash_password(password))
        if user is None:
            return _fail("That username is already taken.")
        request.session["uid"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.post("/auth/login")
    async def auth_login(request: Request) -> Response:
        form = await request.form()
        username = auth_mod.normalize_username(form.get("username") or "")
        password = form.get("password") or ""
        user = db.get_user_by_username(username)
        if user is None or not auth_mod.verify_password(password, user.password_hash):
            return TEMPLATES.TemplateResponse(
                request, "login.html",
                {"error": "Invalid username or password."},
                status_code=400,
            )
        request.session["uid"] = user.id
        db.touch_user_login(user.id)
        return RedirectResponse("/", status_code=303)

    @app.post("/auth/logout")
    async def auth_logout(request: Request) -> Response:
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    def _hidden_source_names(request: Request) -> set[str]:
        """Source names to hide from THIS request. Over the public tunnel,
        non-public TBB units are hidden (privacy, Phase 3); on the LAN/admin
        side nothing is hidden so the operator sees every unit."""
        if getattr(request.state, "is_public", False):
            return db.private_unit_source_names()
        return set()

    def _runtime_to_cfg(row) -> SourceConfig:
        from birdbrain.config import OcrConfig
        return SourceConfig(
            name=row.name,
            kind=row.kind,
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

    def _all_sources() -> tuple[list[SourceConfig], dict[str, SourceConfig], list[LiveTile]]:
        """Static (toml) + runtime (DB) sources merged; runtime wins on name
        clash. Admin-disabled sources are dropped from the active roster — the
        supervisor's next 15-s tick then stops the worker."""
        disabled = db.list_disabled_source_names()
        merged: dict[str, SourceConfig] = {
            s.name: s for s in static_sources if s.name not in disabled
        }
        runtime_names: set[str] = set()
        for row in db.list_runtime_sources():
            if row.name in disabled:
                continue
            merged[row.name] = _runtime_to_cfg(row)
            runtime_names.add(row.name)
        ordered = list(merged.values())
        tiles = _build_tiles(ordered)
        for t in tiles:
            t.is_runtime = t.name in runtime_names  # type: ignore[attr-defined]
        return ordered, merged, tiles

    def _map_sites(sources_by_name: dict[str, SourceConfig]) -> list[dict]:
        """Map pins: sites.toml entries (for multi-site OCR resolution) plus any
        single-site source that carries its own lat/lon (most do — runtime
        sources added via /admin always do). Sites.toml wins on name clash so
        the OCR aliases stay authoritative."""
        seen: set[str] = set()
        out: list[dict] = []
        for site in sorted(sites.values(), key=lambda x: x.name):
            out.append({"name": site.name, "lat": site.lat, "lon": site.lon})
            seen.add(site.name)
        for src in sorted(sources_by_name.values(), key=lambda x: x.name):
            if src.name in seen or src.lat is None or src.lon is None:
                continue
            out.append({"name": src.name, "lat": src.lat, "lon": src.lon})
            seen.add(src.name)
        return out

    _front_slow_cache: dict = {"ts": 0.0, "val": None}
    _front_slow_lock = threading.Lock()

    def _front_slow_aggregates() -> tuple[dict, dict, list]:
        """The three front-page roll-ups that scan the whole detections table.

        Returns (species_all_by_src, first_seen_by_src, dpairs_30d). Cached for
        ``_FRONT_SLOW_TTL`` because each is a full scan and none of them can
        change visibly in five minutes: two are all-time figures over 1.58M
        rows, the third a 30-day species overlap.

        Double-checked under a lock so a burst of concurrent requests produces
        one recompute rather than one each — this used to be ~4.2s of the
        homepage, so the thundering herd is worth preventing.
        """
        now_m = time.monotonic()
        if (_front_slow_cache["val"] is not None
                and now_m - _front_slow_cache["ts"] <= _FRONT_SLOW_TTL):
            return _front_slow_cache["val"]
        with _front_slow_lock:
            now_m = time.monotonic()
            if (_front_slow_cache["val"] is not None
                    and now_m - _front_slow_cache["ts"] <= _FRONT_SLOW_TTL):
                return _front_slow_cache["val"]
            thirty_d = datetime.now(UTC) - timedelta(days=30)
            with db.session() as s:
                # Per-site all-time figures for the map's site-summary popup:
                # how many distinct species the site has ever logged, and when
                # it first produced a detection ("online since").
                species_all_by_src = dict(
                    s.execute(
                        select(
                            DetectionRow.source_name,
                            func.count(func.distinct(DetectionRow.scientific_name)),
                        ).group_by(DetectionRow.source_name)
                    ).all()
                )
                first_seen_by_src = dict(
                    s.execute(
                        select(
                            DetectionRow.source_name,
                            func.min(DetectionRow.started_at),
                        ).group_by(DetectionRow.source_name)
                    ).all()
                )
                dpairs = s.execute(
                    select(DetectionRow.source_name, DetectionRow.scientific_name)
                    .where(DetectionRow.started_at >= thirty_d)
                    .distinct()
                ).all()
            val = (species_all_by_src, first_seen_by_src, dpairs)
            _front_slow_cache.update(ts=time.monotonic(), val=val)
            return val

    def _front_activity(
        sources_by_name: dict[str, SourceConfig],
    ) -> tuple[list[dict], dict, list[dict]]:
        """Front-page activity: per mapped site, the count of unique species
        heard in the last 24h (drives the map bubbles) and the most-recently-
        heard distinct species (with age, for the by-site panel + popups).
        Also returns headline stats and a top-2-per-site list of inter-site
        species-overlap pairs (last 30 days) for the map's connection web."""
        now = datetime.now(UTC)
        since = now - timedelta(hours=24)
        recent_n = 6
        with db.session() as s:
            grouped = s.execute(
                select(
                    DetectionRow.source_name,
                    DetectionRow.scientific_name,
                    func.max(DetectionRow.common_name),
                    func.max(DetectionRow.started_at),
                )
                .where(DetectionRow.started_at >= since)
                .group_by(DetectionRow.source_name, DetectionRow.scientific_name)
            ).all()
            ts_rows = s.execute(
                select(DetectionRow.source_name, DetectionRow.started_at)
                .where(DetectionRow.started_at >= since)
            ).all()
            last_det = s.execute(select(func.max(DetectionRow.started_at))).scalar()
            # Distinct species ever recorded (matches the 24h metric's counting
            # — no replay filter — so the two species numbers are comparable).
            species_all = s.execute(
                select(func.count(func.distinct(DetectionRow.scientific_name)))
            ).scalar()

        # Per-site all-time figures for the map's site-summary popup, plus the
        # 30-day overlap used further down. Full scans, so they come from the
        # TTL cache rather than this request.
        species_all_by_src, first_seen_by_src, dpairs = _front_slow_aggregates()

        # tz per source (cached) — drives source-local hour bucketing + bands.
        tz_cache: dict[str, ZoneInfo] = {}

        def _site_tz(name: str) -> ZoneInfo:
            if name not in tz_cache:
                cfg = sources_by_name.get(name)
                tz_cache[name] = _zone_info(cfg.timezone if cfg else "UTC")
            return tz_cache[name]

        # Per-source 24h hour-of-day histogram (in the source's local time).
        per_hours: dict[str, list[int]] = {}
        for src, ts in ts_rows:
            aware = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
            per_hours.setdefault(src, [0] * 24)[aware.astimezone(_site_tz(src)).hour] += 1
        det_24h = len(ts_rows)

        per_source: dict[str, list[dict]] = {}
        species_seen: set[str] = set()
        for src, sci, common, last_seen in grouped:
            species_seen.add(sci)
            seen_at = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=UTC)
            per_source.setdefault(src, []).append(
                {"sci": sci, "common": common, "last_seen": seen_at}
            )

        activity: list[dict] = []
        for site in _map_sites(sources_by_name):
            name = site["name"]
            sp = sorted(
                per_source.get(name, []),
                key=lambda x: x["last_seen"],
                reverse=True,
            )
            hours_arr = per_hours.get(name, [0] * 24)
            tz = _site_tz(name)
            local_now = now.astimezone(tz)
            first_seen = first_seen_by_src.get(name)
            if first_seen is not None:
                if first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=UTC)
                online_since = _portable_strftime(first_seen.astimezone(tz), "%-d %b %Y")
            else:
                online_since = None
            bands = (
                _solar_bands(local_now.date(), site["lat"], site["lon"], tz)
                if site["lat"] is not None and site["lon"] is not None
                else []
            )
            activity.append({
                "name": name,
                "lat": site["lat"],
                "lon": site["lon"],
                # Per-site palette colour drives the dashboard map dot.
                # Falls back to the templates' default emerald.
                "color": SOURCE_COLORS.get(name, "#10b981"),
                "species_24h": len(sp),
                # Site-summary popup fields (location/biome, all-time species,
                # online-since). Concise by design — the popup links through to
                # the full site page for everything else.
                "biome": SOURCE_BIOME.get(name, ""),
                "species_all": int(species_all_by_src.get(name, 0)),
                "online_since": online_since,
                # IANA tz so the modal's hour-drill JS can compute "now"
                # in the site's clock without re-querying the server.
                "tz": str(tz),
                # Full 24-h activity clock for the hour-drill modal. The
                # map markers themselves are simple coloured dots, so no
                # second "compact" variant is shipped — saves ~70 KB of
                # inline SVG per dashboard load.
                "dial_svg_full": _radial_dial_svg(
                    hours_arr, highlight=local_now.hour, bands=bands,
                    compact=False, interactive=True,
                ),
                "recent": [
                    {
                        "sci": x["sci"],
                        "common": x["common"],
                        "age_s": max(0, int((now - x["last_seen"]).total_seconds())),
                    }
                    for x in sp[:recent_n]
                ],
            })

        last_age = None
        if last_det is not None:
            if last_det.tzinfo is None:
                last_det = last_det.replace(tzinfo=UTC)
            last_age = max(0, int((now - last_det).total_seconds()))

        heartbeats = {h.source_name: h for h in db.list_worker_heartbeats()}
        ordered = list(sources_by_name.values())
        cams_live = sum(
            1
            for c in ordered
            if _hb_status(heartbeats.get(c.name), now)[0] == "running"
        )
        stats = {
            "species_24h": len(species_seen),
            "species_all": int(species_all or 0),
            "det_24h": det_24h,
            "cams_live": cams_live,
            "cams_total": len(ordered),
            "last_age_s": last_age,
        }

        # Inter-site shared-species web, from the cached 30-day pairs above.
        # 30 days so the lines reflect ecological / flyway overlap rather than
        # 24h noise. We keep each site's two strongest links and let pairs
        # dedupe — this caps the drawing at ~20 lines for an 11-site network,
        # dense enough to read without smothering the map.
        coords_by_name = {
            e["name"]: (e["lat"], e["lon"])
            for e in activity
            if e["lat"] is not None and e["lon"] is not None
        }
        sp_by_src: dict[str, set[str]] = {}
        for src, sci in dpairs:
            if src in coords_by_name:
                sp_by_src.setdefault(src, set()).add(sci)
        # Symmetric counts; key by sorted tuple so each pair is unique.
        shared: dict[tuple[str, str], int] = {}
        names = sorted(sp_by_src)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                n = len(sp_by_src[a] & sp_by_src[b])
                if n > 0:
                    shared[(a, b)] = n
        # Per-site top-2 strongest neighbours; pairs union across sites so
        # mutually-strong neighbours produce one shared edge, not two.
        keep: set[tuple[str, str]] = set()
        for name in names:
            links = [
                ((a, b), count)
                for (a, b), count in shared.items()
                if name in (a, b)
            ]
            links.sort(key=lambda x: x[1], reverse=True)
            for pair, _ in links[:2]:
                keep.add(pair)
        pairs: list[dict] = []
        for (a, b) in sorted(keep):
            n = shared[(a, b)]
            la, lo_a = coords_by_name[a]
            lb, lo_b = coords_by_name[b]
            pairs.append({
                "a": a, "b": b, "shared": n,
                "lat_a": la, "lon_a": lo_a,
                "lat_b": lb, "lon_b": lo_b,
            })

        return activity, stats, pairs

    def _site_states(tiles: list[LiveTile], sources_by_name: dict[str, SourceConfig]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for tile in tiles:
            if not tile.multisite:
                continue
            cfg_src = sources_by_name[tile.name]
            state = db.get_source_state(tile.name)
            r = state_to_resolved(state, cfg_src)
            out[tile.name] = {
                "site": r.site,
                "latitude": r.latitude,
                "longitude": r.longitude,
                "detected_by": r.detected_by,
            }
        return out

    def _group_detections(rows: list[DetectionRow], bucket_seconds: int) -> list[dict]:
        """Collapse multiple detections of the same (source, species) in one
        time bucket into a single representative row + count. ``rows`` must be
        in descending ``started_at`` order — the first row encountered in each
        bucket is also the latest, which keeps the visible ordering stable."""
        if bucket_seconds <= 0:
            return [{"r": r, "n": 1, "latest": r.started_at} for r in rows]
        groups: dict[tuple, dict] = {}
        order: list[tuple] = []
        for r in rows:
            ts = r.started_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            bucket = int(ts.timestamp() // bucket_seconds)
            key = (r.source_name, r.scientific_name, bucket)
            g = groups.get(key)
            if g is None:
                groups[key] = {"r": r, "n": 1, "latest": r.started_at}
                order.append(key)
            else:
                g["n"] += 1
                # Keep the highest-confidence row as the representative so the
                # modal opens on the best clip in the bucket.
                if r.confidence > g["r"].confidence:
                    g["r"] = r
        return [groups[k] for k in order]

    def _note_tag_context() -> dict:
        """Shared context for any page that renders detection rows with
        palette-aware spectrograms. Keeps the template-side lookup tidy."""
        with db.session() as s:
            notes = list(s.scalars(select(SpeciesNoteRow)))
            tag_by_sci = {n.scientific_name: n.tag for n in notes}
            status_by_sci = {
                n.scientific_name: n.conservation_status
                for n in notes
                if n.conservation_status
            }
            all_species = list(
                s.scalars(
                    select(DetectionRow.common_name)
                    .group_by(DetectionRow.common_name)
                    .order_by(DetectionRow.common_name)
                )
            )
        return {
            "note_tag_by_sci": tag_by_sci,
            "status_by_sci": status_by_sci,
            "palette_for_tag": {
                k: v for k, v in SPEC_PALETTE_FOR_TAG.items() if k is not None
            },
            "default_palette": SPEC_PALETTE_FOR_TAG[None],
            "all_species": all_species,
        }

    @app.get("/robots.txt", response_class=PlainTextResponse)
    def robots_txt() -> PlainTextResponse:
        """Crawl policy. Public and cheap — no DB, no auth, no templating."""
        return PlainTextResponse(ROBOTS_TXT, media_type="text/plain")

    @app.get("/about", response_class=HTMLResponse)
    def about(request: Request) -> HTMLResponse:
        """Static credits + citation page. No DB calls; safe to render publicly
        through the tunnel (middleware only blocks /admin + mutating verbs)."""
        return TEMPLATES.TemplateResponse(request, "about.html", {})

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request) -> HTMLResponse:
        """Visitor guide: how to read the site (spectrograms, confidence, IUCN
        colours) + a one-line tour of each page. Static, no DB — safe to serve
        publicly through the tunnel."""
        return TEMPLATES.TemplateResponse(request, "help.html", {})

    @app.get("/api/species/list")
    def species_list() -> JSONResponse:
        """All detected species (scientific + common) — feeds the header search
        box, which fetches this once and filters client-side."""
        return JSONResponse(
            {
                "species": [
                    {"scientific": sci, "common": common}
                    for sci, common in db.list_detected_species()
                ]
            }
        )

    @app.get("/api/species/catalog")
    def species_catalog() -> JSONResponse:
        """Every species BirdNET can output — the full global label set, not
        just the ones detected here. Feeds the verification 'looked more like…'
        suggestion box so the operator can name any species the model knows.
        Read once from the birdnetlib labels file and cached; read-only, so
        safe to serve publicly through the tunnel."""
        return JSONResponse({"species": _birdnet_catalog()})

    @app.get("/sandbox", response_class=HTMLResponse)
    def sandbox_page(request: Request) -> HTMLResponse:
        """Live monitor for sources in sandbox (test) mode: detections appear as
        they fire but are never persisted, so you can verify a new mic (e.g. by
        playing bird sounds) without touching the live data. Includes a Go-live
        control per source."""
        names = sorted(
            key[len("sandbox:"):]
            for key in db.list_settings_with_prefix("sandbox:")
        )
        return TEMPLATES.TemplateResponse(
            request, "sandbox.html", {"sandbox_sources": names}
        )

    @app.get("/logo-test", response_class=HTMLResponse)
    def logo_test_page(request: Request) -> HTMLResponse:
        """Side-by-side gallery of the 10 logo iterations. SVGs are generated
        by tools/build_logos.py and served from /static/logo-test/. Each card
        shows the same file on dark + light backgrounds at lockup, 32 px and
        16 px scales so favicon legibility is visible at a glance."""
        variants = [
            (1,  "Birdbrain Classic",     "Symmetric · depth 6 · angle 30° · ratio 0.65"),
            (2,  "Heart Canopy",          "Squat · depth 6 · angle 35° · ratio 0.60"),
            (3,  "Tilted Perch",          "Classic geometry rotated 12° clockwise"),
            (4,  "Asymmetric Branches",   "Left split 35°, right split 25°, depth 6"),
            (5,  "Dense Network",         "Classic + skip-connections at depths 4–6"),
            (6,  "Minimal Mark",          "Depth 4, thicker strokes — favicon-optimised"),
            (7,  "Deep Detail",           "Depth 7, fine strokes — large-display canopy"),
            (8,  "Head Accent",           "Classic + emerald chevron and amber beak dot"),
            (9,  "Wing Spread",           "Angle 45°, ratio 0.70 — wings extended"),
            (10, "Spectrogram Rotation",  "Classic rotated 90° CCW — trunk left, branches right"),
            (11, "Crested Heart",         "v2 heart canopy + head chevron + beak dot — bird emerging from brain"),
            (12, "Heart on Neck",         "v2 canopy on an S-curved bird neck — anatomy hint"),
            (13, "Synaptic Heart",        "v2 + sparse arc skip-connections with synaptic dots — dendritic web"),
            (14, "Soaring Heart (polished)",  "Centred head + triangular beak + tail-feather chevron · final candidate"),
            (15, "Wide Crested Heart",    "Angle 40° (compromise between v2 and v9) + head chevron + beak dot"),
        ]
        return TEMPLATES.TemplateResponse(
            request, "logo_test.html",
            {"variants": [
                {"n": n, "name": name, "blurb": blurb,
                 "url": f"/static/logo-test/logo-{n:02d}.svg"}
                for n, name, blurb in variants
            ]},
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        _, sources_by_name, tiles = _all_sources()
        hidden = _hidden_source_names(request)
        if hidden:
            sources_by_name = {k: v for k, v in sources_by_name.items() if k not in hidden}
            tiles = [t for t in tiles if t.name not in hidden]
        # Initial render uses default grouping; JS swaps the URL on toggle.
        group_minutes = 5
        with db.session() as s:
            # Pull a wider window than ``limit`` so we have enough raw rows to
            # form ``limit`` groups when grouping is on. Replays are hidden by
            # default (see Database.not_replay_predicate); the /admin/replays
            # page is the place to inspect what's been filtered. Private TBB
            # units are also hidden from public requests.
            stmt = (
                select(DetectionRow)
                .where(Database.not_replay_predicate())
                .order_by(desc(DetectionRow.started_at))
                .limit(500)
            )
            if hidden:
                stmt = stmt.where(DetectionRow.source_name.not_in(hidden))
            raw = list(s.scalars(stmt))
            rows = _group_detections(raw, group_minutes * 60)[:50]
            # Most-recently-heard distinct species (replays excluded), for the
            # "recently heard" panel beside the map. Dedupe the recent feed by
            # species, keeping each one's latest detection — that row carries the
            # clip id so the panel can show a (clickable) spectrogram.
            _now = datetime.now(UTC)
            recently_heard: list[dict] = []
            _seen_sci: set[str] = set()
            for r in raw:
                if r.scientific_name in _seen_sci:
                    continue
                _seen_sci.add(r.scientific_name)
                last = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=UTC)
                recently_heard.append({
                    "id": r.id,
                    "scientific": r.scientific_name,
                    "common": r.common_name or r.scientific_name,
                    "confidence": r.confidence,
                    "source": r.source_name,
                    "started_at": r.started_at,
                    "age_s": max(0, int((_now - last).total_seconds())),
                })
                if len(recently_heard) >= 8:
                    break

        sites_activity, front_stats, site_pairs = _front_activity(sources_by_name)
        tiles_for_js = [
            {
                "name": t.name,
                "kind": t.kind,
                "video_id": t.video_id,
                "multisite": t.multisite,
                "url": t.url,
                "is_runtime": t.is_runtime,
            }
            for t in tiles
        ]
        source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
        latest_brief = db.get_latest_daily_brief()
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "rows": rows,
                "recently_heard": recently_heard,
                "tiles": tiles,
                "tiles_json": tiles_for_js,
                "sites": sorted(sites.values(), key=lambda s: s.name),
                "sites_activity": sites_activity,
                "site_pairs": site_pairs,
                "front_stats": front_stats,
                "site_states": _site_states(tiles, sources_by_name),
                "source_tz": source_tz,
                "latest_brief": latest_brief,
                **_note_tag_context(),
            },
        )

    @app.get("/partials/detections", response_class=HTMLResponse)
    def detections_partial(
        request: Request,
        source: list[str] | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
        # Upper bound is ~1 year in minutes (allows "annually" bucket).
        group_minutes: int = Query(default=5, ge=0, le=60 * 24 * 366),
        # 0 = "all time" — no recency filter.
        last_minutes: int = Query(default=0, ge=0, le=24 * 60 * 14),
        # 0 = "all species" — no species cap.
        top_species: int = Query(default=0, ge=0, le=200),
        # Default hides replays (YouTube ad/highlight loops, see
        # Database.not_replay_predicate). ?include_replays=1 reveals them.
        include_replays: bool = Query(default=False),
    ) -> HTMLResponse:
        _, sources_by_name, _ = _all_sources()
        # Fetch enough raw rows to fill ``limit`` buckets even when one
        # species dominates the window. The wider the bucket, the more raw
        # data we need to back it — at hourly+ grouping we may need everything.
        if group_minutes >= 60:
            raw_limit = 50_000
        elif group_minutes > 0:
            raw_limit = max(limit, limit * 10)
        else:
            raw_limit = limit
        # Normalise source filter: drop empty entries; empty list means "all".
        sources_filter = [s for s in (source or []) if s]
        hidden = _hidden_source_names(request)
        with db.session() as s:
            stmt = (
                select(DetectionRow)
                .order_by(desc(DetectionRow.started_at))
                .limit(raw_limit)
            )
            if sources_filter:
                stmt = stmt.where(DetectionRow.source_name.in_(sources_filter))
            if hidden:
                stmt = stmt.where(DetectionRow.source_name.not_in(hidden))
            if last_minutes > 0:
                cutoff = datetime.now(UTC) - timedelta(minutes=last_minutes)
                stmt = stmt.where(DetectionRow.started_at >= cutoff)
            if not include_replays:
                stmt = stmt.where(Database.not_replay_predicate())
            raw = list(s.scalars(stmt))
        rows = _group_detections(raw, group_minutes * 60)
        if top_species > 0:
            # Keep rows belonging to the N most recently active species (first
            # seen while scanning desc-by-time output). Order within each
            # species is preserved.
            allowed: list[str] = []
            seen: set[str] = set()
            for entry in rows:
                sci = entry["r"].scientific_name
                if sci not in seen:
                    seen.add(sci)
                    allowed.append(sci)
                    if len(allowed) >= top_species:
                        break
            allowed_set = set(allowed)
            rows = [e for e in rows if e["r"].scientific_name in allowed_set]
        rows = rows[:limit]
        source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
        return TEMPLATES.TemplateResponse(
            request,
            "_detection_rows.html",
            {
                "rows": rows,
                "selected_source": source,
                # Carry the feed's site filter to species pages only when it's
                # unambiguous (exactly one source selected); otherwise links go
                # to the all-sites species view.
                "page_source": sources_filter[0] if len(sources_filter) == 1 else None,
                "source_tz": source_tz,
                **_note_tag_context(),
            },
        )

    @app.get("/partials/site/{name}", response_class=HTMLResponse)
    def site_panel(request: Request, name: str) -> HTMLResponse:
        _, sources_by_name, tiles = _all_sources()
        _, sources_by_name, _ = _all_sources()
        if name not in sources_by_name:
            raise HTTPException(404, f"Unknown source: {name}")
        cfg_src = sources_by_name[name]
        state = db.get_source_state(name)
        resolved = state_to_resolved(state, cfg_src)
        return TEMPLATES.TemplateResponse(
            request,
            "_site_panel.html",
            {
                "tile": next(t for t in tiles if t.name == name),
                "sites": sorted(sites.values(), key=lambda s: s.name),
                "current": resolved,
            },
        )

    @app.post("/api/sources/{name}/site")
    def set_site(
        request: Request,
        name: str,
        site: str = Form(...),
        ttl_minutes: int = Form(default=DEFAULT_OVERRIDE_MINUTES),
    ) -> Response:
        _, sources_by_name, _ = _all_sources()
        if name not in sources_by_name:
            raise HTTPException(404, f"Unknown source: {name}")
        site_obj = sites.get(site)
        if site_obj is None:
            raise HTTPException(404, f"Unknown site: {site}")
        until = datetime.now(UTC) + timedelta(minutes=max(1, ttl_minutes))
        db.set_source_state(
            name,
            site=site_obj.name,
            latitude=site_obj.lat,
            longitude=site_obj.lon,
            detected_by="manual",
            manual_until=until,
        )
        # If this came from HTMX, return the updated panel; otherwise plain JSON.
        if request.headers.get("hx-request"):
            return site_panel(request, name)
        return JSONResponse({"ok": True, "site": site_obj.name, "manual_until": until.isoformat()})

    @app.delete("/api/sources/{name}/site")
    def clear_site(request: Request, name: str) -> Response:
        _, sources_by_name, _ = _all_sources()
        if name not in sources_by_name:
            raise HTTPException(404, f"Unknown source: {name}")
        db.clear_manual_override(name)
        if request.headers.get("hx-request"):
            return site_panel(request, name)
        return JSONResponse({"ok": True})

    @app.get("/api/sources/{name}/site")
    def get_site(name: str) -> JSONResponse:
        _, sources_by_name, _ = _all_sources()
        if name not in sources_by_name:
            raise HTTPException(404, f"Unknown source: {name}")
        cfg_src = sources_by_name[name]
        state = db.get_source_state(name)
        resolved = state_to_resolved(state, cfg_src)
        return JSONResponse(
            {
                "source": name,
                "site": resolved.site,
                "latitude": resolved.latitude,
                "longitude": resolved.longitude,
                "detected_by": resolved.detected_by,
            }
        )

    @app.get("/api/detections")
    def api_detections(
        source: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        include_replays: bool = Query(default=False),
    ) -> JSONResponse:
        with db.session() as s:
            stmt = (
                select(DetectionRow)
                .order_by(desc(DetectionRow.started_at))
                .limit(limit)
            )
            if source:
                stmt = stmt.where(DetectionRow.source_name == source)
            if not include_replays:
                stmt = stmt.where(Database.not_replay_predicate())
            rows = list(s.scalars(stmt))
        return JSONResponse(
            [
                {
                    "id": r.id,
                    "source_name": r.source_name,
                    "started_at": r.started_at.isoformat(),
                    "duration_s": r.duration_s,
                    "scientific_name": r.scientific_name,
                    "common_name": r.common_name,
                    "confidence": r.confidence,
                    "site": r.site,
                    "latitude": r.latitude,
                    "longitude": r.longitude,
                    "clip_url": f"/clips/{r.id}" if r.clip_path else None,
                }
                for r in rows
            ]
        )

    # --- Runtime sources (added/removed via the dashboard) ---

    @app.get("/partials/sources", response_class=HTMLResponse)
    def sources_partial(request: Request) -> HTMLResponse:
        _, _, tiles = _all_sources()
        return TEMPLATES.TemplateResponse(
            request, "_sources_panel.html", {"tiles": tiles}
        )

    @app.post("/api/sources")
    def add_runtime_source(
        request: Request,
        name: str = Form(...),
        url: str = Form(...),
        kind: str = Form(default="youtube"),
        multisite: bool = Form(default=False),
        lat: float | None = Form(default=None),
        lon: float | None = Form(default=None),
        cookies_file: str | None = Form(default=None),
        timezone: str = Form(default="UTC"),
        min_confidence: float = Form(default=0.3),
    ) -> Response:
        if kind not in ("youtube", "rtsp", "device"):
            raise HTTPException(400, "kind must be youtube, rtsp, or device")
        if not name.strip():
            raise HTTPException(400, "name is required")
        # Validate timezone before persisting so a typo doesn't slip through.
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as e:
            raise HTTPException(400, f"unknown timezone: {timezone}") from e
        db.add_runtime_source(
            name=name.strip(),
            kind=kind,
            url=url.strip(),
            lat=lat,
            lon=lon,
            min_confidence=min_confidence,
            multisite=multisite,
            cookies_from_browser=None,
            cookies_file=cookies_file or None,
            timezone=timezone,
        )
        if request.headers.get("hx-request"):
            return sources_partial(request)
        return JSONResponse({"ok": True, "name": name})

    @app.post("/api/settings/min-confidence", response_class=HTMLResponse)
    def update_global_min_confidence(
        request: Request, value: str = Form(default="")
    ) -> Response:
        """Set or clear the site-wide detection floor. An empty value clears
        the override so each source falls back to its own min_confidence.
        Workers pick up the change within the floor-refresh interval (~60s)."""
        raw = value.strip()
        if raw == "":
            db.set_global_min_confidence(None)
        else:
            try:
                parsed = float(raw)
            except ValueError as e:
                raise HTTPException(400, "min confidence must be a number") from e
            if not (0.0 <= parsed <= 1.0):
                raise HTTPException(400, "min confidence must be between 0 and 1")
            db.set_global_min_confidence(parsed)
        if request.headers.get("hx-request"):
            return TEMPLATES.TemplateResponse(
                request,
                "_global_min_conf.html",
                {"global_min_confidence": db.global_min_confidence()},
            )
        return JSONResponse({"ok": True, "global_min_confidence": db.global_min_confidence()})

    def _parse_cutoff(value: str) -> float | None:
        """Parse a cutoff form value: empty string → None (clear the override),
        otherwise a float validated into [0, 1]."""
        raw = value.strip()
        if raw == "":
            return None
        try:
            parsed = float(raw)
        except ValueError as e:
            raise HTTPException(400, "min confidence must be a number") from e
        if not (0.0 <= parsed <= 1.0):
            raise HTTPException(400, "min confidence must be between 0 and 1")
        return parsed

    def _site_cutoffs_ctx() -> dict:
        """Context for the per-site cutoffs panel — the source rows (with
        override-aware min_confidence) plus the active site-wide floor."""
        return {
            "rows": _admin_view()["rows"],
            "global_min_confidence": db.global_min_confidence(),
        }

    def _species_cutoffs_ctx() -> dict:
        """Context for the per-species cutoffs panel — current overrides, the
        species picker options, and the active site-wide floor (for the note)."""
        cutoffs = sorted(
            (
                {
                    "scientific_name": n.scientific_name,
                    "common_name": n.common_name or n.scientific_name,
                    "value": n.min_confidence,
                }
                for n in db.list_species_notes()
                if n.min_confidence is not None
            ),
            key=lambda c: c["common_name"].lower(),
        )
        options = [
            {"scientific_name": sci, "common_name": common}
            for sci, common in db.list_detected_species()
        ]
        return {
            "species_cutoffs": cutoffs,
            "species_options": options,
            "global_min_confidence": db.global_min_confidence(),
        }

    @app.post("/api/source-cutoff", response_class=HTMLResponse)
    def update_source_cutoff(
        request: Request,
        source_name: str = Form(...),
        value: str = Form(default=""),
    ) -> Response:
        """Set or clear a per-source detection floor. Empty value clears it,
        restoring the source's configured threshold. Workers apply it within
        the floor-refresh interval (~60s)."""
        name = source_name.strip()
        if not name:
            raise HTTPException(400, "source_name is required")
        db.set_source_min_confidence(name, _parse_cutoff(value))
        if request.headers.get("hx-request"):
            return TEMPLATES.TemplateResponse(
                request, "_site_cutoffs.html", _site_cutoffs_ctx()
            )
        return JSONResponse({"ok": True, "name": name})

    @app.post("/api/species-cutoff", response_class=HTMLResponse)
    def update_species_cutoff(
        request: Request,
        scientific_name: str = Form(...),
        value: str = Form(default=""),
    ) -> Response:
        """Set or clear a per-species detection floor. Empty value clears it.
        Creates a minimal species-note row to hang the override on if needed."""
        sci = scientific_name.strip()
        if not sci:
            raise HTTPException(400, "scientific_name is required")
        db.set_species_min_confidence(sci, _parse_cutoff(value))
        if request.headers.get("hx-request"):
            return TEMPLATES.TemplateResponse(
                request, "_species_cutoffs.html", _species_cutoffs_ctx()
            )
        return JSONResponse({"ok": True, "scientific_name": sci})

    @app.post("/api/sources/{name}/gate-highlights")
    def set_gate_highlights(
        request: Request, name: str, enabled: int = Form(...)
    ) -> Response:
        """Turn highlight-reel gating on/off for a source. The running worker
        polls this setting (~60s) and starts/stops its frame watcher to match —
        no restart needed. Returns the admin table so the toggle re-renders."""
        db.set_setting(f"gate_highlights:{name}", "1" if enabled else None)
        if request.headers.get("hx-request"):
            return admin_partial(request)
        return JSONResponse({"ok": True, "name": name, "enabled": bool(enabled)})

    @app.post("/api/sources/{name}/sandbox")
    def set_sandbox(request: Request, name: str, enabled: int = Form(...)) -> Response:
        """Turn sandbox (test) mode on/off for a source. The worker polls this
        (~60s): ON = run detection but divert results to the in-memory sandbox
        feed, never the DB or clips; OFF ("go live") = persist normally. No
        restart. Used to verify a new mic by playing sounds without polluting
        the live data."""
        db.set_setting(f"sandbox:{name}", "1" if enabled else None)
        if not enabled:
            sandbox.clear(name)  # going live — drop the test feed
        return JSONResponse({"ok": True, "name": name, "sandbox": bool(enabled)})

    @app.post("/api/sources/{name}/location")
    def set_source_location(
        request: Request,
        name: str,
        lat: str = Form(default=""),
        lon: str = Form(default=""),
    ) -> Response:
        """Set/clear a runtime source's coordinates (BirdNET's location filter).
        Empty fields clear them. Applies on the worker's next (re)start — lat/lon
        are read at startup, not polled. Returns the admin table so it re-renders."""
        def _parse(v: str) -> float | None:
            v = (v or "").strip()
            return float(v) if v else None
        try:
            latv, lonv = _parse(lat), _parse(lon)
        except ValueError as e:
            raise HTTPException(400, "lat/lon must be numbers") from e
        if not db.set_runtime_source_location(name, latv, lonv):
            raise HTTPException(404, f"no runtime source named {name!r}")
        if request.headers.get("hx-request"):
            return admin_partial(request)
        return JSONResponse({"ok": True, "name": name, "lat": latv, "lon": lonv})

    def _pw_mic_node(name: str) -> tuple[int | None, str]:
        """Resolve a device source's PipeWire capture-node id (by node.name, so
        it survives id reshuffles). Returns (id_or_None, reason)."""
        cfg = _all_sources()[1].get(name)
        if cfg is None or cfg.kind != "device" or not cfg.url.startswith("pulse:"):
            return None, "not a pulse mic source"
        target = cfg.url[len("pulse:"):]
        try:
            out = subprocess.run(
                ["pw-dump"], capture_output=True, text=True, timeout=6
            ).stdout
            for o in json.loads(out):
                props = (o.get("info") or {}).get("props") or {}
                if props.get("node.name") == target and str(
                    props.get("media.class", "")
                ).startswith("Audio/Source"):
                    return int(o["id"]), "ok"
        except Exception as e:  # pw-dump missing / not running / parse error
            return None, f"pipewire query failed: {e}"
        return None, "mic not found in PipeWire (check it's connected)"

    @app.get("/api/sources/{name}/mic-gain")
    def get_mic_gain(name: str) -> JSONResponse:
        """Current capture gain (0–1, where 1.0 = the device's max hardware gain)
        for a pulse-routed mic. supported=False for anything else."""
        node, reason = _pw_mic_node(name)
        if node is None:
            return JSONResponse({"supported": False, "reason": reason})
        try:
            out = subprocess.run(
                ["wpctl", "get-volume", str(node)],
                capture_output=True, text=True, timeout=5,
            ).stdout
            gain = float(out.strip().split()[1])  # "Volume: 0.45"
        except Exception:
            return JSONResponse({"supported": False, "reason": "could not read volume"})
        return JSONResponse({"supported": True, "gain": gain})

    @app.post("/api/sources/{name}/mic-gain")
    def set_mic_gain(name: str, gain: float = Form(...)) -> JSONResponse:
        """Set a pulse mic's capture gain via PipeWire (maps to the hardware
        preamp gain — lowering it cuts the noise floor). 0–1.0."""
        node, reason = _pw_mic_node(name)
        if node is None:
            raise HTTPException(400, reason)
        g = max(0.0, min(1.0, gain))
        try:
            subprocess.run(
                ["wpctl", "set-volume", str(node), f"{g:.2f}"],
                check=True, timeout=5,
            )
        except Exception as e:
            raise HTTPException(500, f"set-volume failed: {e}") from e
        return JSONResponse({"ok": True, "gain": g})

    @app.get("/api/sources/{name}/highpass")
    def get_highpass(name: str) -> JSONResponse:
        """Current high-pass cutoff (Hz) for a device source; 0 = off."""
        cfg = _all_sources()[1].get(name)
        raw = db.get_setting(f"highpass_hz:{name}")
        try:
            hz = int(float(raw)) if raw else 0
        except (TypeError, ValueError):
            hz = 0
        return JSONResponse(
            {"supported": cfg is not None and cfg.kind == "device", "hz": hz}
        )

    @app.post("/api/sources/{name}/highpass")
    def set_highpass(name: str, hz: int = Form(...)) -> JSONResponse:
        """Set the capture high-pass cutoff (Hz) for a device source; 0 disables.
        The worker relaunches ffmpeg with the new filter within ~60s; the
        audition stream applies it on the next (re)fetch."""
        cfg = _all_sources()[1].get(name)
        if cfg is None or cfg.kind != "device":
            raise HTTPException(400, "not a device source")
        hz = max(0, min(20000, int(hz)))
        db.set_setting(f"highpass_hz:{name}", str(hz) if hz > 0 else None)
        return JSONResponse({"ok": True, "hz": hz})

    @app.get("/api/sandbox/{name}")
    def sandbox_feed(name: str) -> JSONResponse:
        """Recent sandbox detections for a source — live monitor feed. In-memory
        only (empty once the worker restarts or the source goes live)."""
        return JSONResponse(
            {
                "name": name,
                "sandbox": bool(db.get_setting(f"sandbox:{name}")),
                "detections": sandbox.recent(name),
            }
        )

    def _is_static_source(name: str) -> bool:
        """True if ``name`` matches a sources.toml entry — independent of any
        runtime row that might exist with the same name."""
        return any(s.name == name for s in static_sources)

    @app.post("/api/sources/{name}/enable")
    def enable_source(request: Request, name: str) -> Response:
        """Re-enable a source. Dispatches by origin: runtime sources have
        their soft-delete cleared; static sources have their disable-override
        row removed. Supervisor picks it up within SUPERVISOR_INTERVAL (15s)
        and starts a worker."""
        did_runtime = db.enable_runtime_source(name)
        did_static = False
        if _is_static_source(name):
            db.set_source_disabled(name, disabled=False)
            did_static = True
        if not (did_runtime or did_static):
            raise HTTPException(404, f"Source not found: {name}")
        if request.headers.get("hx-request"):
            return admin_partial(request)
        return JSONResponse({"ok": True, "name": name})

    @app.post("/api/sources/{name}/disable")
    def disable_source(request: Request, name: str) -> Response:
        """Disable a source. Dispatches by origin: runtime sources are soft-
        deleted (deleted_at set); static sources get a row in
        source_disable_overrides. Supervisor stops the worker on its next tick.
        Returns the admin partial so the table re-renders in place."""
        did_runtime = db.soft_delete_runtime_source(name)
        did_static = False
        if _is_static_source(name):
            db.set_source_disabled(name, disabled=True)
            did_static = True
        if not (did_runtime or did_static):
            raise HTTPException(404, f"Source not found: {name}")
        if request.headers.get("hx-request"):
            return admin_partial(request)
        return JSONResponse({"ok": True, "name": name})

    @app.post("/api/sources/{name}/min_confidence")
    def set_source_min_confidence(
        request: Request,
        name: str,
        value: float = Form(..., ge=0.0, le=1.0),
    ) -> Response:
        """Edit a runtime source's detection floor. File-managed sources (in
        sources.toml) are not mutable through the UI by design — return 400
        with a hint to edit the file. The supervisor sees the row update on
        its next reconcile tick (~15 s) and respawns the worker so the new
        floor takes effect."""
        ok = db.update_runtime_source(name, min_confidence=value)
        if not ok:
            # Distinguish "doesn't exist" from "exists but file-managed".
            _, sources_by_name, _ = _all_sources()
            if name in sources_by_name:
                raise HTTPException(
                    400,
                    f"{name} is file-managed (sources.toml) — edit there instead.",
                )
            raise HTTPException(404, f"Runtime source not found: {name}")
        if request.headers.get("hx-request"):
            return admin_partial(request)
        return JSONResponse({"ok": True, "name": name, "min_confidence": value})

    @app.delete("/api/sources/{name}")
    def remove_runtime_source(request: Request, name: str) -> Response:
        ok = db.soft_delete_runtime_source(name)
        if not ok:
            raise HTTPException(404, f"Runtime source not found: {name}")
        if request.headers.get("hx-request"):
            return sources_partial(request)
        return JSONResponse({"ok": True})

    @app.get("/api/sources")
    def list_sources() -> JSONResponse:
        _, _, tiles = _all_sources()
        return JSONResponse(
            [
                {
                    "name": t.name,
                    "kind": t.kind,
                    "url": t.url,
                    "video_id": t.video_id,
                    "multisite": t.multisite,
                    "is_runtime": t.is_runtime,
                }
                for t in tiles
            ]
        )

    @app.get("/species", response_class=HTMLResponse)
    def species(
        request: Request,
        since: str = Query(default="all"),  # 24h | 7d | all
        source: list[str] | None = Query(default=None),
        q: str = Query(default=""),
        tail: bool = Query(default=False),  # level-2 zoom: only ≤median species
    ) -> HTMLResponse:
        """Per-source treemap of the species catalogue. Top-level rectangles
        are sources; leaves are (source, species) pairs sized by detection
        count in the chosen window. Clicking a leaf goes to /species/<sci>
        already scoped to that source."""
        _, sources_by_name, _ = _all_sources()
        sources_filter = [s for s in (source or []) if s]

        # Time window.
        now = datetime.now(UTC)
        if since == "24h":
            start = now - timedelta(hours=24)
        elif since == "7d":
            start = now - timedelta(days=7)
        else:
            start = None

        # One GROUP BY query covers everything we need for the treemap.
        stmt = (
            select(
                DetectionRow.source_name,
                DetectionRow.scientific_name,
                DetectionRow.common_name,
                func.count().label("n"),
                func.max(DetectionRow.confidence).label("max_conf"),
                func.max(DetectionRow.started_at).label("last_seen"),
            )
            .group_by(
                DetectionRow.source_name,
                DetectionRow.scientific_name,
                DetectionRow.common_name,
            )
        )
        if start is not None:
            stmt = stmt.where(DetectionRow.started_at >= start)
        if sources_filter:
            stmt = stmt.where(DetectionRow.source_name.in_(sources_filter))

        with db.session() as s:
            rows = list(s.execute(stmt))
            iucn = dict(s.execute(
                select(
                    SpeciesNoteRow.scientific_name,
                    SpeciesNoteRow.conservation_status,
                )
            ).all())

        # Group rows by source.
        per_source: dict[str, list[dict]] = {}
        for r in rows:
            per_source.setdefault(r.source_name, []).append({
                "scientific": r.scientific_name,
                "common": r.common_name,
                "n": int(r.n),
                "max_conf": float(r.max_conf or 0),
                "last_seen": r.last_seen,
                "iucn": iucn.get(r.scientific_name),
            })

        # Level-2 zoom: tail mode keeps only species at-or-below the per-site
        # median count, so the long tail expands to fill the viewport. Only
        # applied when exactly one source is in scope (otherwise the median
        # split is per-site and doesn't compose cleanly across sites). The
        # "loud" species we hid are remembered so the template can surface
        # them as a back-out list.
        tail_active = bool(tail) and len(per_source) == 1
        hidden_loud: list[dict] = []
        tail_threshold: int | None = None
        if tail_active:
            src_only = next(iter(per_source))
            counts = sorted(sp["n"] for sp in per_source[src_only])
            if counts:
                med = counts[len(counts) // 2]
                tail_threshold = med
                kept = [sp for sp in per_source[src_only] if sp["n"] <= med]
                hidden = sorted(
                    (sp for sp in per_source[src_only] if sp["n"] > med),
                    key=lambda s: -s["n"],
                )
                # If the median collapses everything (e.g. all species have
                # count 1), don't bother — fall back to the level-1 view.
                if kept and hidden:
                    per_source[src_only] = kept
                    hidden_loud = [
                        {
                            "scientific": sp["scientific"],
                            "common": sp["common"],
                            "n": sp["n"],
                            "max_conf": sp["max_conf"],
                            "iucn": sp["iucn"],
                        }
                        for sp in hidden
                    ]
                else:
                    tail_active = False
                    tail_threshold = None

        # ---- Squarified layout: outer = sources (sized by total detections),
        # inner = species inside each source ----
        viewport = {"w": 1200, "h": 700}
        source_totals = sorted(
            ((src, sum(sp["n"] for sp in per_source[src])) for src in per_source),
            key=lambda t: -t[1],
        )
        outer_rects: list[dict] = []
        tiles: list[dict] = []
        source_pad_top = 18  # leaves room for the source header label
        # When the user has zoomed in to a single source, the whole viewport
        # is dedicated to that site — show every species, including the long
        # tail. Otherwise roll the dust into "+N more" so the layout reads.
        single_source = len(source_totals) == 1
        min_leaf_area = 0 if single_source else 12 * 12

        if source_totals:
            outer_sizes = squarify.normalize_sizes(
                [t[1] for t in source_totals], viewport["w"], viewport["h"]
            )
            outer_layout = squarify.squarify(
                outer_sizes, 0, 0, viewport["w"], viewport["h"]
            )
            for (src_name, src_count), rect in zip(
                source_totals, outer_layout, strict=False
            ):
                outer_rects.append({
                    "name": src_name, "count": src_count,
                    "x": rect["x"], "y": rect["y"],
                    "w": rect["dx"], "h": rect["dy"],
                })
                inner_x = rect["x"] + 1
                inner_y = rect["y"] + source_pad_top
                inner_w = max(1.0, rect["dx"] - 2)
                inner_h = max(1.0, rect["dy"] - source_pad_top - 1)
                avail_area = inner_w * inner_h

                species_in_src = sorted(
                    per_source[src_name], key=lambda s: -s["n"]
                )
                total_size = sum(sp["n"] for sp in species_in_src)
                kept: list[dict] = []
                other_n = 0
                other_count = 0
                for sp in species_in_src:
                    est_area = (
                        sp["n"] / total_size * avail_area if total_size else 0
                    )
                    if est_area >= min_leaf_area:
                        kept.append(sp)
                    else:
                        other_n += sp["n"]
                        other_count += 1

                leaf_sizes = [sp["n"] for sp in kept] + (
                    [other_n] if other_n else []
                )
                if not leaf_sizes:
                    continue
                leaf_sizes_norm = squarify.normalize_sizes(
                    leaf_sizes, inner_w, inner_h
                )
                leaf_layout = squarify.squarify(
                    leaf_sizes_norm, inner_x, inner_y, inner_w, inner_h
                )
                for sp, lr in zip(kept, leaf_layout, strict=False):
                    ls = sp["last_seen"]
                    if ls.tzinfo is None:
                        ls = ls.replace(tzinfo=UTC)
                    tiles.append({
                        "source": src_name,
                        "scientific": sp["scientific"],
                        "common": sp["common"],
                        "n": sp["n"],
                        "max_conf": sp["max_conf"],
                        "age_s": int((now - ls).total_seconds()),
                        "iucn": sp["iucn"],
                        "x": lr["x"], "y": lr["y"],
                        "w": lr["dx"], "h": lr["dy"],
                        "is_other": False,
                    })
                if other_n:
                    or_ = leaf_layout[-1]
                    tiles.append({
                        "source": src_name,
                        "scientific": None,
                        "common": f"+{other_count} more",
                        "n": other_n,
                        "max_conf": None,
                        "age_s": None,
                        "iucn": None,
                        "x": or_["x"], "y": or_["y"],
                        "w": or_["dx"], "h": or_["dy"],
                        "is_other": True,
                    })

        # Alphabetical list — drives the mobile fallback and serves as an
        # accessible flat view of everything the treemap covers.
        all_species = sorted(
            {(sp["scientific"], sp["common"])
             for ss in per_source.values() for sp in ss},
            key=lambda x: x[1].lower(),
        )

        # Long tail at the zoomed-in level: rarest species at this site,
        # surfaced as a clickable list so the tiny tiles aren't the only
        # way to reach them. Empty list when not zoomed.
        long_tail: list[dict] = []
        if single_source:
            src_name = source_totals[0][0]
            long_tail = [
                {
                    "scientific": sp["scientific"],
                    "common": sp["common"],
                    "n": sp["n"],
                    "max_conf": sp["max_conf"],
                    "iucn": sp["iucn"],
                }
                for sp in sorted(per_source[src_name], key=lambda s: s["n"])[:24]
            ]

        all_known_sources = sorted(
            set(per_source.keys())
            | {c.name for c in sources_by_name.values()}
        )

        return TEMPLATES.TemplateResponse(
            request,
            "species.html",
            {
                "outer_rects": outer_rects,
                "tiles": tiles,
                "all_species": all_species,
                "viewport": viewport,
                "since": since,
                "source_filter": sources_filter,
                "q": q,
                "all_source_names": all_known_sources,
                "total_species": len(all_species),
                "total_detections": sum(
                    sp["n"] for ss in per_source.values() for sp in ss
                ),
                "source_colors": SOURCE_COLORS,
                "zoomed": single_source,
                "zoomed_source": (
                    source_totals[0][0] if single_source else None
                ),
                "long_tail": long_tail,
                "tail_active": tail_active,
                "tail_threshold": tail_threshold,
                "hidden_loud": hidden_loud,
            },
        )

    @app.get("/species/{scientific:path}", response_class=HTMLResponse)
    def species_detail(
        request: Request,
        scientific: str,
        source: str | None = Query(default=None),
        include_replays: bool = Query(default=False),
    ) -> HTMLResponse:
        """Every figure here is an aggregate or a handful of clips, so the page
        asks SQL for exactly that instead of reducing raw rows in Python.

        It used to materialise every detection for the species as ORM objects:
        167,606 of them for African Scops-Owl, roughly a gigabyte of a web
        worker per request, with the session pinned long enough to drain the
        connection pool (558 QueuePool timeouts between 2026-08-15 and 08-26,
        every traceback rooted here). Python never hands that memory back, so
        one visit permanently raised the worker's floor and the Pi eventually
        swapped itself to a standstill.

        The cost model is now the *number of times the replay predicate runs*,
        not the number of rows returned. ``not_replay_predicate`` is a
        correlated NOT EXISTS costing ~1.4s on a popular species, so the
        histograms are collected as one grouped grid — every dimension the page
        needs, in a single pass — and rolled up below. Splitting them back into
        a query apiece costs a second each. The grid is deliberately *unscoped*:
        the bubble map counts across all sites, and the site filter is a cheap
        slice of a few thousand grouped rows.
        """
        from collections import Counter

        _, sources_by_name, _ = _all_sources()
        active_source = source or None

        def _where(scoped: bool) -> list:
            w = [DetectionRow.scientific_name == scientific]
            if not include_replays:
                w.append(Database.not_replay_predicate())
            if scoped and active_source:
                w.append(DetectionRow.source_name == active_source)
            return w

        # Hour buckets keep their date so the tz conversion below stays
        # DST-correct rather than assuming a fixed offset per source.
        hour_expr = func.strftime("%Y-%m-%d %H", DetectionRow.started_at)
        bin_expr = func.cast(DetectionRow.confidence * 10, Integer)
        has_clip_expr = DetectionRow.clip_path.is_not(None)

        with db.session() as s:
            note = s.get(SpeciesNoteRow, scientific)
            grid = s.execute(
                select(
                    DetectionRow.source_name,
                    hour_expr,
                    DetectionRow.label,
                    bin_expr,
                    has_clip_expr,
                    DetectionRow.suggested_species,
                    func.count(),
                    func.max(DetectionRow.confidence),
                    func.min(DetectionRow.started_at),
                    func.max(DetectionRow.started_at),
                )
                .where(*_where(False))
                .group_by(
                    DetectionRow.source_name, hour_expr, DetectionRow.label,
                    bin_expr, has_clip_expr, DetectionRow.suggested_species,
                )
            ).all()

            if not grid and note is None:
                raise HTTPException(404, f"No detections or note for {scientific!r}")

            common_name = s.execute(
                select(DetectionRow.common_name)
                .where(*_where(False))
                .order_by(desc(DetectionRow.started_at))
                .limit(1)
            ).scalar() or (note.common_name if note else scientific)

            # Sample clips — the only full rows the page loads, at most 8 each.
            clip_where = [*_where(True), DetectionRow.clip_path.is_not(None)]
            latest_clip = s.scalars(
                select(DetectionRow).where(*clip_where)
                .order_by(desc(DetectionRow.started_at)).limit(1)
            ).first()
            top_clips = list(s.scalars(
                select(DetectionRow).where(*clip_where)
                .order_by(desc(DetectionRow.confidence)).limit(5)
            ))
            good_clips = list(s.scalars(
                select(DetectionRow)
                .where(*clip_where, DetectionRow.label == "good")
                .order_by(desc(DetectionRow.started_at)).limit(8)
            ))

            # Per-site totals for the bubble map: all sites, filter-independent.
            site_counts = Counter()
            for g in grid:
                site_counts[g[0]] += g[6]

            scoped = [g for g in grid if not active_source or g[0] == active_source]

            # Six clips spread across the confidence range — the same selection
            # the row-by-row version made (every step-th of the ascending list),
            # done with a window function so the list stays inside SQLite.
            n_clips = sum(g[6] for g in scoped if g[4])
            spread_clips = []
            if n_clips >= 6:
                step = max(1, n_clips // 6)
                ranked = (
                    select(
                        DetectionRow.id.label("id"),
                        func.row_number().over(
                            order_by=DetectionRow.confidence
                        ).label("rn"),
                    )
                    .where(*clip_where)
                    .subquery()
                )
                ids = [
                    r[0] for r in s.execute(
                        select(ranked.c.id)
                        .where((ranked.c.rn - 1) % step == 0)
                        .limit(6)
                    ).all()
                ]
                if ids:
                    by_id = {
                        r.id: r for r in s.scalars(
                            select(DetectionRow).where(DetectionRow.id.in_(ids))
                        )
                    }
                    spread_clips = [by_id[i] for i in ids if i in by_id]

        # ---- roll the grid up into the page's figures (all cheap, in memory) --
        per_source: dict[str, dict] = {}
        for src, _b, _l, _bin, _hc, _sg, n, mx, first, last in scoped:
            e = per_source.setdefault(
                src, {"count": 0, "max_conf": mx, "first_seen": first, "last_seen": last}
            )
            e["count"] += n
            e["max_conf"] = max(e["max_conf"], mx)
            e["first_seen"] = min(e["first_seen"], first)
            e["last_seen"] = max(e["last_seen"], last)
        # Count descending, ties broken by most-recently-heard. The row-by-row
        # version got that tie-break for free — it bucketed rows already ordered
        # started_at DESC and Python's sort is stable — so state it explicitly:
        # three Majete cameras tie at one detection each for Otus senegalensis,
        # and without this the table reshuffles between loads.
        ordered = sorted(per_source.items(), key=lambda kv: kv[1]["last_seen"], reverse=True)
        ordered.sort(key=lambda kv: -kv[1]["count"])
        source_summary = [{"source": src, **e} for src, e in ordered]
        total = sum(e["count"] for e in per_source.values())
        max_conf = max((e["max_conf"] for e in per_source.values()), default=0)

        DAYS_BACK = 14
        today_utc = datetime.now(UTC).date()
        oldest = today_utc - timedelta(days=DAYS_BACK - 1)
        day_counts: dict[str, int] = {}
        hours = [0] * 24
        conf_bins = [0] * 10
        label_counts_raw: Counter = Counter()
        suggested_counter: Counter = Counter()
        tz_cache: dict[str, object] = {}
        for src, bucket, label, cbin, _hc, sugg, n, _mx, _first, _last in scoped:
            if src not in tz_cache:
                cfg_src = sources_by_name.get(src)
                tz_cache[src] = _zone_info(cfg_src.timezone if cfg_src else "UTC")
            ts = datetime.strptime(bucket, "%Y-%m-%d %H").replace(tzinfo=UTC)
            hours[ts.astimezone(tz_cache[src]).hour] += n
            day = bucket[:10]
            if day >= oldest.isoformat():
                day_counts[day] = day_counts.get(day, 0) + n
            conf_bins[min(9, max(0, int(cbin)))] += n
            label_counts_raw[label] += n
            if sugg:
                suggested_counter[sugg] += n
        peak_hour = max(hours) or 1
        peak_conf_bin = max(conf_bins) or 1
        daily = [
            {
                "date": oldest + timedelta(days=i),
                "count": day_counts.get((oldest + timedelta(days=i)).isoformat(), 0),
            }
            for i in range(DAYS_BACK)
        ]
        peak_day = max((d["count"] for d in daily), default=0) or 1
        label_counts = {
            "good": label_counts_raw.get("good", 0),
            "bad": label_counts_raw.get("bad", 0),
            "unsure": label_counts_raw.get("unsure", 0),
            "unreviewed": label_counts_raw.get(None, 0),
        }

        # Map of every site this species HAS been heard at, coloured by the
        # site's biome palette (see SOURCE_COLORS). Sites that have never
        # recorded this species are omitted from the map entirely — the
        # absence isn't informative on a species page (the alphabetical
        # "By site" table below already lists who heard it and who didn't).
        sites_for_map = [
            {**site, "count": site_counts.get(site["name"], 0)}
            for site in _map_sites(sources_by_name)
            if site_counts.get(site["name"], 0) > 0
        ]
        # Sites where it was heard, most frequent first — drives the <select>
        # fallback (and covers any source lacking map coordinates).
        all_sources = [name for name, _ in site_counts.most_common()]

        # Per-(species, site) AI note — only when scoped to a single site.
        site_note = (
            db.get_species_site_note(scientific, active_source)
            if active_source else None
        )

        return TEMPLATES.TemplateResponse(
            request,
            "species_detail.html",
            {
                "scientific": scientific,
                "common_name": common_name,
                "note": note,
                "site_note": site_note,
                "active_source": active_source,
                "sites_for_map": sites_for_map,
                "source_colors": SOURCE_COLORS,
                "all_sources": all_sources,
                "total": total,
                "max_conf": max_conf,
                "source_summary": source_summary,
                "daily": daily,
                "peak_day": peak_day,
                "hours": hours,
                "peak_hour": peak_hour,
                "label_counts": label_counts,
                "suggested": suggested_counter.most_common(8),
                "conf_bins": conf_bins,
                "peak_conf_bin": peak_conf_bin,
                "top_clips": top_clips,
                "spread_clips": spread_clips,
                "good_clips": good_clips,
                "latest_clip": latest_clip,
                "source_tz": {
                    name: cfg.timezone for name, cfg in sources_by_name.items()
                },
                **_note_tag_context(),
            },
        )
    @app.get("/confidence", response_class=HTMLResponse)
    def confidence_page(
        request: Request,
        since: str = Query(default="all"),
        cutoff: float = Query(default=0.65),
    ) -> HTMLResponse:
        """How does the species roster collapse as you raise the BirdNET
        confidence floor? We pre-compute, per source, the right-cumulative
        species count and detection count over a 1%-wide bucket grid spanning
        0.10 → 0.99 (90 buckets). The slider is then a pure client-side index
        lookup — no server round-trip per drag tick."""
        from sqlalchemy import Integer
        from sqlalchemy import cast as sa_cast

        now = datetime.now(UTC)
        if since == "24h":
            start = now - timedelta(hours=24)
        elif since == "7d":
            start = now - timedelta(days=7)
        else:
            start = None

        N_BUCKETS = 90
        CONF_MIN = 0.10  # bucket index = floor((conf - 0.10) * 100), clamped

        # Per-(source, species) max confidence — drives the species curves.
        # common_name comes along (1:1 with scientific in BirdNET labels, so
        # grouping by it doesn't fragment counts) to label the drop-out roster.
        stmt_max = (
            select(
                DetectionRow.source_name,
                DetectionRow.scientific_name,
                DetectionRow.common_name,
                func.max(DetectionRow.confidence).label("max_conf"),
            )
            .group_by(
                DetectionRow.source_name,
                DetectionRow.scientific_name,
                DetectionRow.common_name,
            )
        )
        # Per-(source, bucket) detection counts — drives the detection curves.
        bucket = sa_cast(
            (DetectionRow.confidence - CONF_MIN) * 100, Integer
        ).label("bucket")
        stmt_hist = (
            select(
                DetectionRow.source_name,
                bucket,
                func.count().label("n"),
            )
            .where(DetectionRow.confidence >= CONF_MIN)
            .group_by(DetectionRow.source_name, bucket)
        )
        if start is not None:
            stmt_max = stmt_max.where(DetectionRow.started_at >= start)
            stmt_hist = stmt_hist.where(DetectionRow.started_at >= start)

        with db.session() as s:
            rows_max = list(s.execute(stmt_max))
            rows_hist = list(s.execute(stmt_hist))

        per_source_maxconfs: dict[str, list[float]] = {}
        species_global_max: dict[str, float] = {}
        # Drop-out roster: every species with its max confidence, both per-site
        # and overall (each species at its global best). The client splits these
        # into kept/dropped at the slider's cutoff. Bucket index matches the
        # curve's (see species_buckets) so the roster's kept count equals the
        # headline "species kept". Species below CONF_MIN never enter the curve
        # floor, so we skip them here too.
        def _bucket(c: float) -> int:
            return max(0, min(N_BUCKETS - 1, int((c - CONF_MIN) * 100)))

        roster_by_site: dict[str, list[dict]] = {}
        species_global: dict[str, dict] = {}  # sci -> {c: common, s: sci, v, b}
        for r in rows_max:
            c = float(r.max_conf or 0)
            per_source_maxconfs.setdefault(r.source_name, []).append(c)
            if c > species_global_max.get(r.scientific_name, 0.0):
                species_global_max[r.scientific_name] = c
            if c >= CONF_MIN:
                entry = {
                    "c": r.common_name,
                    "s": r.scientific_name,
                    "v": round(c, 2),
                    "b": _bucket(c),
                }
                roster_by_site.setdefault(r.source_name, []).append(entry)
                g = species_global.get(r.scientific_name)
                if g is None or c > g["_raw"]:
                    species_global[r.scientific_name] = {**entry, "_raw": c}

        def _sorted_roster(entries: list[dict]) -> list[dict]:
            # Highest confidence first; drop the internal _raw helper key.
            return [
                {k: v for k, v in e.items() if k != "_raw"}
                for e in sorted(entries, key=lambda e: -e["v"])
            ]

        roster_overall = _sorted_roster(list(species_global.values()))
        roster_by_site = {
            src: _sorted_roster(entries) for src, entries in roster_by_site.items()
        }

        per_source_hist: dict[str, list[int]] = {}
        for r in rows_hist:
            h = per_source_hist.setdefault(r.source_name, [0] * N_BUCKETS)
            b = int(r.bucket)
            if 0 <= b < N_BUCKETS:
                h[b] += int(r.n)

        def species_buckets(maxconfs: list[float]) -> list[int]:
            b = [0] * N_BUCKETS
            for c in maxconfs:
                idx = int((c - CONF_MIN) * 100)
                if idx < 0:
                    continue
                if idx >= N_BUCKETS:
                    idx = N_BUCKETS - 1
                b[idx] += 1
            return b

        def cumulative_from_right(buckets: list[int]) -> list[int]:
            out = [0] * len(buckets)
            running = 0
            for i in range(len(buckets) - 1, -1, -1):
                running += buckets[i]
                out[i] = running
            return out

        species_curves: dict[str, list[int]] = {
            src: cumulative_from_right(species_buckets(per_source_maxconfs[src]))
            for src in per_source_maxconfs
        }
        det_curves: dict[str, list[int]] = {
            src: cumulative_from_right(per_source_hist[src])
            for src in per_source_hist
        }
        overall_species_curve = cumulative_from_right(
            species_buckets(list(species_global_max.values()))
        )
        overall_hist = [0] * N_BUCKETS
        for h in per_source_hist.values():
            for i, v in enumerate(h):
                overall_hist[i] += v
        overall_det_curve = cumulative_from_right(overall_hist)

        # Chart geometry.
        chart = {"w": 820, "h": 320, "padL": 44, "padR": 16, "padT": 14, "padB": 32}
        chart["plotW"] = chart["w"] - chart["padL"] - chart["padR"]
        chart["plotH"] = chart["h"] - chart["padT"] - chart["padB"]

        y_max_species = max(
            overall_species_curve[0] if overall_species_curve else 0, 1
        )

        def polyline(curve: list[int], y_max: int) -> str:
            pts = []
            for i, v in enumerate(curve):
                x = chart["padL"] + (i / max(1, N_BUCKETS - 1)) * chart["plotW"]
                y = chart["padT"] + chart["plotH"] * (1 - v / y_max)
                pts.append(f"{x:.1f},{y:.1f}")
            return " ".join(pts)

        site_polylines = {
            src: polyline(species_curves[src], y_max_species)
            for src in species_curves
        }
        overall_polyline = polyline(overall_species_curve, y_max_species)

        x_ticks = []
        for tv in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.99):
            idx = int(round((tv - CONF_MIN) * 100))
            x_ticks.append({
                "val": tv,
                "x": chart["padL"] + (idx / max(1, N_BUCKETS - 1)) * chart["plotW"],
            })

        # Y ticks: pick a 1/2/5 × 10^k step that yields ~5 ticks.
        def nice_step(y_max: int, target: int = 5) -> int:
            raw = max(1, y_max / target)
            mag = 10 ** int(math.floor(math.log10(raw)))
            for m in (1, 2, 5, 10):
                if raw / mag <= m:
                    return m * mag
            return 10 * mag

        step = nice_step(y_max_species)
        y_ticks = []
        v = 0
        while v <= y_max_species + step // 2:
            y_ticks.append({
                "val": v,
                "y": chart["padT"]
                    + chart["plotH"] * (1 - v / max(1, y_max_species)),
            })
            v += step

        cutoff = max(CONF_MIN, min(0.99, cutoff))
        default_idx = int(round((cutoff - CONF_MIN) * 100))

        return TEMPLATES.TemplateResponse(
            request,
            "confidence.html",
            {
                "since": since,
                "site_names": sorted(per_source_maxconfs.keys()),
                "species_curves": species_curves,
                "det_curves": det_curves,
                "overall_species_curve": overall_species_curve,
                "overall_det_curve": overall_det_curve,
                "site_polylines": site_polylines,
                "overall_polyline": overall_polyline,
                "source_colors": SOURCE_COLORS,
                "chart": chart,
                "y_max_species": y_max_species,
                "x_ticks": x_ticks,
                "y_ticks": y_ticks,
                "conf_min": CONF_MIN,
                "n_buckets": N_BUCKETS,
                "default_cutoff": cutoff,
                "default_idx": default_idx,
                "total_species_floor": (
                    overall_species_curve[0] if overall_species_curve else 0
                ),
                "total_dets_floor": (
                    overall_det_curve[0] if overall_det_curve else 0
                ),
                "roster_overall": roster_overall,
                "roster_by_site": roster_by_site,
            },
        )

    # IUCN statuses we treat as "conservation-flagged" on /rare. NT is on the
    # edge but worth surfacing for sub-Saharan birding; EX/EW are kept for
    # completeness even though hearing one would be world-news. Ordered most
    # urgent first so /rare can sort by this list's index.
    _RARE_IUCN_ORDER = ["CR", "EN", "VU", "NT", "EW", "EX"]
    _RARE_IUCN_SET = set(_RARE_IUCN_ORDER)

    def _rare_pick_clip_id(s, sci: str, src: str, max_conf: float) -> int | None:
        """For a (species, source) pair, return the detection_id of the
        clip whose confidence equals the group's max_conf. Used by every
        section to pick a representative spectrogram trigger."""
        return s.execute(
            select(DetectionRow.id)
            .where(DetectionRow.scientific_name == sci)
            .where(DetectionRow.source_name == src)
            .where(DetectionRow.confidence == max_conf)
            .order_by(desc(DetectionRow.started_at))
            .limit(1)
        ).scalar()

    def _rare_newly_heard(s, start, sources: list[str]) -> list[dict]:
        """(source, species) pairs whose first-ever detection at that source
        lies inside the window. Empty for since=all. Up to 20 rows, sorted
        by max confidence so the most clip-worthy newcomers come first."""
        if start is None:
            return []
        stmt = (
            select(
                DetectionRow.source_name,
                DetectionRow.scientific_name,
                DetectionRow.common_name,
                func.min(DetectionRow.started_at).label("first_at"),
                func.max(DetectionRow.confidence).label("max_conf"),
                func.count().label("n"),
            )
            .group_by(
                DetectionRow.source_name,
                DetectionRow.scientific_name,
                DetectionRow.common_name,
            )
            .having(func.min(DetectionRow.started_at) >= start)
            .order_by(desc("max_conf"))
            .limit(20)
        )
        if sources:
            stmt = stmt.where(DetectionRow.source_name.in_(sources))
        out = []
        for r in s.execute(stmt):
            out.append({
                "source": r.source_name,
                "scientific": r.scientific_name,
                "common": r.common_name,
                "first_at": r.first_at,
                "max_conf": float(r.max_conf or 0),
                "n": int(r.n),
                "detection_id": _rare_pick_clip_id(
                    s, r.scientific_name, r.source_name, float(r.max_conf or 0)
                ),
            })
        return out

    def _rare_long_tail(s, start, sources: list[str],
                        bad_set: set[str]) -> list[dict]:
        """Species with COUNT ≤ 3 in the window and max_conf ≥ 0.50, ranked
        by max_conf. Drops species whose every reviewed clip is 'bad' — those
        are systemic errors, not rarities. Surfaces (best_source, best clip)
        for each, so the user can audit the one most likely to be a real
        find."""
        stmt = (
            select(
                DetectionRow.scientific_name,
                DetectionRow.common_name,
                func.count().label("n"),
                func.max(DetectionRow.confidence).label("max_conf"),
                func.count(func.distinct(DetectionRow.source_name)).label(
                    "n_sites"
                ),
            )
            .group_by(
                DetectionRow.scientific_name,
                DetectionRow.common_name,
            )
            .having(func.count() <= 3)
            .having(func.max(DetectionRow.confidence) >= 0.50)
            .order_by(desc("max_conf"))
            .limit(40)  # over-fetch; bad_set filter trims below
        )
        if start is not None:
            stmt = stmt.where(DetectionRow.started_at >= start)
        if sources:
            stmt = stmt.where(DetectionRow.source_name.in_(sources))
        rows = list(s.execute(stmt))
        out = []
        for r in rows:
            if r.scientific_name in bad_set:
                continue
            # Best source for this species in the window.
            best_src = s.execute(
                select(
                    DetectionRow.source_name,
                    func.max(DetectionRow.confidence).label("max_conf"),
                )
                .where(DetectionRow.scientific_name == r.scientific_name)
                .where(DetectionRow.confidence == r.max_conf)
                .group_by(DetectionRow.source_name)
                .limit(1)
            ).first()
            src_name = best_src.source_name if best_src else None
            out.append({
                "scientific": r.scientific_name,
                "common": r.common_name,
                "n": int(r.n),
                "n_sites": int(r.n_sites),
                "max_conf": float(r.max_conf or 0),
                "best_source": src_name,
                "detection_id": (
                    _rare_pick_clip_id(
                        s, r.scientific_name, src_name, float(r.max_conf or 0)
                    ) if src_name else None
                ),
            })
            if len(out) >= 20:
                break
        return out

    def _rare_label_counts(s, sources: list[str]) -> dict[str, dict]:
        """Per-species good/bad/unsure tallies across all time (labels are
        rare enough that the window doesn't materially help here)."""
        stmt = (
            select(
                DetectionRow.scientific_name,
                DetectionRow.label,
                func.count().label("n"),
            )
            .where(DetectionRow.label.isnot(None))
            .group_by(DetectionRow.scientific_name, DetectionRow.label)
        )
        if sources:
            stmt = stmt.where(DetectionRow.source_name.in_(sources))
        out: dict[str, dict] = {}
        for r in s.execute(stmt):
            d = out.setdefault(r.scientific_name, {
                "good": 0, "bad": 0, "unsure": 0,
            })
            if r.label in d:
                d[r.label] = int(r.n)
        return out

    def _rare_misclass_patterns(s, label_counts: dict[str, dict],
                                tag_by_sci: dict[str, str | None],
                                common_by_sci: dict[str, str],
                                start, sources: list[str]) -> list[dict]:
        """Species the model gets wrong: tag='suspect' OR (>=5 reviewed AND
        bad/(good+bad) >= 0.5). Each row carries its dominant suggested-
        species correction and a representative clip."""
        suspect: list[dict] = []
        seen: set[str] = set()
        # Candidates: every labelled species AND every tag='suspect' species
        # (the latter may have no labelled detections yet, but we still want
        # them surfaced as patterns the model is known to confuse).
        candidates = set(label_counts.keys()) | {
            sci for sci, tag in tag_by_sci.items() if tag == "suspect"
        }
        for sci in candidates:
            counts = label_counts.get(sci, {"good": 0, "bad": 0, "unsure": 0})
            good, bad = counts["good"], counts["bad"]
            reviewed = good + bad
            tag = tag_by_sci.get(sci)
            bad_rate = (bad / reviewed) if reviewed else 0.0
            qualifies = (
                tag == "suspect"
                or (reviewed >= 5 and bad_rate >= 0.5)
            )
            if not qualifies:
                continue
            seen.add(sci)
            # Top correction for this species (most common
            # suggested_species when labelled bad).
            top_corr = s.execute(
                select(
                    DetectionRow.suggested_species,
                    func.count().label("n"),
                )
                .where(DetectionRow.scientific_name == sci)
                .where(DetectionRow.label == "bad")
                .where(DetectionRow.suggested_species.isnot(None))
                .group_by(DetectionRow.suggested_species)
                .order_by(desc("n"))
                .limit(1)
            ).first()
            # Best clip in window for audit.
            clip_q = (
                select(
                    DetectionRow.id,
                    DetectionRow.source_name,
                    func.max(DetectionRow.confidence).label("c"),
                )
                .where(DetectionRow.scientific_name == sci)
                .group_by(DetectionRow.source_name)
                .order_by(desc("c"))
                .limit(1)
            )
            if start is not None:
                clip_q = clip_q.where(DetectionRow.started_at >= start)
            if sources:
                clip_q = clip_q.where(
                    DetectionRow.source_name.in_(sources)
                )
            clip = s.execute(clip_q).first()
            suspect.append({
                "scientific": sci,
                "common": common_by_sci.get(sci, sci),
                "tag": tag,
                "good": good,
                "bad": bad,
                "reviewed": reviewed,
                "bad_rate": bad_rate,
                "top_correction": top_corr.suggested_species if top_corr else None,
                "top_correction_n": int(top_corr.n) if top_corr else 0,
                "best_source": clip.source_name if clip else None,
                "best_conf": float(clip.c) if clip else 0.0,
                "detection_id": clip.id if clip else None,
            })
        # Sort: tag='suspect' first, then by bad_rate desc, then reviewed desc.
        suspect.sort(
            key=lambda r: (
                0 if r["tag"] == "suspect" else 1,
                -r["bad_rate"],
                -r["reviewed"],
            )
        )
        return suspect[:10], seen

    def _rare_top_corrections(s, sources: list[str]) -> list[dict]:
        """Across all reviews, the most common "X actually was Y" patterns.
        Surfaces systemic BirdNET confusions like 'Red-eyed Dove →
        Cape Turtle-Dove'. Compact list, capped at 10."""
        stmt = (
            select(
                DetectionRow.scientific_name,
                DetectionRow.common_name,
                DetectionRow.suggested_species,
                func.count().label("n"),
            )
            .where(DetectionRow.label == "bad")
            .where(DetectionRow.suggested_species.isnot(None))
            .group_by(
                DetectionRow.scientific_name,
                DetectionRow.common_name,
                DetectionRow.suggested_species,
            )
            .order_by(desc("n"))
            .limit(10)
        )
        if sources:
            stmt = stmt.where(DetectionRow.source_name.in_(sources))
        return [
            {
                "scientific": r.scientific_name,
                "common": r.common_name,
                "suggested": r.suggested_species,
                "n": int(r.n),
            }
            for r in s.execute(stmt)
        ]

    def _rare_audit_queue(s, start, sources: list[str],
                          patterns_set: set[str]) -> list[dict]:
        """Up to 10 unreviewed in-window detections of species already
        flagged as suspect. Highest-confidence first — those are the
        outliers most worth a listen."""
        if not patterns_set:
            return []
        stmt = (
            select(DetectionRow)
            .where(DetectionRow.label.is_(None))
            .where(DetectionRow.scientific_name.in_(patterns_set))
            .order_by(desc(DetectionRow.confidence))
            .limit(10)
        )
        if start is not None:
            stmt = stmt.where(DetectionRow.started_at >= start)
        if sources:
            stmt = stmt.where(DetectionRow.source_name.in_(sources))
        return list(s.scalars(stmt))

    def _rare_conservation_flagged(s, start, sources: list[str]) -> list[dict]:
        """Species whose IUCN status is in {CR,EN,VU,NT,EW,EX} and have at
        least one detection in the window. Sorted by status severity, then
        by max confidence so the strongest evidence per status floats up."""
        # Get every threatened-status species' notes — small table, in-memory
        # filter is fine.
        notes = list(s.execute(
            select(
                SpeciesNoteRow.scientific_name,
                SpeciesNoteRow.common_name,
                SpeciesNoteRow.conservation_status,
            ).where(
                SpeciesNoteRow.conservation_status.in_(_RARE_IUCN_SET)
            )
        ))
        if not notes:
            return []
        sci_to_status = {n.scientific_name: n.conservation_status for n in notes}
        sci_to_common = {n.scientific_name: n.common_name for n in notes}
        stmt = (
            select(
                DetectionRow.scientific_name,
                func.count().label("n"),
                func.max(DetectionRow.confidence).label("max_conf"),
                func.max(DetectionRow.started_at).label("last_seen"),
                func.count(func.distinct(DetectionRow.source_name)).label(
                    "n_sites"
                ),
            )
            .where(DetectionRow.scientific_name.in_(sci_to_status.keys()))
            .group_by(DetectionRow.scientific_name)
        )
        if start is not None:
            stmt = stmt.where(DetectionRow.started_at >= start)
        if sources:
            stmt = stmt.where(DetectionRow.source_name.in_(sources))
        out = []
        for r in s.execute(stmt):
            status = sci_to_status[r.scientific_name]
            # Best site for this species (where the max-conf clip lives).
            best_src = s.execute(
                select(DetectionRow.source_name)
                .where(DetectionRow.scientific_name == r.scientific_name)
                .where(DetectionRow.confidence == r.max_conf)
                .limit(1)
            ).scalar()
            out.append({
                "scientific": r.scientific_name,
                "common": sci_to_common[r.scientific_name],
                "conservation_status": status,
                "severity": _RARE_IUCN_ORDER.index(status),
                "n": int(r.n),
                "n_sites": int(r.n_sites),
                "max_conf": float(r.max_conf or 0),
                "last_seen": r.last_seen,
                "best_source": best_src,
                "detection_id": (
                    _rare_pick_clip_id(
                        s, r.scientific_name, best_src,
                        float(r.max_conf or 0),
                    ) if best_src else None
                ),
            })
        # Severity first (CR has index 0 — most urgent), then max_conf desc.
        out.sort(key=lambda r: (r["severity"], -r["max_conf"]))
        return out[:20]

    @app.get("/rare", response_class=HTMLResponse)
    def rare_page(
        request: Request,
        since: str = Query(default="7d"),
        source: list[str] | None = Query(default=None),
    ) -> HTMLResponse:
        """Rare & interesting detections — surfaces species the loud-common
        cacophony drowns out, and patterns that look like systemic BirdNET
        misclassifications. Four sections; see the plan file for the rules
        behind each."""
        _, sources_by_name, _ = _all_sources()
        sources_filter = [s for s in (source or []) if s]

        now = datetime.now(UTC)
        if since == "24h":
            start = now - timedelta(hours=24)
        elif since == "7d":
            start = now - timedelta(days=7)
        else:
            start = None

        with db.session() as s:
            # Shared lookups used by multiple sections.
            notes = list(s.execute(
                select(
                    SpeciesNoteRow.scientific_name,
                    SpeciesNoteRow.common_name,
                    SpeciesNoteRow.tag,
                    SpeciesNoteRow.conservation_status,
                )
            ))
            tag_by_sci = {n.scientific_name: n.tag for n in notes}
            status_by_sci = {
                n.scientific_name: n.conservation_status
                for n in notes if n.conservation_status
            }
            common_by_sci = {n.scientific_name: n.common_name for n in notes}

            label_counts = _rare_label_counts(s, sources_filter)
            # Set of species whose every reviewed clip was 'bad' — excluded
            # from the long-tail surface so we don't promote known noise.
            all_bad = {
                sci for sci, c in label_counts.items()
                if c["bad"] > 0 and c["good"] == 0 and c["unsure"] == 0
            }

            newly_heard = _rare_newly_heard(s, start, sources_filter)
            long_tail = _rare_long_tail(s, start, sources_filter, all_bad)
            misclass_patterns, patterns_set = _rare_misclass_patterns(
                s, label_counts, tag_by_sci, common_by_sci,
                start, sources_filter,
            )
            top_corrections = _rare_top_corrections(s, sources_filter)
            audit_queue = _rare_audit_queue(
                s, start, sources_filter, patterns_set,
            )
            conservation = _rare_conservation_flagged(
                s, start, sources_filter,
            )

        all_source_names = sorted(sources_by_name.keys())

        return TEMPLATES.TemplateResponse(
            request,
            "rare.html",
            {
                "since": since,
                "source_filter": sources_filter,
                "all_source_names": all_source_names,
                "newly_heard": newly_heard,
                "long_tail": long_tail,
                "misclass_patterns": misclass_patterns,
                "top_corrections": top_corrections,
                "audit_queue": audit_queue,
                "conservation": conservation,
                "source_tz": {
                    n: c.timezone for n, c in sources_by_name.items()
                },
                "status_by_sci": status_by_sci,
                "tag_by_sci": tag_by_sci,
                **_note_tag_context(),
            },
        )

    @app.get("/site/{name:path}", response_class=HTMLResponse)
    def site_detail(request: Request, name: str) -> HTMLResponse:
        """Per-site detail page. Mirrors /species/{scientific}: AI commentary
        on top, then top species, hourly rhythm, and recent detections."""
        # A private unit's page is not visible over the public tunnel.
        if name in _hidden_source_names(request):
            raise HTTPException(404, f"No site or detections for {name!r}")
        _, sources_by_name, _ = _all_sources()
        src_cfg = sources_by_name.get(name)
        site_note = db.get_site_note(name)
        with db.session() as s:
            total = s.execute(
                select(func.count(DetectionRow.id))
                .where(DetectionRow.source_name == name)
            ).scalar() or 0

            if total == 0 and site_note is None and src_cfg is None:
                raise HTTPException(404, f"No site or detections for {name!r}")

            top_species = list(s.execute(
                select(
                    DetectionRow.scientific_name,
                    DetectionRow.common_name,
                    func.count(DetectionRow.id).label("n"),
                    func.max(DetectionRow.confidence).label("max_conf"),
                    func.max(DetectionRow.started_at).label("last_seen"),
                )
                .where(DetectionRow.source_name == name)
                .group_by(DetectionRow.scientific_name, DetectionRow.common_name)
                .order_by(desc("n"))
                .limit(20)
            ))

            # Recent unique species at this site — one row per species, most-
            # recently-heard first. Drives the scrollable panel beside the
            # video; complements top_species (sorted by count). We pick the
            # actual most-recent detection row per species (via a row_number
            # window) so the panel can show a clickable spectrogram of that
            # clip, not just the name.
            _ru = (
                select(
                    DetectionRow.id,
                    DetectionRow.source_name,
                    DetectionRow.scientific_name,
                    DetectionRow.common_name,
                    DetectionRow.confidence,
                    DetectionRow.started_at,
                    DetectionRow.label,
                    DetectionRow.suggested_species,
                    DetectionRow.sound_rating,
                    func.row_number().over(
                        partition_by=DetectionRow.scientific_name,
                        order_by=desc(DetectionRow.started_at),
                    ).label("rn"),
                )
                .where(DetectionRow.source_name == name)
                .subquery()
            )
            recent_unique = list(s.execute(
                select(_ru)
                .where(_ru.c.rn == 1)
                .order_by(desc(_ru.c.started_at))
                .limit(40)
            ))

            recent = list(s.scalars(
                select(DetectionRow)
                .where(DetectionRow.source_name == name)
                .where(Database.not_replay_predicate())
                .order_by(desc(DetectionRow.started_at))
                .limit(25)
            ))

            per_hour = dict(s.execute(
                select(
                    func.strftime("%H", DetectionRow.started_at),
                    func.count(DetectionRow.id),
                )
                .where(DetectionRow.source_name == name)
                .group_by(func.strftime("%H", DetectionRow.started_at))
            ).all())

            first_seen, last_seen, distinct_species = s.execute(
                select(
                    func.min(DetectionRow.started_at),
                    func.max(DetectionRow.started_at),
                    func.count(func.distinct(DetectionRow.scientific_name)),
                ).where(DetectionRow.source_name == name)
            ).one()

        # Render the hourly histogram in the source's local timezone so the
        # bar chart matches what the operator hears wall-clock at the site.
        tz_name = src_cfg.timezone if src_cfg else "UTC"
        local_tz = _zone_info(tz_name)
        # SQLite gave us UTC hour strings; convert each utc hour to the
        # corresponding local hour. For most timezones this is a fixed offset
        # so a single shift works.
        offset = int(datetime.now(local_tz).utcoffset().total_seconds() // 3600)
        hours = [0] * 24
        for hr_str, c in per_hour.items():
            if hr_str is None:
                continue
            local_hr = (int(hr_str) + offset) % 24
            hours[local_hr] += int(c)
        peak_hour = max(hours) or 1

        # Live video embed: derived from the source's url + kind. video_id is
        # None for non-YouTube sources, in which case the template hides the
        # iframe and shows the open-original link instead.
        video_id = (
            _youtube_video_id(src_cfg.url)
            if src_cfg and src_cfg.kind == "youtube" else None
        )

        # Downtime data for the uptime panel: how long this site has been
        # down right now (None = up), how much in the last 24h / 7 days, and
        # the last 5 closed outages with their cause.
        now_utc = datetime.now(UTC)
        cur_down = db.current_downtime_by_source().get(name)
        cur_started = cur_down.started_at if cur_down else None
        if cur_started is not None and cur_started.tzinfo is None:
            cur_started = cur_started.replace(tzinfo=UTC)
        downtime = {
            "current_outage_s": (
                int((now_utc - cur_started).total_seconds()) if cur_started else None
            ),
            "current_reason": cur_down.reason if cur_down else None,
            "down_24h_s": db.downtime_seconds_since(name, now_utc - timedelta(hours=24)),
            "down_7d_s": db.downtime_seconds_since(name, now_utc - timedelta(days=7)),
            "recent": db.recent_downtime(name, limit=5),
        }

        # Liveness for the header health chip: the worker's heartbeat status,
        # how long since it beat, and its last error. Combined client-side
        # with the downtime + audio-quality data already loaded so the chip's
        # hover tooltip can show the full operational picture in one place.
        site_hb = {h.source_name: h for h in db.list_worker_heartbeats()}.get(name)
        hb_status, hb_since_s, _hb_state, hb_error = _hb_status(site_hb, now_utc)
        health = {
            "status": hb_status,
            "since_s": hb_since_s,
            "last_error": hb_error,
        }

        # Notable-day anomalies for the panel between the AI narrative and
        # hourly activity. 30-day lookback is enough to catch a migration
        # event without burying recent ones under stale entries.
        anomalies = db.list_recent_anomalies(name, days=30)

        # Current local time + weather for the site header. Both come from
        # cached upstreams (Open-Meteo TTL = 1h) so this adds no real cost
        # to the page render. Falls back to None when coords/tz missing or
        # the API call fails — the template hides the line gracefully.
        current_weather = None
        if src_cfg and src_cfg.lat is not None and src_cfg.lon is not None:
            current_weather = weather_module.current_weather_at(
                src_cfg.lat, src_cfg.lon, tz_name
            )
        local_now = datetime.now(local_tz).strftime("%H:%M")
        tz_abbr = _tz_abbr(tz_name)

        # Audio-quality: current readout + 24h trend sparkline.
        aq_now = datetime.now(UTC)
        aq_row = db.audio_quality_by_source().get(name)
        audio_quality = None
        if aq_row is not None:
            upd = aq_row.updated_at
            if upd.tzinfo is None:
                upd = upd.replace(tzinfo=UTC)
            yld = aq_row.structure_score                      # repurposed: yield score
            clip_pen = max(0.3, min(1.0, 1.0 - aq_row.clip_fraction / 0.05))
            yld_gate = max(0.0, min(1.0, yld / 0.30))
            bh = aq_row.band_hz_high
            band_pen = (
                1.0 if (bh is None or bh >= 10000)
                else max(0.6, min(1.0, 0.6 + 0.4 * (bh - 4000) / 6000))
            )
            det_6h = db.detection_count_since(name, aq_now - timedelta(hours=6))
            audio_quality = {
                "score": aq_row.score,
                "issue": aq_row.issue_label,
                "level_dbfs": aq_row.level_dbfs,
                "structure": yld,
                "stale": (aq_now - upd).total_seconds() > 180,
                # breakdown of the score calculation, for the click-through chart
                "level_score": aq_row.level_score,
                "avail_score": aq_row.avail_score,
                "yield_score": yld,
                "silence_pct": round(aq_row.silence_fraction * 100),
                "clip_pct": round(aq_row.clip_fraction * 100, 2),
                "det_per_h": round(det_6h / 6.0, 1),
                "clip_penalty": round(clip_pen, 2),
                "yield_gate": round(yld_gate, 2),
                "band_penalty": round(band_pen, 2),
                "w_level": round(0.35 * aq_row.level_score, 3),
                "w_avail": round(0.25 * aq_row.avail_score, 3),
                "w_yield": round(0.40 * yld, 3),
                "weighted_sum": round(
                    0.35 * aq_row.level_score + 0.25 * aq_row.avail_score + 0.40 * yld, 3),
                # Frequency band (Hz). band_limited = upper edge well below the
                # ~16 kHz YouTube codec ceiling → mic/source limitation.
                "band_hz_low": aq_row.band_hz_low,
                "band_hz_high": aq_row.band_hz_high,
                "band_limited": (
                    aq_row.band_hz_high is not None and aq_row.band_hz_high < 9000
                ),
            }
        aq_samples = db.audio_quality_samples_since(name, aq_now - timedelta(hours=24))
        audio_sparkline = ""
        if len(aq_samples) >= 2:
            t0 = aq_samples[0][0]
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=UTC)
            span = max((aq_samples[-1][0].replace(tzinfo=UTC) if aq_samples[-1][0].tzinfo is None
                        else aq_samples[-1][0]) .timestamp() - t0.timestamp(), 1.0)
            pts = []
            for ts, sc in aq_samples:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                x = (ts.timestamp() - t0.timestamp()) / span * 100.0
                y = 30.0 - (max(0, min(100, sc)) / 100.0) * 30.0
                pts.append(f"{x:.1f},{y:.1f}")
            audio_sparkline = " ".join(pts)
        # Activity clock: the same radial dial the dashboard map used to
        # open in a modal, now surfaced inline on the site page. Computed
        # here because the simple bar histogram it replaces only needed the
        # `hours` array; the dial also wants solar bands + the current hour.
        # `local_now` above is a formatted string, so derive a fresh dt.
        dial_now = datetime.now(local_tz)
        dial_bands = (
            _solar_bands(dial_now.date(), src_cfg.lat, src_cfg.lon, local_tz)
            if src_cfg and src_cfg.lat is not None and src_cfg.lon is not None
            else []
        )
        dial_svg = _radial_dial_svg(
            hours, highlight=dial_now.hour, bands=dial_bands,
            compact=False, interactive=True,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "site_detail.html",
            {
                "name": name,
                "dial_svg": dial_svg,
                "src_cfg": src_cfg,
                "source_color": SOURCE_COLORS.get(name, "#10b981"),
                "video_id": video_id,
                "audio_quality": audio_quality,
                "audio_sparkline": audio_sparkline,
                "health": health,
                "multisite": bool(src_cfg and src_cfg.multisite),
                "note": site_note,
                "total": total,
                "distinct_species": distinct_species or 0,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "top_species": top_species,
                "recent_unique": recent_unique,
                "recent": recent,
                "downtime": downtime,
                "hours": hours,
                "peak_hour": peak_hour,
                "tz_name": tz_name,
                "source_tz": {n: c.timezone for n, c in sources_by_name.items()},
                "anomalies": anomalies,
                "current_weather": current_weather,
                "local_now": local_now,
                "tz_abbr": tz_abbr,
                **_note_tag_context(),
            },
        )

    @app.get("/briefs", response_class=HTMLResponse)
    def briefs_index(request: Request) -> HTMLResponse:
        """Archive page: every generated daily brief, newest first."""
        briefs = db.list_daily_briefs(limit=180)
        return TEMPLATES.TemplateResponse(
            request,
            "briefs.html",
            {"briefs": briefs},
        )

    def _admin_view() -> dict:
        """Build the per-source admin payload: status + knobs + heartbeat info.
        Lists every source we know about — file-defined (sources.toml), live
        runtime, soft-deleted runtime, AND disabled-via-override — so the
        operator can toggle any of them from /admin."""
        disabled = db.list_disabled_source_names()
        runtime_rows = db.list_runtime_sources(include_deleted=True)
        runtime_by_name = {r.name: r for r in runtime_rows}
        heartbeats = {h.source_name: h for h in db.list_worker_heartbeats()}
        current_downs = db.current_downtime_by_source()

        now = datetime.now(UTC)
        last_24h = now - timedelta(hours=24)
        rows: list[dict] = []
        seen: set[str] = set()

        def _down_extras(name: str) -> tuple[int | None, int]:
            cur = current_downs.get(name)
            cur_started = cur.started_at if cur else None
            if cur_started is not None and cur_started.tzinfo is None:
                cur_started = cur_started.replace(tzinfo=UTC)
            current_outage_s = (
                int((now - cur_started).total_seconds()) if cur_started else None
            )
            return current_outage_s, db.downtime_seconds_since(name, last_24h)

        # Static sources first — they're the stable backbone. A live runtime
        # row with the same name overrides the config (existing "runtime wins"
        # merge); a soft-deleted runtime row of the same name is ignored.
        for static in static_sources:
            seen.add(static.name)
            rt = runtime_by_name.get(static.name)
            cfg_src = (
                _runtime_to_cfg(rt) if (rt and rt.deleted_at is None) else static
            )
            hb = heartbeats.get(static.name)
            cur_s, day_s = _down_extras(static.name)
            # Static-source disabled-ness lives only in the override table.
            rows.append(_admin_row(
                cfg_src, rt, hb, deleted=(static.name in disabled), now=now,
                current_outage_s=cur_s, down_today_s=day_s,
            ))

        # Runtime-only sources (no static counterpart) — soft-deleted ones
        # are still listed so they can be re-enabled. Disabled by either the
        # soft-delete OR the override set.
        for rt in runtime_rows:
            if rt.name in seen:
                continue
            cfg_src = _runtime_to_cfg(rt)
            hb = heartbeats.get(rt.name)
            is_disabled = rt.deleted_at is not None or rt.name in disabled
            cur_s, day_s = _down_extras(rt.name)
            rows.append(_admin_row(
                cfg_src, rt, hb, deleted=is_disabled, now=now,
                current_outage_s=cur_s, down_today_s=day_s,
            ))

        rows.sort(key=lambda r: r["name"].lower())
        # Layer per-source cutoff overrides over each source's configured value
        # so the table and the cutoffs panel both show the value the pipeline
        # actually applies. ``min_conf_base`` keeps the configured fallback for
        # the editor's placeholder.
        source_overrides = db.source_min_confidence_map()
        for r in rows:
            r["min_conf_base"] = r["min_confidence"]
            override = source_overrides.get(r["name"])
            r["min_conf_overridden"] = override is not None
            if override is not None:
                r["min_confidence"] = override
        # Per-source highlight-gating on/off (toggled from the admin table).
        gate_on = {
            key[len("gate_highlights:"):]
            for key, val in db.list_settings_with_prefix("gate_highlights:").items()
            if val
        }
        for r in rows:
            # Only YouTube cams can carry the highlight banner / be watched.
            r["gate_supported"] = r["kind"] == "youtube"
            r["gate_highlights"] = r["name"] in gate_on
        # Live-vs-highlight playback state for monitored sources. A reading is
        # only trusted when fresh (the watcher checks every ~2 min); a stale
        # checked_at means the watcher isn't running, so show nothing.
        playback = db.playback_state_by_source()
        PLAYBACK_STALE_S = 360
        for r in rows:
            ps = playback.get(r["name"])
            r["playback"] = None
            r["playback_since"] = None
            r["highlight_secs_24h"] = 0
            if ps is not None:
                checked = ps.checked_at
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=UTC)
                if (now - checked).total_seconds() <= PLAYBACK_STALE_S:
                    r["playback"] = "highlight" if ps.in_highlight else "live"
                    since = ps.since
                    if since.tzinfo is None:
                        since = since.replace(tzinfo=UTC)
                    r["playback_since"] = since
                    r["highlight_secs_24h"] = db.highlight_seconds_since(
                        r["name"], last_24h
                    )
        # Audio-quality metric. Trust it only when fresh (the worker flushes
        # ~every 60s); a stale row means the worker isn't running, so grey it.
        audio_q = db.audio_quality_by_source()
        AUDIO_STALE_S = 180
        for r in rows:
            aq = audio_q.get(r["name"])
            r["audio_score"] = None
            r["audio_issue"] = None
            r["audio_stale"] = False
            if aq is not None:
                upd = aq.updated_at
                if upd.tzinfo is None:
                    upd = upd.replace(tzinfo=UTC)
                r["audio_score"] = aq.score
                r["audio_issue"] = aq.issue_label
                r["audio_stale"] = (now - upd).total_seconds() > AUDIO_STALE_S
        running = sum(1 for r in rows if r["status"] == "running")
        # Defaults for the add-source form: pick the most common timezone
        # already in use so the user doesn't have to retype it. Fall back to
        # UTC if no source has a sensible value yet.
        from collections import Counter
        from zoneinfo import available_timezones
        tz_counter = Counter(
            r["timezone"] for r in rows if r["timezone"] and r["timezone"] != "UTC"
        )
        default_tz = tz_counter.most_common(1)[0][0] if tz_counter else "UTC"

        # Timezone dropdown options. Every Africa/* zone (the project's
        # primary geography) plus a handful of common non-African zones,
        # grouped so the select renders as optgroups.
        africa_zones = sorted(
            tz for tz in available_timezones() if tz.startswith("Africa/")
        )
        common_zones = [
            "UTC",
            "Europe/London",
            "Europe/Berlin",
            "Europe/Paris",
            "America/New_York",
            "America/Los_Angeles",
            "America/Sao_Paulo",
            "Asia/Tokyo",
            "Asia/Singapore",
            "Australia/Sydney",
        ]
        tz_options = [
            {"group": "Common", "zones": common_zones},
            {"group": "Africa", "zones": africa_zones},
        ]

        # Existing sites with coords, for the map picker.
        existing_sites = [
            {"name": r["name"], "lat": r["lat"], "lon": r["lon"]}
            for r in rows
            if r["lat"] is not None and r["lon"] is not None
        ]
        # Suggested default location for a new source: centroid of existing
        # sites if any, otherwise a sensible southern-Africa fallback. The
        # form drops a pin here on page load so submit-without-clicking-the-
        # map works, but the user can move it freely.
        if existing_sites:
            default_lat = sum(s["lat"] for s in existing_sites) / len(existing_sites)
            default_lon = sum(s["lon"] for s in existing_sites) / len(existing_sites)
        else:
            default_lat, default_lon = -24.0, 31.0

        return {
            "rows": rows,
            "running": running,
            "total": len(rows),
            "now": now,
            "global_min_confidence": db.global_min_confidence(),
            "default_tz": default_tz,
            "tz_options": tz_options,
            "existing_sites": existing_sites,
            "default_lat": round(default_lat, 6),
            "default_lon": round(default_lon, 6),
        }

    def _hb_status(
        heartbeat: WorkerHeartbeatRow | None, now: datetime
    ) -> tuple[str, int | None, str | None, str | None]:
        """Derive a worker's health from its heartbeat row.
        Returns (status, seconds_since_heartbeat, raw_state, last_error)."""
        if heartbeat is None:
            return "never", None, None, None
        hb_at = heartbeat.last_heartbeat_at
        if hb_at.tzinfo is None:
            hb_at = hb_at.replace(tzinfo=UTC)
        since_s = max(0, int((now - hb_at).total_seconds()))
        state = heartbeat.state
        error = heartbeat.last_error
        if state == "running" and since_s < 60:
            status = "running"
        elif state == "backoff":
            status = "backoff"
        elif state == "stopped":
            status = "stopped"
        else:
            status = "stale"
        return status, since_s, state, error

    def _site_health_map() -> dict[str, dict]:
        """Per-source operational health for the Sites index: liveness status,
        current outage, 24h downtime, and audio-quality score. Composes the same
        per-source maps /admin uses (heartbeats, downtime, audio quality) — keyed
        by source name. Sites absent from a given map just lack that signal."""
        now = datetime.now(UTC)
        last_24h = now - timedelta(hours=24)
        heartbeats = {h.source_name: h for h in db.list_worker_heartbeats()}
        current_downs = db.current_downtime_by_source()
        audio_q = db.audio_quality_by_source()
        AUDIO_STALE_S = 180
        out: dict[str, dict] = {}
        for name in set(heartbeats) | set(current_downs) | set(audio_q):
            status, since_s, _state, _err = _hb_status(heartbeats.get(name), now)
            cur = current_downs.get(name)
            cur_started = cur.started_at if cur else None
            if cur_started is not None and cur_started.tzinfo is None:
                cur_started = cur_started.replace(tzinfo=UTC)
            current_outage_s = (
                int((now - cur_started).total_seconds()) if cur_started else None
            )
            aq = audio_q.get(name)
            audio_score = None
            audio_issue = None
            audio_stale = False
            if aq is not None:
                upd = aq.updated_at
                if upd.tzinfo is None:
                    upd = upd.replace(tzinfo=UTC)
                audio_score = aq.score
                audio_issue = aq.issue_label
                audio_stale = (now - upd).total_seconds() > AUDIO_STALE_S
            out[name] = {
                "status": status,
                "since_s": since_s,
                "current_outage_s": current_outage_s,
                "down_24h_s": db.downtime_seconds_since(name, last_24h),
                "audio_score": audio_score,
                "audio_issue": audio_issue,
                "audio_stale": audio_stale,
            }
        return out

    def _admin_row(
        cfg_src: SourceConfig,
        runtime_row,
        heartbeat: WorkerHeartbeatRow | None,
        *,
        deleted: bool,
        now: datetime,
        current_outage_s: int | None = None,
        down_today_s: int = 0,
    ) -> dict:
        is_runtime = runtime_row is not None
        if deleted:
            status, since_s, state, error = "disabled", None, None, None
        else:
            status, since_s, state, error = _hb_status(heartbeat, now)
        return {
            "name": cfg_src.name,
            "kind": cfg_src.kind,
            "url": cfg_src.url,
            "lat": cfg_src.lat,
            "lon": cfg_src.lon,
            "min_confidence": cfg_src.min_confidence,
            "week": cfg_src.week,
            "multisite": cfg_src.multisite,
            "timezone": cfg_src.timezone,
            "is_runtime": is_runtime,
            "deleted": deleted,
            "status": status,
            "state": state,
            "since_s": since_s,
            "error": error,
            "current_outage_s": current_outage_s,
            "down_today_s": down_today_s,
        }

    _silence_base_cache: dict = {"ts": 0.0, "val": None}
    _silence_watch_cache: dict = {"ts": 0.0, "val": None}
    _silence_lock = threading.Lock()

    def _silence_baselines() -> dict[str, float]:
        """Per source, the longest silence it normally goes through in a day.

        Median over ``_SILENCE_BASELINE_DAYS`` of each day's largest gap between
        consecutive detections. Sources with fewer than ``_SILENCE_MIN_DAYS`` of
        history are left out — a source that has barely run has no habits to be
        judged against, and guessing produces exactly the false alarm that makes
        people stop reading alerts.

        A window function over two weeks of rows, so it is cached for an hour.
        """
        rows = None
        with db.session() as s:
            rows = s.execute(
                text(
                    """
                    WITH g AS (
                      SELECT source_name, started_at,
                             (julianday(started_at) - julianday(
                                LAG(started_at) OVER (
                                  PARTITION BY source_name ORDER BY started_at)
                             )) * 86400.0 AS gap
                      FROM detections
                      WHERE started_at >= :since
                    )
                    SELECT source_name, date(started_at) AS d, MAX(gap) AS worst
                    FROM g WHERE gap IS NOT NULL
                    GROUP BY source_name, d
                    """
                ),
                {
                    "since": (
                        datetime.now(UTC) - timedelta(days=_SILENCE_BASELINE_DAYS)
                    ).strftime("%Y-%m-%d %H:%M:%S")
                },
            ).all()
        per_source: dict[str, list[float]] = {}
        for src, _d, worst in rows:
            if worst is not None:
                per_source.setdefault(src, []).append(float(worst))
        return {
            src: statistics.median(v)
            for src, v in per_source.items()
            if len(v) >= _SILENCE_MIN_DAYS
        }

    def _silence_watch() -> dict[str, dict]:
        """Per source: how long since its last detection, and how long it is
        allowed to be quiet before that counts as deaf.

        Returns ``{name: {"silent_s": float, "threshold_s": float}}``, only for
        sources that have a baseline. The caller decides what to do with it —
        this says nothing about whether the worker is running.
        """
        now_m = time.monotonic()
        cached = _silence_watch_cache["val"]
        if cached is not None and now_m - _silence_watch_cache["ts"] <= _SILENCE_WATCH_TTL:
            return cached
        with _silence_lock:
            now_m = time.monotonic()
            cached = _silence_watch_cache["val"]
            if cached is not None and now_m - _silence_watch_cache["ts"] <= _SILENCE_WATCH_TTL:
                return cached
            base = _silence_base_cache["val"]
            if base is None or now_m - _silence_base_cache["ts"] > _SILENCE_BASELINE_TTL:
                base = _silence_baselines()
                _silence_base_cache.update(ts=time.monotonic(), val=base)
            now = datetime.now(UTC)
            with db.session() as s:
                last_by_src = dict(
                    s.execute(
                        select(
                            DetectionRow.source_name,
                            func.max(DetectionRow.started_at),
                        ).group_by(DetectionRow.source_name)
                    ).all()
                )
            out: dict[str, dict] = {}
            for src, typical in base.items():
                last = last_by_src.get(src)
                if last is None:
                    continue
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                out[src] = {
                    "silent_s": max(0.0, (now - last).total_seconds()),
                    "threshold_s": max(_SILENCE_FLOOR_S, _SILENCE_FACTOR * typical),
                }
            _silence_watch_cache.update(ts=time.monotonic(), val=out)
            return out

    def _deaf_sources(running: list[str]) -> list[dict]:
        """Of the sources whose worker claims to be running, the ones that have
        gone quiet for far longer than they ever normally do.

        Kept apart from the "not running" list on purpose. Those sources admit
        they are down and show up on every other panel; this is the opposite
        failure — a source insisting it is fine while producing nothing — and
        folding the two together would bury the one you cannot see any other
        way.
        """
        silence = _silence_watch()
        out = [
            {
                "name": name,
                "silent_s": int(q["silent_s"]),
                "threshold_s": int(q["threshold_s"]),
            }
            for name in running
            if (q := silence.get(name)) and q["silent_s"] > q["threshold_s"]
        ]
        out.sort(key=lambda d: -d["silent_s"])
        return out

    def _health_view() -> dict:
        """Raspberry Pi host health for the admin page: CPU load, memory, SoC
        temperature, throttling/under-voltage, uptime and storage headroom —
        plus worker liveness and detection freshness as data-flow signals."""
        now = datetime.now(UTC)
        ordered, _, _ = _all_sources()
        heartbeats = {h.source_name: h for h in db.list_worker_heartbeats()}
        workers_running = 0
        problems: list[dict] = []
        running: list[str] = []
        for cfg_src in ordered:
            status, since_s, _, error = _hb_status(heartbeats.get(cfg_src.name), now)
            if status == "running":
                workers_running += 1
                running.append(cfg_src.name)
            else:
                problems.append(
                    {"name": cfg_src.name, "status": status, "since_s": since_s, "error": error}
                )
        deaf = _deaf_sources(running)

        with db.session() as s:
            last_det = s.execute(select(func.max(DetectionRow.started_at))).scalar()
            det_24h = s.execute(
                select(func.count(DetectionRow.id)).where(
                    DetectionRow.started_at >= now - timedelta(hours=24)
                )
            ).scalar() or 0

        last_det_age_s = None
        if last_det is not None:
            if last_det.tzinfo is None:
                last_det = last_det.replace(tzinfo=UTC)
            last_det_age_s = max(0, int((now - last_det).total_seconds()))

        # Storage headroom for the filesystem holding the clips + DB (O(1) — no
        # tree walk) plus the SQLite file size.
        try:
            target = clips_root if clips_root.exists() else clips_root.parent
            du = shutil.disk_usage(target)
            disk = {"total": du.total, "used": du.used, "free": du.free}
        except OSError:
            disk = None
        db_path = Path(cfg.db_url.removeprefix("sqlite:///"))
        db_bytes = db_path.stat().st_size if db_path.exists() else None

        # Serialized-detector saturation: all workers share one locked
        # BirdNetDetector, so the ceiling is chunk_seconds / inference_time.
        chunk_s = cfg.chunk_seconds or 3.0
        inf_ms = cfg.inference_ms_estimate or 110.0
        infer_pct = round(workers_running * inf_ms / (chunk_s * 1000) * 100)
        infer_max = int(chunk_s * 1000 / inf_ms)

        # Notes / AI-commentary worker liveness. last_error is the authoritative
        # failure signal (set by the worker on any failing tick, "<iso>|<msg>");
        # brief freshness is the user-visible "reports flowing" signal. A stalled
        # worker (e.g. exhausted API credits) once went unnoticed for ~10 days —
        # this makes it a red card + banner on /admin.
        def _iso(v: str | None) -> datetime | None:
            if not v:
                return None
            try:
                return datetime.fromisoformat(v)
            except ValueError:
                return None

        notes_last_ok = _iso(db.get_setting("notes_worker_last_ok"))
        err_raw = db.get_setting("notes_worker_last_error")
        err_at = err_msg = None
        if err_raw and "|" in err_raw:
            ts, _, err_msg = err_raw.partition("|")
            err_at = _iso(ts)
        # A stale error older than the last clean tick means it already
        # recovered — only flag when the error is the worker's latest word.
        notes_failing = bool(
            err_at and (notes_last_ok is None or err_at > notes_last_ok)
        )
        latest_brief = db.get_latest_daily_brief()
        brief_date = latest_brief.date_utc if latest_brief else None
        # A brief for UTC date D is generated just after D ends, so "yesterday"
        # (age 1) is the freshest normal state; ≥2 days behind means a gap.
        brief_age_days = (now.date() - brief_date).days if brief_date else None

        return {
            "health": {
                "now": now,
                "host": host_metrics(),
                "disk": disk,
                "db_bytes": db_bytes,
                "workers_running": workers_running,
                "workers_total": len(ordered),
                "worker_problems": problems,
                "deaf_sources": deaf,
                "last_det_age_s": last_det_age_s,
                "det_24h": det_24h,
                "infer_pct": infer_pct,
                "infer_max": infer_max,
                "infer_ms": round(inf_ms),
                "notes": {
                    "failing": notes_failing,
                    "error_msg": err_msg if notes_failing else None,
                    "error_age_s": (
                        max(0, int((now - err_at).total_seconds()))
                        if notes_failing and err_at else None
                    ),
                    "brief_date": brief_date.isoformat() if brief_date else None,
                    "brief_age_days": brief_age_days,
                },
            }
        }

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request) -> HTMLResponse:
        # Opportunistic retention prune (operator views /admin only now and then).
        db.prune_pageviews(90)
        return TEMPLATES.TemplateResponse(
            request,
            "admin.html",
            {
                **_admin_view(), **_species_cutoffs_ctx(), **_health_view(),
                "visitors": db.pageview_stats(),
            },
        )

    # Ecologically-implausible marine/coastal/pelagic species — the FP-suggestion
    # basis for /admin/suppressions. Over-inclusive is harmless: only species
    # actually detected here surface. (See the marine-FP root-cause analysis.)
    _MARINE_FP_CANDIDATES = [
        # Coastal/passage waders & terns
        "Pluvialis squatarola", "Numenius phaeopus", "Numenius arquata",
        "Thalasseus sandvicensis", "Sterna hirundo", "Sterna paradisaea",
        "Arenaria interpres", "Phalacrocorax carbo", "Limosa limosa",
        "Limosa lapponica", "Calidris alba", "Calidris canutus", "Calidris alpina",
        "Haematopus ostralegus", "Larus argentatus", "Larus canus",
        "Chroicocephalus ridibundus",
        # Pelagic seabirds — impossible inland (albatross/shearwater/petrel/gannet/skua)
        "Calonectris diomedea", "Puffinus puffinus", "Hydrobates leucorhous",
        "Morus bassanus", "Fulmarus glacialis", "Stercorarius parasiticus",
        "Stercorarius skua",
    ]

    def _suppressions_ctx() -> dict:
        rules = db.list_species_suppressions()
        active = {(r["source_name"], r["scientific_name"]) for r in rules}
        suggestions = [
            s for s in db.species_site_hi_counts(_MARINE_FP_CANDIDATES)
            if (s["source_name"], s["scientific_name"]) not in active
        ][:25]
        return {
            # Split for the template: all-sites ("*") rules vs per-site rules.
            "global_rules": [r for r in rules if r["source_name"] == ALL_SITES_SENTINEL],
            "site_rules": [r for r in rules if r["source_name"] != ALL_SITES_SENTINEL],
            "all_sites": ALL_SITES_SENTINEL,
            "suggestions": suggestions,
            "sources": sorted(_all_sources()[1].keys()),
        }

    @app.get("/admin/suppressions", response_class=HTMLResponse)
    def admin_suppressions(request: Request) -> HTMLResponse:
        """Manage per-site false-positive suppression rules. The pipeline polls
        these on its 60s cadence, so a rule lands within a minute, no restart."""
        return TEMPLATES.TemplateResponse(
            request, "admin_suppressions.html", _suppressions_ctx()
        )

    @app.post("/api/suppressions")
    async def add_suppression(
        request: Request,
        source_name: str = Form(...),
        scientific_name: str = Form(...),
        common_name: str = Form(default=""),
        note: str = Form(default=""),
    ) -> RedirectResponse:
        if source_name.strip() and scientific_name.strip():
            db.add_species_suppression(
                source_name.strip(), scientific_name.strip(),
                common_name.strip() or None, created_by="admin",
                note=note.strip() or None,
            )
        return RedirectResponse("/admin/suppressions", status_code=303)

    @app.post("/api/suppressions/delete")
    async def delete_suppression(
        request: Request,
        source_name: str = Form(...),
        scientific_name: str = Form(...),
    ) -> RedirectResponse:
        db.remove_species_suppression(source_name.strip(), scientific_name.strip())
        return RedirectResponse("/admin/suppressions", status_code=303)

    @app.post("/api/suppressions/reactivate-all")
    async def reactivate_suppression_everywhere(
        request: Request,
        scientific_name: str = Form(...),
    ) -> RedirectResponse:
        """Fully re-enable a species: drop ALL its rules (all-sites + per-site)."""
        db.remove_species_everywhere(scientific_name.strip())
        return RedirectResponse("/admin/suppressions", status_code=303)

    @app.get("/admin/replays", response_class=HTMLResponse)
    def admin_replays(
        request: Request,
        days: int = Query(default=30, ge=1, le=365),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> HTMLResponse:
        """Audit page: lists hash-groups that appear at multiple distinct
        timestamps at the same source in the lookback window. Each group is
        a candidate ad/highlight loop that the replay filter is hiding from
        every other detection feed.

        Multi-species-within-one-chunk groups (BirdNET firing multiple
        species labels on a single audio buffer) are excluded: those share
        a single ``started_at`` and aren't replays of anything — they're
        the legitimate parallel output of one detection event.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        with db.session() as s:
            # True replays: same (source, hash) appearing at ≥2 distinct
            # started_at values. ``n`` is total detection rows sharing the
            # hash; ``n_times`` is the number of replay events (≥ 2);
            # ``hidden`` is how many rows the replay filter actually drops.
            groups = list(s.execute(
                select(
                    DetectionRow.source_name,
                    DetectionRow.audio_hash,
                    func.count(DetectionRow.id).label("n"),
                    func.count(func.distinct(DetectionRow.started_at)).label("n_times"),
                    func.min(DetectionRow.started_at).label("first_seen"),
                    func.max(DetectionRow.started_at).label("last_seen"),
                    func.count(func.distinct(DetectionRow.scientific_name)).label("n_species"),
                )
                .where(DetectionRow.audio_hash.is_not(None))
                .where(DetectionRow.started_at >= cutoff)
                .group_by(DetectionRow.source_name, DetectionRow.audio_hash)
                .having(func.count(func.distinct(DetectionRow.started_at)) >= 2)
                .order_by(desc("n_times"), desc("n"))
                .limit(limit)
            ).all())

            # For each group, pull a representative detection id (highest
            # confidence with a clip) so the audit page can offer a play
            # button without a second round-trip.
            samples: dict[tuple[str, str], int] = {}
            top_species: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
            for g in groups:
                key = (g.source_name, g.audio_hash)
                rep = s.scalar(
                    select(DetectionRow.id)
                    .where(DetectionRow.source_name == g.source_name)
                    .where(DetectionRow.audio_hash == g.audio_hash)
                    .where(DetectionRow.clip_path.is_not(None))
                    .order_by(desc(DetectionRow.confidence))
                    .limit(1)
                )
                if rep is not None:
                    samples[key] = rep
                # Which species BirdNET flagged within this hash-group — a
                # true ad-loop often makes BirdNET cycle through 3-5
                # different "species" depending on jitter, so seeing the
                # mix is itself a signal that this is replayed audio.
                top_species[key] = list(s.execute(
                    select(
                        DetectionRow.common_name,
                        DetectionRow.scientific_name,
                        func.count(DetectionRow.id).label("c"),
                    )
                    .where(DetectionRow.source_name == g.source_name)
                    .where(DetectionRow.audio_hash == g.audio_hash)
                    .group_by(DetectionRow.scientific_name, DetectionRow.common_name)
                    .order_by(desc("c"))
                    .limit(5)
                ).all())

            # Headline numbers for the page banner.
            total = s.scalar(
                select(func.count(DetectionRow.id))
                .where(DetectionRow.audio_hash.is_not(None))
                .where(DetectionRow.started_at >= cutoff)
            ) or 0
            hidden_subq = (
                select(func.count(DetectionRow.id))
                .where(DetectionRow.audio_hash.is_not(None))
                .where(DetectionRow.started_at >= cutoff)
                .where(~Database.not_replay_predicate())
            )
            hidden = s.scalar(hidden_subq) or 0

        rows = [
            {
                "source_name": g.source_name,
                "audio_hash": g.audio_hash,
                # 16-char prefix is unique in practice; 12 was prone to
                # birthday collisions on busy sources (the audio_hash
                # high-bits are biased toward 0/f by the mel-band ordering).
                "hash_short": (g.audio_hash or "")[:16],
                "n": int(g.n),
                "n_times": int(g.n_times),
                "replays": int(g.n_times) - 1,
                "first_seen": g.first_seen,
                "last_seen": g.last_seen,
                "n_species": int(g.n_species),
                "sample_detection_id": samples.get((g.source_name, g.audio_hash)),
                "top_species": [
                    {"common": c, "scientific": sc, "count": int(n)}
                    for c, sc, n in top_species.get((g.source_name, g.audio_hash), [])
                ],
            }
            for g in groups
        ]
        return TEMPLATES.TemplateResponse(
            request,
            "admin_replays.html",
            {
                "rows": rows,
                "days": days,
                "limit": limit,
                "total_hashed": total,
                "total_hidden": hidden,
            },
        )

    @app.get("/partials/admin", response_class=HTMLResponse)
    def admin_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "_admin_table.html", _admin_view()
        )

    @app.get("/partials/health", response_class=HTMLResponse)
    def health_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "_system_health.html", _health_view()
        )

    @app.get("/scoreboard")
    def scoreboard_redirect(request: Request) -> RedirectResponse:
        """Retired — the per-site rollup is now the richer /sites page. Keep the
        old URL (and any bookmarks/links) working."""
        q = request.url.query
        return RedirectResponse("/sites" + (("?" + q) if q else ""), status_code=307)

    @app.get("/rollup")
    def rollup_redirect(request: Request) -> RedirectResponse:
        """Even older name for the site rollup — also lands on /sites."""
        q = request.url.query
        return RedirectResponse("/sites" + (("?" + q) if q else ""), status_code=307)

    @app.get("/sites", response_class=HTMLResponse)
    def sites_index(
        request: Request,
        hours: int = Query(default=24, ge=0, le=8760),  # 0 = all-time, up to 1y
    ) -> HTMLResponse:
        """Sites index (WIP alternative to /scoreboard): one compact row per
        site combining activity (species/detections/sparkline/last-heard) with
        the operational-health signals that otherwise live only on each detail
        page (liveness, 24h uptime, audio-quality). Currently-down sites are
        included even with no detections in-window so outages surface."""
        from sqlalchemy import Integer
        from sqlalchemy import cast as sa_cast

        _, sources_by_name, _ = _all_sources()
        now = datetime.now(UTC)
        # Inline sparkline: fixed 40 bars across whatever window is selected.
        n_spark = 40
        with db.session() as s:
            if hours <= 0:  # all-time → back to the first detection on record
                first = s.scalar(select(func.min(DetectionRow.started_at)))
                since = (
                    (first.replace(tzinfo=UTC) if first.tzinfo is None else first)
                    if first is not None else now
                )
            else:
                since = now - timedelta(hours=hours)
            window_s = max(60.0, (now - since).total_seconds())
            since_epoch = since.timestamp()
            spark_s = window_s / n_spark
            # Per-source totals — SQL aggregation, no row loading, so long
            # windows (6m/1y/all) stay cheap.
            agg = {
                row[0]: (int(row[1]), int(row[2]), row[3])
                for row in s.execute(
                    select(
                        DetectionRow.source_name,
                        func.count(DetectionRow.id),
                        func.count(func.distinct(DetectionRow.scientific_name)),
                        func.max(DetectionRow.started_at),
                    )
                    .where(DetectionRow.started_at >= since)
                    .group_by(DetectionRow.source_name)
                ).all()
            }
            # Per-source sparkline buckets, also via GROUP BY (strftime → epoch).
            bucket_col = sa_cast(
                (func.strftime("%s", DetectionRow.started_at) - since_epoch) / spark_s,
                Integer,
            )
            # Both metrics per bucket: detection count and distinct-species count.
            det_spark: dict[str, list[int]] = {}
            sp_spark: dict[str, list[int]] = {}
            for src, bk, dcnt, scnt in s.execute(
                select(
                    DetectionRow.source_name,
                    bucket_col,
                    func.count(),
                    func.count(func.distinct(DetectionRow.scientific_name)),
                )
                .where(DetectionRow.started_at >= since)
                .group_by(DetectionRow.source_name, bucket_col)
            ).all():
                if bk is None or bk < 0:
                    continue
                i = min(int(bk), n_spark - 1)
                det_spark.setdefault(src, [0] * n_spark)[i] += int(dcnt)
                sp_spark.setdefault(src, [0] * n_spark)[i] += int(scnt)

        total = sum(v[0] for v in agg.values())
        health = _site_health_map()
        # Full live roster: every configured source (online or offline). Admin-
        # disabled sources are already dropped by _all_sources(), so the list is
        # stable regardless of window — quiet/offline cams stay visible.
        roster = set(sources_by_name)

        site_rows = []
        for name in roster:
            count, species, last_seen = agg.get(name, (0, 0, None))
            if last_seen is not None and getattr(last_seen, "tzinfo", None) is None:
                last_seen = last_seen.replace(tzinfo=UTC)
            det_b = det_spark.get(name, [0] * n_spark)
            sp_b = sp_spark.get(name, [0] * n_spark)
            h = health.get(name, {})
            down_24h_s = h.get("down_24h_s", 0)
            current_outage_s = h.get("current_outage_s")
            audio_score = h.get("audio_score")
            audio_stale = h.get("audio_stale", False)
            # Composite system-health 0-100: half uptime, half audio quality.
            # Currently-down → 0; audio unknown/stale → uptime-only (flagged).
            uptime_pct = (
                100.0 if down_24h_s <= 0
                else max(0.0, 100.0 * (86400 - down_24h_s) / 86400)
            )
            audio_ok = audio_score is not None and not audio_stale
            if current_outage_s is not None:
                health_score = 0
            elif audio_ok:
                health_score = round(0.5 * uptime_pct + 0.5 * audio_score)
            else:
                health_score = round(uptime_pct)
            site_rows.append(
                {
                    "source": name,
                    "count": count,
                    "species": species,
                    "det_buckets": det_b,
                    "det_peak": max(det_b) or 1,
                    "sp_buckets": sp_b,
                    "sp_peak": max(sp_b) or 1,
                    "last_seen": last_seen,
                    "status": h.get("status", "never"),
                    "current_outage_s": current_outage_s,
                    "down_24h_s": down_24h_s,
                    "uptime_pct": round(uptime_pct, 1),
                    "audio_score": audio_score,
                    "audio_issue": h.get("audio_issue"),
                    "audio_stale": audio_stale,
                    "audio_ok": audio_ok,
                    "health": health_score,
                }
            )
        # Rank by species diversity (count breaks ties), as the scoreboard did;
        # currently-down rows are tinted in the template rather than re-sorted.
        site_rows.sort(key=lambda r: (r["species"], r["count"]), reverse=True)
        source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
        windows = [
            (1, "1h"), (6, "6h"), (24, "24h"), (48, "48h"), (168, "7d"),
            (720, "30d"), (4380, "6m"), (8760, "1y"), (0, "all time"),
        ]
        window_label = dict(windows).get(hours, f"{hours}h")
        return TEMPLATES.TemplateResponse(
            request,
            "sites.html",
            {
                "hours": hours,
                "since": since,
                "total": total,
                "site_rows": site_rows,
                "source_tz": source_tz,
                "windows": windows,
                "window_label": window_label,
            },
        )

    @app.get("/api/sites/{name:path}/activity")
    def site_activity(
        name: str,
        hours: int = Query(default=24, ge=0, le=24 * 400),
        metric: str = Query(default="detections"),  # detections | species
    ) -> dict:
        """Per-bucket activity for one site over the last ``hours`` (``hours=0``
        = all-time). ``metric`` selects detection counts or distinct-species
        counts. Bars adapt in width to the window; returns five absolute
        date:time axis labels in the site's local timezone."""
        metric = metric if metric in ("detections", "species") else "detections"
        _, sources_by_name, _ = _all_sources()
        cfg_src = sources_by_name.get(name)
        tz = _zone_info(cfg_src.timezone if cfg_src else "UTC")
        now = datetime.now(UTC)
        if hours <= 0:  # all-time
            with db.session() as s:
                first = s.scalar(
                    select(func.min(DetectionRow.started_at))
                    .where(DetectionRow.source_name == name)
                )
            if first is None:
                return {"hours": 0, "metric": metric, "buckets": [], "total": 0,
                        "peak": 1, "bucket_label": "—", "axis": [], "boundaries": [],
                        "times": []}
            since = first.replace(tzinfo=UTC) if first.tzinfo is None else first
        else:
            since = now - timedelta(hours=hours)
        window_s = max(60.0, (now - since).total_seconds())
        with db.session() as s:
            rows = s.execute(
                select(DetectionRow.started_at, DetectionRow.scientific_name)
                .where(DetectionRow.source_name == name)
                .where(DetectionRow.started_at >= since)
            ).all()
        n = 120 if hours <= 0 else min(120, max(12, hours))
        bucket_s = window_s / n
        if metric == "species":
            sets: list[set] = [set() for _ in range(n)]
            seen: set = set()
            for t, sci in rows:
                if t.tzinfo is None:
                    t = t.replace(tzinfo=UTC)
                idx = int((t - since).total_seconds() // bucket_s)
                if 0 <= idx < n:
                    sets[idx].add(sci)
                seen.add(sci)
            buckets = [len(x) for x in sets]
            total = len(seen)
        else:
            buckets = [0] * n
            for t, _sci in rows:
                if t.tzinfo is None:
                    t = t.replace(tzinfo=UTC)
                idx = int((t - since).total_seconds() // bucket_s)
                if 0 <= idx < n:
                    buckets[idx] += 1
            total = len(rows)
        if bucket_s < 3600:
            label = f"{int(round(bucket_s / 60))}-min bars"
        elif bucket_s < 86400:
            label = f"{round(bucket_s / 3600, 1)}h bars"
        else:
            label = f"{round(bucket_s / 86400, 1)}d bars"
        # Five absolute date:time ticks across the window, in the site's tz.
        long = window_s > 30 * 86400
        afmt = "%Y-%m-%d" if long else "%m-%d %H:%M"
        axis = [
            (since + timedelta(seconds=window_s * i / 4)).astimezone(tz).strftime(afmt)
            for i in range(5)
        ]
        # Per-bucket start time (local, for the hover readout) and the indices
        # that begin a new local day (or month, for long windows) for separators.
        boundaries: list[int] = []
        times: list[str] = []
        prev_key = None
        for i in range(n):
            d = (since + timedelta(seconds=bucket_s * i)).astimezone(tz)
            times.append(d.strftime(afmt))
            key = (d.year, d.month) if long else (d.year, d.month, d.day)
            if prev_key is not None and key != prev_key:
                boundaries.append(i)
            prev_key = key
        if len(boundaries) > 40:  # too dense to be useful
            boundaries = []
        return {
            "hours": hours,
            "metric": metric,
            "buckets": buckets,
            "total": total,
            "peak": max(buckets) or 1,
            "bucket_label": label,
            "axis": axis,
            "boundaries": boundaries,
            "times": times,
        }

    def _detections_context(
        source: str | None,
        species: str | None,
        min_conf: float,
        max_conf: float,
        label_filter: str,
        note_tag: str,
        order: str,
        limit: int,
        user_id: int | None = None,
    ) -> dict:
        """Build the template context for the per-detection view of /review.
        Pulled out of the old /audition handler so /review can dispatch. When
        ``user_id`` is given, /review is that tester's personal queue: filters
        and counts key off THEIR scores (unreviewed = not scored by them),
        not the consensus."""
        _, sources_by_name, _ = _all_sources()

        def _user_score_exists(label=None):
            q = (
                select(DetectionScoreRow.id)
                .where(DetectionScoreRow.detection_id == DetectionRow.id)
                .where(DetectionScoreRow.user_id == user_id)
            )
            if label is not None:
                q = q.where(DetectionScoreRow.label == label)
            return q.exists()

        with db.session() as s:
            stmt = (
                select(DetectionRow)
                .where(DetectionRow.clip_path.is_not(None))
                .where(DetectionRow.confidence >= min_conf)
                .where(DetectionRow.confidence <= max_conf)
            )
            if source:
                stmt = stmt.where(DetectionRow.source_name == source)
            if species:
                stmt = stmt.where(DetectionRow.common_name.ilike(f"%{species}%"))
            if user_id is not None:
                # Per-user queue: scored-by-me vs not.
                if label_filter == "unreviewed":
                    stmt = stmt.where(~_user_score_exists())
                elif label_filter in ("good", "bad", "unsure"):
                    stmt = stmt.where(_user_score_exists(label_filter))
            else:
                # Anonymous/LAN-not-logged-in: fall back to consensus.
                if label_filter == "unreviewed":
                    stmt = stmt.where(DetectionRow.label.is_(None))
                elif label_filter in ("good", "bad", "unsure"):
                    stmt = stmt.where(DetectionRow.label == label_filter)
            # 'all' adds no filter

            if note_tag in ("reliable", "suspect", "rare"):
                stmt = stmt.where(
                    DetectionRow.scientific_name.in_(
                        select(SpeciesNoteRow.scientific_name).where(
                            SpeciesNoteRow.tag == note_tag
                        )
                    )
                )
            elif note_tag == "untagged":
                # Either no note at all, or a note with NULL tag (neutral).
                stmt = stmt.where(
                    DetectionRow.scientific_name.notin_(
                        select(SpeciesNoteRow.scientific_name).where(
                            SpeciesNoteRow.tag.is_not(None)
                        )
                    )
                )
            # 'any' adds no filter

            if order == "conf_asc":
                stmt = stmt.order_by(DetectionRow.confidence.asc())
            elif order == "recent":
                stmt = stmt.order_by(desc(DetectionRow.started_at))
            else:
                stmt = stmt.order_by(desc(DetectionRow.confidence))
            rows = list(s.scalars(stmt.limit(limit)))

            all_sources = list(
                s.scalars(
                    select(DetectionRow.source_name)
                    .group_by(DetectionRow.source_name)
                    .order_by(DetectionRow.source_name)
                )
            )
            all_species = list(
                s.scalars(
                    select(DetectionRow.common_name)
                    .group_by(DetectionRow.common_name)
                    .order_by(DetectionRow.common_name)
                )
            )
            # Counts for the filter chips. Per-user when logged in (their own
            # tallies; unreviewed = clips they haven't scored), else consensus.
            your_labels: dict[int, str | None] = {}
            if user_id is not None:
                uc = dict(s.execute(
                    select(DetectionScoreRow.label, func.count())
                    .where(DetectionScoreRow.user_id == user_id)
                    .group_by(DetectionScoreRow.label)
                ).all())
                scored = sum(uc.values())
                total_clips = s.scalar(
                    select(func.count()).select_from(DetectionRow)
                    .where(DetectionRow.clip_path.is_not(None))
                ) or 0
                label_counts = {
                    "good": uc.get("good", 0), "bad": uc.get("bad", 0),
                    "unsure": uc.get("unsure", 0),
                    None: max(0, total_clips - scored),
                }
                if rows:
                    your_labels = dict(s.execute(
                        select(DetectionScoreRow.detection_id, DetectionScoreRow.label)
                        .where(DetectionScoreRow.user_id == user_id)
                        .where(DetectionScoreRow.detection_id.in_([r.id for r in rows]))
                    ).all())
            else:
                label_counts = dict(
                    s.execute(
                        select(DetectionRow.label, func.count())
                        .group_by(DetectionRow.label)
                    ).all()
                )
            # Species → note tag, so each row can encode its palette in the
            # spectrogram URL (browser cache busts on re-tag without a full
            # page reload).
            _notes = list(s.scalars(select(SpeciesNoteRow)))
            note_tag_by_sci = {n.scientific_name: n.tag for n in _notes}
            status_by_sci = {
                n.scientific_name: n.conservation_status
                for n in _notes
                if n.conservation_status
            }

        source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
        return {
            "rows": rows,
            "all_sources": all_sources,
            "all_species": all_species,
            "note_tag_by_sci": note_tag_by_sci,
            "status_by_sci": status_by_sci,
            "palette_for_tag": {
                k: v for k, v in SPEC_PALETTE_FOR_TAG.items() if k is not None
            },
            "default_palette": SPEC_PALETTE_FOR_TAG[None],
            "source_tz": source_tz,
            "filters": {
                "source": source or "",
                "species": species or "",
                "min_conf": min_conf,
                "max_conf": max_conf,
                "label_filter": label_filter,
                "note_tag": note_tag,
                "order": order,
                "limit": limit,
            },
            "label_counts": {
                "good": label_counts.get("good", 0),
                "bad": label_counts.get("bad", 0),
                "unsure": label_counts.get("unsure", 0),
                "unreviewed": label_counts.get(None, 0),
            },
            "your_labels": your_labels,
        }

    def _chunks_context(
        source: str | None,
        min_species: int,
        order: str,
        limit: int,
        bucket: str,
    ) -> dict:
        """Build the template context for the multi-species-chunk view of
        /review. Pulled out of the old /soundscape handler.

        Every DetectionRow sharing a ``clip_path`` came from the same 3 s
        window, so a clip_path with >=2 rows means BirdNET heard several
        birds — or hedged between similar species — in the same instant.

        Split into two buckets:

        * **genuine** — distinct genera AND a tight confidence cluster: the
          model is confident about each species separately, so we treat the
          chunk as a real multi-bird moment.
        * **confusions** — anything else: two species sharing a genus, a
          weak runner-up next to a strong leader, or any below-floor row.
          We err here on purpose; better to under-celebrate a soundscape
          than to mistake a hedge for one.
        """
        if bucket not in ("genuine", "confusions"):
            bucket = "genuine"
        _, sources_by_name, _ = _all_sources()

        # Pull the genus out of the scientific name in SQL so we can count
        # distinct genera per chunk. Two species sharing a genus is the
        # cleanest "BirdNET is hedging" signal we have.
        space_idx = func.instr(DetectionRow.scientific_name, " ")
        genus_expr = case(
            (space_idx > 0,
             func.substr(DetectionRow.scientific_name, 1, space_idx - 1)),
            else_=DetectionRow.scientific_name,
        )

        # Thresholds that define "genuine." See the docstring above for the
        # rationale; tuning these is the main lever if results drift.
        conf_floor = 0.50      # absolute floor for the weakest row
        conf_ratio_min = 0.50  # min_conf / max_conf must clear this

        n_col = func.count().label("n")
        n_genera_col = func.count(func.distinct(genus_expr)).label("n_genera")
        min_conf_col = func.min(DetectionRow.confidence).label("min_conf")
        max_conf_col = func.max(DetectionRow.confidence).label("max_conf")

        genuine_having = and_(
            n_col >= min_species,
            n_genera_col == n_col,
            min_conf_col >= conf_floor,
            min_conf_col >= conf_ratio_min * max_conf_col,
        )
        confusion_having = and_(
            n_col >= min_species,
            or_(
                n_genera_col < n_col,
                min_conf_col < conf_floor,
                min_conf_col < conf_ratio_min * max_conf_col,
            ),
        )
        bucket_having = genuine_having if bucket == "genuine" else confusion_having

        with db.session() as s:
            group_stmt = (
                select(
                    DetectionRow.clip_path.label("clip_path"),
                    n_col,
                    func.max(DetectionRow.started_at).label("latest"),
                    max_conf_col,
                    min_conf_col,
                    n_genera_col,
                )
                .where(DetectionRow.clip_path.is_not(None))
                .group_by(DetectionRow.clip_path)
                .having(bucket_having)
            )
            if source:
                group_stmt = group_stmt.where(DetectionRow.source_name == source)
            if order == "n_species":
                group_stmt = group_stmt.order_by(desc("n"), desc("latest"))
            elif order == "max_conf":
                group_stmt = group_stmt.order_by(desc("max_conf"))
            else:
                group_stmt = group_stmt.order_by(desc("latest"))

            groups_meta = list(s.execute(group_stmt.limit(limit)).all())
            clip_paths = [g.clip_path for g in groups_meta]

            if clip_paths:
                rows = list(
                    s.scalars(
                        select(DetectionRow)
                        .where(DetectionRow.clip_path.in_(clip_paths))
                        .order_by(desc(DetectionRow.confidence))
                    )
                )
            else:
                rows = []

            by_clip: dict[str, list[DetectionRow]] = {}
            for r in rows:
                by_clip.setdefault(r.clip_path, []).append(r)

            groups = [
                {
                    "clip_path": g.clip_path,
                    "n": g.n,
                    "latest": g.latest,
                    "rows": by_clip.get(g.clip_path, []),
                }
                for g in groups_meta
                if by_clip.get(g.clip_path)
            ]

            all_sources_list = list(
                s.scalars(
                    select(DetectionRow.source_name)
                    .group_by(DetectionRow.source_name)
                    .order_by(DetectionRow.source_name)
                )
            )
            _notes = list(s.scalars(select(SpeciesNoteRow)))
            note_tag_by_sci = {n.scientific_name: n.tag for n in _notes}
            status_by_sci = {
                n.scientific_name: n.conservation_status
                for n in _notes
                if n.conservation_status
            }
            all_species = list(
                s.scalars(
                    select(DetectionRow.common_name)
                    .group_by(DetectionRow.common_name)
                    .order_by(DetectionRow.common_name)
                )
            )

            def _bucket_count(having_clause) -> int:
                sub = (
                    select(DetectionRow.clip_path)
                    .where(DetectionRow.clip_path.is_not(None))
                )
                if source:
                    sub = sub.where(DetectionRow.source_name == source)
                sub = sub.group_by(DetectionRow.clip_path).having(having_clause)
                return s.scalar(
                    select(func.count()).select_from(sub.subquery())
                ) or 0

            genuine_count = _bucket_count(genuine_having)
            confusion_count = _bucket_count(confusion_having)
            total_groups = genuine_count + confusion_count

        source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
        return {
            "groups": groups,
            "total_groups": total_groups,
            "genuine_count": genuine_count,
            "confusion_count": confusion_count,
            "bucket": bucket,
            "all_sources": all_sources_list,
            "all_species": all_species,
            "note_tag_by_sci": note_tag_by_sci,
            "status_by_sci": status_by_sci,
            "palette_for_tag": {
                k: v for k, v in SPEC_PALETTE_FOR_TAG.items() if k is not None
            },
            "default_palette": SPEC_PALETTE_FOR_TAG[None],
            "source_tz": source_tz,
            "filters": {
                "source": source or "",
                "min_species": min_species,
                "order": order,
                "limit": limit,
                "bucket": bucket,
            },
        }

    @app.get("/review", response_class=HTMLResponse)
    def review(
        request: Request,
        tab: str = Query(default="detections"),
        # Detection-view filters
        source: str | None = Query(default=None),
        species: str | None = Query(default=None),
        min_conf: float = Query(default=0.0, ge=0.0, le=1.0),
        max_conf: float = Query(default=1.0, ge=0.0, le=1.0),
        label_filter: str = Query(default="unreviewed"),
        note_tag: str = Query(default="any"),
        # Chunk-view filters
        min_species: int = Query(default=2, ge=2, le=10),
        bucket: str = Query(default="genuine"),
        # Both tabs share these but defaults differ — see below.
        order: str | None = Query(default=None),
        limit: int | None = Query(default=None),
    ) -> HTMLResponse:
        """Unified review surface — merges the old /audition (per-detection)
        and /soundscape (multi-species chunks) into one page with tabs."""
        if tab not in ("detections", "chunks"):
            tab = "detections"
        if tab == "chunks":
            ctx = _chunks_context(
                source=source,
                min_species=min_species,
                order=order or "recent",
                limit=limit or 50,
                bucket=bucket,
            )
        else:
            _user = getattr(request.state, "user", None)
            ctx = _detections_context(
                source=source,
                species=species,
                min_conf=min_conf,
                max_conf=max_conf,
                label_filter=label_filter,
                note_tag=note_tag,
                order=order or "conf_desc",
                limit=limit or 50,
                user_id=_user.id if _user else None,
            )
        ctx["tab"] = tab
        ctx.update(_note_tag_context())
        return TEMPLATES.TemplateResponse(request, "review.html", ctx)

    @app.get("/audition")
    def audition_redirect(request: Request) -> RedirectResponse:
        """Legacy /audition URLs get punted to /review?tab=detections,
        preserving the query string so bookmarked filters keep working."""
        qs = request.url.query
        target = "/review?tab=detections" + (f"&{qs}" if qs else "")
        return RedirectResponse(target, status_code=302)

    @app.get("/soundscape")
    def soundscape_redirect(request: Request) -> RedirectResponse:
        qs = request.url.query
        target = "/review?tab=chunks" + (f"&{qs}" if qs else "")
        return RedirectResponse(target, status_code=302)

    @app.get("/diurnal", response_class=HTMLResponse)
    def diurnal(
        request: Request,
        source: str | None = Query(default=None),
        days: int = Query(default=7, ge=1, le=365),
        # Strings, not ``date``, because the form posts empty ``since=`` when
        # the calendar inputs are blank — and FastAPI would 422 on that.
        since: str | None = Query(default=None),
        until: str | None = Query(default=None),
        top_n: int = Query(default=12, ge=3, le=40),
        min_conf: float = Query(default=0.5, ge=0.0, le=1.0),
    ) -> HTMLResponse:
        def _parse_iso_date(s: str | None) -> date | None:
            if not s:
                return None
            try:
                return date.fromisoformat(s.strip())
            except ValueError:
                return None
        since_d = _parse_iso_date(since)
        until_d = _parse_iso_date(until)
        """Activity by hour of day, in each source's local timezone.

        Two charts:
          * total detections per hour across the selection (bar)
          * top species × hour cells (heatmap)

        Hour is computed in the *source's* configured timezone so dawn-chorus
        peaks line up regardless of which camera the row came from."""
        _, sources_by_name, _ = _all_sources()

        def _tz_for(name: str) -> ZoneInfo:
            cfg = sources_by_name.get(name)
            return _zone_info(cfg.timezone if cfg else "UTC")

        # Effective window. If both date pickers filled, they win and we
        # back-compute ``days`` from the span (used downstream for the
        # weather call and the chart header).
        now = datetime.now(UTC)
        if since_d is not None and until_d is not None and until_d >= since_d:
            since_dt = datetime(since_d.year, since_d.month, since_d.day, tzinfo=UTC)
            until_dt = datetime(
                until_d.year, until_d.month, until_d.day, tzinfo=UTC
            ) + timedelta(days=1)
            days = max(1, min(365, (until_dt - since_dt).days))
            window_label = f"{since_d.isoformat()} → {until_d.isoformat()}"
        else:
            since_dt = now - timedelta(days=days)
            until_dt = now
            window_label = f"last {days} day" + ("" if days == 1 else "s")

        with db.session() as s:
            stmt = (
                select(DetectionRow)
                .where(DetectionRow.started_at >= since_dt)
                .where(DetectionRow.started_at < until_dt)
                .where(DetectionRow.confidence >= min_conf)
            )
            if source:
                stmt = stmt.where(DetectionRow.source_name == source)
            rows = list(s.scalars(stmt))

            all_sources_list = list(
                s.scalars(
                    select(DetectionRow.source_name)
                    .group_by(DetectionRow.source_name)
                    .order_by(DetectionRow.source_name)
                )
            )

        hour_overall = [0] * 24
        per_species: dict[str, dict] = {}
        # Track which timezone is producing the most rows. When sources span
        # different tzs (e.g., a UTC-default runtime source mixed with the
        # Africa/Johannesburg cams) we use this to pick the tz the bars are
        # mostly drawn in — and align the solar bands to it.
        tz_counts: dict[str, int] = {}
        for r in rows:
            ts = r.started_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            cfg = sources_by_name.get(r.source_name)
            tz_name = cfg.timezone if cfg else "UTC"
            tz_counts[tz_name] = tz_counts.get(tz_name, 0) + 1
            hr = ts.astimezone(_tz_for(r.source_name)).hour
            hour_overall[hr] += 1
            d = per_species.setdefault(
                r.scientific_name,
                {
                    "scientific": r.scientific_name,
                    "common": r.common_name,
                    "hours": [0] * 24,
                    "total": 0,
                },
            )
            d["hours"][hr] += 1
            d["total"] += 1

        dominant_tz_name = (
            max(tz_counts, key=tz_counts.get) if tz_counts else "UTC"
        )

        ranked = sorted(per_species.values(), key=lambda d: d["total"], reverse=True)
        top_species = ranked[:top_n]

        peak_hour = max(range(24), key=lambda h: hour_overall[h]) if rows else None
        peak_hour_count = hour_overall[peak_hour] if peak_hour is not None else 0

        # Pick the tz used for the chart axis and the bands. Always prefer
        # the tz that owns the most detections in the window (matches the
        # bars). For a single selected source, that's just its tz.
        if source and source in sources_by_name:
            axis_tz_name = sources_by_name[source].timezone
        else:
            axis_tz_name = dominant_tz_name
        tz_label = _tz_abbr(axis_tz_name)

        # Solar bands: average lat/lon across the relevant sources and use
        # the window midpoint date. For a single selected source, that's
        # just that source. Sun times only meaningfully differ within
        # southern Africa by a few minutes across these cameras.
        if source and source in sources_by_name:
            band_cfgs = [sources_by_name[source]]
        else:
            band_cfgs = [c for c in sources_by_name.values()
                         if c.lat is not None and c.lon is not None]
        lats = [c.lat for c in band_cfgs if c.lat is not None]
        lons = [c.lon for c in band_cfgs if c.lon is not None]
        bands: list[dict] = []
        if lats and lons:
            avg_lat = sum(lats) / len(lats)
            avg_lon = sum(lons) / len(lons)
            mid_dt = since_dt + (until_dt - since_dt) / 2
            try:
                band_tz = ZoneInfo(axis_tz_name)
            except ZoneInfoNotFoundError:
                band_tz = UTC  # type: ignore[assignment]
            mid_date = mid_dt.astimezone(band_tz).date()
            bands = _solar_bands(mid_date, avg_lat, avg_lon, band_tz)

        payload = {
            "tz_label": tz_label,
            "total": len(rows),
            "n_species": len(per_species),
            "peak_hour": peak_hour,
            "peak_hour_count": peak_hour_count,
            "hour_overall": [{"hour": h, "count": hour_overall[h]} for h in range(24)],
            "hour_by_species": [
                {
                    "species": d["common"],
                    "scientific": d["scientific"],
                    "total": d["total"],
                    "hours": d["hours"],
                }
                for d in top_species
            ],
            "bands": bands,
            "filters": {
                "source": source or "",
                "days": days,
                "since": since_d.isoformat() if since_d else "",
                "until": until_d.isoformat() if until_d else "",
                "min_conf": min_conf,
            },
        }

        return TEMPLATES.TemplateResponse(
            request,
            "diurnal.html",
            {
                "data_json": json.dumps(payload),
                "data": payload,
                "all_sources": all_sources_list,
                "window_label": window_label,
                "filters": {
                    "source": source or "",
                    "days": days,
                    "since": since_d.isoformat() if since_d else "",
                    "until": until_d.isoformat() if until_d else "",
                    "top_n": top_n,
                    "min_conf": min_conf,
                },
                "preset_days": [
                    (1, "1d"),
                    (7, "7d"),
                    (30, "30d"),
                    (90, "3mo"),
                    (365, "1y"),
                ],
            },
        )

    @app.get("/partials/diurnal-popup", response_class=HTMLResponse)
    def diurnal_popup(
        request: Request,
        scientific: str = Query(..., min_length=1),
        hour: int = Query(..., ge=0, le=23),
        source: str | None = Query(default=None),
        days: int = Query(default=7, ge=1, le=365),
        since: str | None = Query(default=None),
        until: str | None = Query(default=None),
        min_conf: float = Query(default=0.5, ge=0.0, le=1.0),
    ) -> HTMLResponse:
        """Cell popup: species summary, 24-hour radial activity dial, and
        weather at this hour averaged across the lookback window. The dial
        sums detections per source-local hour over the same window the
        chart used. Weather requires a single source with lat/lon."""
        _, sources_by_name, _ = _all_sources()

        def _parse_iso_date(s: str | None) -> date | None:
            if not s:
                return None
            try:
                return date.fromisoformat(s.strip())
            except ValueError:
                return None

        since_d = _parse_iso_date(since)
        until_d = _parse_iso_date(until)
        now = datetime.now(UTC)
        if since_d is not None and until_d is not None and until_d >= since_d:
            since_dt = datetime(since_d.year, since_d.month, since_d.day, tzinfo=UTC)
            until_dt = datetime(
                until_d.year, until_d.month, until_d.day, tzinfo=UTC
            ) + timedelta(days=1)
        else:
            since_dt = now - timedelta(days=days)
            until_dt = now
        weather_days = max(1, min(92, (until_dt - since_dt).days or 1))

        def _tz_for(name: str) -> ZoneInfo:
            cfg = sources_by_name.get(name)
            return _zone_info(cfg.timezone if cfg else "UTC")

        with db.session() as s:
            note = s.get(SpeciesNoteRow, scientific)
            sample = s.scalar(
                select(DetectionRow)
                .where(DetectionRow.scientific_name == scientific)
                .order_by(desc(DetectionRow.confidence))
                .limit(1)
            )
            stmt = (
                select(DetectionRow)
                .where(DetectionRow.scientific_name == scientific)
                .where(DetectionRow.started_at >= since_dt)
                .where(DetectionRow.started_at < until_dt)
                .where(DetectionRow.confidence >= min_conf)
            )
            if source:
                stmt = stmt.where(DetectionRow.source_name == source)
            species_rows = list(s.scalars(stmt))

        hours_arr = [0] * 24
        for r in species_rows:
            ts = r.started_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            hours_arr[ts.astimezone(_tz_for(r.source_name)).hour] += 1
        peak_hour = (
            max(range(24), key=lambda h: hours_arr[h]) if any(hours_arr) else None
        )

        # Solar bands for the dial coloring — same logic as the chart route
        # so dial wedges align with the chart's time-of-day shading.
        if source and source in sources_by_name:
            band_cfgs = [sources_by_name[source]]
        else:
            band_cfgs = [
                c for c in sources_by_name.values()
                if c.lat is not None and c.lon is not None
            ]
        lats = [c.lat for c in band_cfgs if c.lat is not None]
        lons = [c.lon for c in band_cfgs if c.lon is not None]
        dial_bands: list[dict] = []
        if lats and lons:
            avg_lat = sum(lats) / len(lats)
            avg_lon = sum(lons) / len(lons)
            mid_dt = since_dt + (until_dt - since_dt) / 2
            if source and source in sources_by_name:
                band_tz_name = sources_by_name[source].timezone or "UTC"
            elif len({cfg.timezone for cfg in sources_by_name.values()}) == 1:
                band_tz_name = next(iter(sources_by_name.values())).timezone or "UTC"
            else:
                band_tz_name = "UTC"
            try:
                band_tz = ZoneInfo(band_tz_name)
            except ZoneInfoNotFoundError:
                band_tz = UTC  # type: ignore[assignment]
            mid_date = mid_dt.astimezone(band_tz).date()
            dial_bands = _solar_bands(mid_date, avg_lat, avg_lon, band_tz)

        common = (
            (note.common_name if note and note.common_name else None)
            or (sample.common_name if sample else None)
            or scientific
        )

        weather = None
        weather_note = None
        if source and source in sources_by_name:
            cfg = sources_by_name[source]
            if cfg.lat is not None and cfg.lon is not None:
                tz_name = cfg.timezone or "UTC"
                # Local archive first — fast, no network. Falls back to a
                # live Open-Meteo call only when the weather worker hasn't
                # populated this coord yet (fresh DB, disabled worker, new
                # site added between ticks).
                weather = db.weather_hour_summary(
                    cfg.lat, cfg.lon, hour, since_dt, until_dt, tz_name,
                )
                if weather is None:
                    data = _open_meteo_hourly(
                        cfg.lat, cfg.lon, weather_days, tz_name,
                    )
                    if data:
                        weather = _weather_at_hour(data, hour) or None
                    elif data is None:
                        weather_note = "Open-Meteo lookup failed."
            else:
                weather_note = "This source has no lat/lon configured."
        else:
            weather_note = "Pick a single source to see weather."

        return TEMPLATES.TemplateResponse(
            request,
            "_diurnal_popup.html",
            {
                "scientific": scientific,
                "common": common,
                "hour": hour,
                "source": source,
                "days": days,
                "min_conf": min_conf,
                "note": note,
                "weather": weather,
                "weather_note": weather_note,
                "dial_svg": _radial_dial_svg(
                    hours_arr, highlight=hour, bands=dial_bands
                ),
                "species_total": sum(hours_arr),
                "peak_hour": peak_hour,
            },
        )

    @app.get("/partials/diurnal-detections", response_class=HTMLResponse)
    def diurnal_detections_partial(
        request: Request,
        scientific: str = Query(..., min_length=1),
        hour: int = Query(..., ge=0, le=23),
        source: str | None = Query(default=None),
        days: int = Query(default=7, ge=1, le=365),
        min_conf: float = Query(default=0.5, ge=0.0, le=1.0),
        limit: int = Query(default=40, ge=1, le=200),
    ) -> HTMLResponse:
        """Drill-down for a heatmap cell: detections of one scientific name,
        filtered to one hour-of-day in source-local time, within the diurnal
        window. Returns rendered ``_audition_row.html`` items."""
        _, sources_by_name, _ = _all_sources()

        def _local_hour(ts: datetime, src_name: str) -> int:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            cfg = sources_by_name.get(src_name)
            tz_name = cfg.timezone if cfg else "UTC"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = UTC  # type: ignore[assignment]
            return ts.astimezone(tz).hour

        since = datetime.now(UTC) - timedelta(days=days)
        with db.session() as s:
            stmt = (
                select(DetectionRow)
                .where(DetectionRow.started_at >= since)
                .where(DetectionRow.scientific_name == scientific)
                .where(DetectionRow.confidence >= min_conf)
                .where(Database.not_replay_predicate())
                .order_by(desc(DetectionRow.confidence))
            )
            if source:
                stmt = stmt.where(DetectionRow.source_name == source)
            # Fetch a generous slice from SQL, then filter the hour in Python
            # because hour-of-day depends on each source's timezone (not
            # something SQLite can do cleanly in a WHERE clause).
            candidates = list(s.scalars(stmt.limit(limit * 8)))
            rows = [r for r in candidates if _local_hour(r.started_at, r.source_name) == hour][:limit]

            note = s.get(SpeciesNoteRow, scientific)
            common_name = rows[0].common_name if rows else scientific

        source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
        return TEMPLATES.TemplateResponse(
            request,
            "_diurnal_detail.html",
            {
                "rows": rows,
                "common_name": common_name,
                "scientific": scientific,
                "source": source,
                "hour": hour,
                "days": days,
                "min_conf": min_conf,
                "source_tz": source_tz,
                "note_tag_by_sci": {scientific: note.tag} if note and note.tag else {},
                "status_by_sci": (
                    {scientific: note.conservation_status}
                    if note and note.conservation_status else {}
                ),
                "palette_for_tag": {
                    k: v for k, v in SPEC_PALETTE_FOR_TAG.items() if k is not None
                },
                "default_palette": SPEC_PALETTE_FOR_TAG[None],
            },
        )

    @app.post("/api/detections/{detection_id}/label", response_class=HTMLResponse)
    async def label_detection(
        request: Request,
        detection_id: int,
    ) -> Response:
        # Read the raw form so we can distinguish "field absent → leave the
        # suggestion alone" from "field present but empty → clear it."
        # FastAPI's ``Form(default=None)`` collapses both to ``None``.
        form = await request.form()
        value_raw = form.get("value")
        new_label = (value_raw or "").strip() or None
        if new_label is not None and new_label not in ("good", "bad", "unsure"):
            raise HTTPException(400, "value must be good/bad/unsure or empty")
        kwargs: dict = {}
        if "suggested" in form:
            s_raw = form.get("suggested") or ""
            kwargs["suggested"] = s_raw.strip() or None
        # sound_rating: absent → leave untouched; present-but-empty → clear;
        # "1".."5" → set. Anything else is a bad request.
        if "sound_rating" in form:
            r_raw = (form.get("sound_rating") or "").strip()
            if r_raw == "":
                kwargs["sound_rating"] = None
            elif r_raw in ("1", "2", "3", "4", "5"):
                kwargs["sound_rating"] = int(r_raw)
            else:
                raise HTTPException(400, "sound_rating must be 1-5 or empty")
        user = require_user(request)   # per-user scoring; 401 if not logged in
        ok = db.upsert_detection_score(detection_id, user.id, new_label, **kwargs)
        if not ok:
            raise HTTPException(404, "detection not found")
        if request.headers.get("hx-request"):
            with db.session() as s:
                row = s.get(DetectionRow, detection_id)
                note = s.get(SpeciesNoteRow, row.scientific_name) if row else None
            your = db.get_user_score(detection_id, user.id)
            _, sources_by_name, _ = _all_sources()
            source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
            return TEMPLATES.TemplateResponse(
                request,
                "_audition_row.html",
                {
                    "r": row,
                    # Badge + data-label reflect THIS user's own score; the
                    # modal's score-fetch fills suggestion/rating on open.
                    "your_labels": {detection_id: (your.label if your else None)},
                    "source_tz": source_tz,
                    "note_tag_by_sci": {row.scientific_name: note.tag} if note else {},
                    "status_by_sci": (
                        {row.scientific_name: note.conservation_status}
                        if note and note.conservation_status else {}
                    ),
                    "palette_for_tag": {
                        k: v for k, v in SPEC_PALETTE_FOR_TAG.items() if k is not None
                    },
                    "default_palette": SPEC_PALETTE_FOR_TAG[None],
                },
            )
        return JSONResponse({"ok": True, "id": detection_id, "label": new_label})

    @app.get("/api/detections/{detection_id}/score")
    def detection_score(request: Request, detection_id: int) -> JSONResponse:
        """The current user's score for a detection + the rater tally, so the
        modal can paint the verdict/stars and show consensus on any page."""
        user = getattr(request.state, "user", None)
        uid = user.id if user else None
        sc = db.get_user_score(detection_id, uid) if uid else None
        tally = db.get_score_tally(detection_id, uid)
        return JSONResponse({
            "authed": uid is not None,
            "label": sc.label if sc else None,
            "suggested": (sc.suggested_species if sc else None) or "",
            "sound_rating": (sc.sound_rating if sc else None) or "",
            "tally": tally,
        })

    @app.get("/partials/site-hour-detail", response_class=HTMLResponse)
    def site_hour_detail_partial(
        request: Request,
        source: str = Query(..., min_length=1),
        hour: int = Query(..., ge=0, le=23),
    ) -> HTMLResponse:
        """Hour drill-down for the dashboard's interactive 24-h clock.
        The dial sums detections in the trailing 24 h bucketed by source-
        local hour, so the rail does the same: any detection within the
        last ~25 h whose source-local hour matches is in scope. Hours
        already past today read as today's slot; hours later than now
        read as yesterday's. The slot label tells the operator which."""
        _, sources_by_name, _ = _all_sources()
        cfg = sources_by_name.get(source)
        tz_name = cfg.timezone if cfg else "UTC"
        tz = _zone_info(tz_name)
        now_local = datetime.now(tz)
        # ~25 h lookback (24 + a small buffer) so detections right at the
        # boundary aren't dropped if the worker is mid-write.
        since = datetime.now(UTC) - timedelta(hours=25)

        with db.session() as s:
            cand = list(s.scalars(
                select(DetectionRow)
                .where(DetectionRow.source_name == source)
                .where(DetectionRow.started_at >= since)
                .where(Database.not_replay_predicate())
                .order_by(desc(DetectionRow.confidence))
            ))

        def _local_hour_of(ts: datetime) -> int:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return ts.astimezone(tz).hour

        def _local_date_of(ts: datetime) -> date:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return ts.astimezone(tz).date()

        rows = [r for r in cand if _local_hour_of(r.started_at) == hour]
        # Label the slot by the date of the most recent matching detection.
        # For hours later than now-local this will typically resolve to
        # yesterday; for hours already past today, to today. Empty rails
        # default to "today" / today's date for display.
        today_local = now_local.date()
        slot_dates = sorted(
            {_local_date_of(r.started_at) for r in rows}, reverse=True
        )
        slot_date = slot_dates[0] if slot_dates else today_local
        if slot_date == today_local:
            slot_label = "today"
        elif slot_date == today_local - timedelta(days=1):
            slot_label = "yesterday"
        else:
            slot_label = slot_date.strftime("%a %d %b")
        # Aggregate species heard at this hour. ``conf`` is the max-
        # confidence detection of that species inside the bucket; ``n`` is
        # the count. The template renders one row per species with a
        # background bar of width n / max_n in the site's palette colour.
        by_species: dict[str, dict] = {}
        for r in rows:
            cur = by_species.setdefault(
                r.scientific_name,
                {"sci": r.scientific_name, "common": r.common_name, "n": 0, "conf": 0.0},
            )
            cur["n"] += 1
            cur["conf"] = max(cur["conf"], float(r.confidence or 0))
        species_list = sorted(
            by_species.values(), key=lambda x: (-x["n"], -x["conf"])
        )
        species_max_n = max((s["n"] for s in species_list), default=1)

        # Top clip = highest-confidence row in the bucket with a clip_path.
        top_clip = next((r for r in rows if r.clip_path), None)

        # Weather: pure local SQL read against weather_observations (filled
        # hourly by the background weather worker). Same window the rail
        # uses — last 25 h — so a click on hour=14 picks the most recent
        # 14:00 local observation. Returns None when the worker hasn't
        # populated this coord yet; the template hides the block.
        weather = None
        if cfg and cfg.lat is not None and cfg.lon is not None:
            weather = db.weather_hour_summary(
                cfg.lat, cfg.lon, hour, since, datetime.now(UTC), tz_name,
            )

        return TEMPLATES.TemplateResponse(
            request,
            "_site_hour_detail.html",
            {
                "source": source,
                "hour": hour,
                "tz_name": tz_name,
                "slot_date": slot_date,
                "slot_label": slot_label,
                "n_detections": len(rows),
                "n_species": len(by_species),
                "species_list": species_list,
                "species_max_n": species_max_n,
                "site_color": SOURCE_COLORS.get(source, "#10b981"),
                "top_clip": top_clip,
                "weather": weather,
            },
        )

    @app.get("/api/detections/{detection_id}/reanalyze")
    def reanalyze(
        detection_id: int,
        start_s: float | None = Query(default=None, ge=0.0),
        end_s: float | None = Query(default=None, ge=0.0),
    ) -> JSONResponse:
        """Re-run BirdNET on a saved clip and return the top candidates.

        Pass ``start_s`` + ``end_s`` (seconds, both required if either given)
        to analyse only a slice of the clip — useful when the user drags a
        region on the spectrogram to ask "what's that sound at 1.5s?".
        Slices shorter than 3 s are zero-padded so BirdNET's 3-second
        window is satisfied; longer slices get sliding-window analysis
        from birdnetlib internally.

        Uses a lower min_confidence than the live pipeline so runners-up
        surface. lat/lon are taken from the original detection row so the
        species filter matches what was applied during ingest.
        """
        import librosa
        import numpy as np

        from birdbrain.audio.source import AudioChunk

        with db.session() as s:
            row = s.get(DetectionRow, detection_id)
            if row is None or row.clip_path is None:
                raise HTTPException(404, "no clip for this detection")
            wav_path = Path(row.clip_path).resolve()
            lat = row.latitude
            lon = row.longitude
            started_at = row.started_at
            source_name = row.source_name
            orig_sci = row.scientific_name
            orig_common = row.common_name
            orig_conf = row.confidence

        try:
            wav_path.relative_to(clips_root)
        except ValueError as e:
            raise HTTPException(403, "clip outside allowed root") from e
        if not wav_path.is_file():
            raise HTTPException(404, "clip file missing")

        try:
            samples, sr = librosa.load(str(wav_path), sr=48_000, mono=True)
        except Exception as e:
            raise HTTPException(500, f"failed to load clip: {e}") from e
        samples = np.asarray(samples, dtype=np.float32)
        full_duration_s = float(len(samples)) / float(sr)

        analyzed_window: tuple[float, float] | None = None
        if start_s is not None and end_s is not None and end_s > start_s:
            s0 = max(0.0, min(start_s, full_duration_s))
            s1 = max(s0, min(end_s, full_duration_s))
            i0 = int(s0 * sr)
            i1 = int(s1 * sr)
            samples = samples[i0:i1]
            analyzed_window = (round(s0, 3), round(s1, 3))
            min_len = int(3 * sr)
            if len(samples) < min_len:
                samples = np.pad(samples, (0, min_len - len(samples)))
                samples = np.asarray(samples, dtype=np.float32)

        if started_at is not None and started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        chunk = AudioChunk(
            samples=samples,
            sample_rate=int(sr),
            started_at=started_at or datetime.now(UTC),
            source_name=source_name,
        )
        detector = _get_reanalyze_detector()
        # Low floor on purpose — we want to see runners-up the live pipeline
        # would have suppressed. Per-species overrides are deliberately NOT
        # applied here either.
        detections = detector.analyze(
            chunk, lat=lat, lon=lon, week=None, min_confidence=0.05
        )
        detections.sort(key=lambda d: d.confidence, reverse=True)
        top = [
            {
                "scientific_name": d.scientific_name,
                "common_name": d.common_name,
                "confidence": round(float(d.confidence), 4),
                "is_original": d.scientific_name == orig_sci,
            }
            for d in detections[:10]
        ]
        return JSONResponse(
            {
                "detections": top,
                "original": {
                    "scientific_name": orig_sci,
                    "common_name": orig_common,
                    "confidence": round(float(orig_conf), 4),
                },
                "clip_duration_s": round(full_duration_s, 3),
                "analyzed_window_s": list(analyzed_window) if analyzed_window else None,
            }
        )

    @app.get("/api/detections/{detection_id}/locate")
    def locate(detection_id: int) -> JSONResponse:
        """For the clip containing this detection, return an approximate
        time/frequency marker for every species detected in that clip —
        the peak of spectrogram energy inside the species's typical
        frequency band. Useful overlay on the modal spectrogram for
        multi-species chunks ("which call belongs to which species?").
        """
        from birdbrain.audio.locator import locate_species_in_clip

        with db.session() as s:
            row = s.get(DetectionRow, detection_id)
            if row is None or row.clip_path is None:
                raise HTTPException(404, "no clip for this detection")
            wav_path = Path(row.clip_path).resolve()
            siblings = list(
                s.scalars(
                    select(DetectionRow)
                    .where(DetectionRow.clip_path == row.clip_path)
                    .order_by(desc(DetectionRow.confidence))
                )
            )

        try:
            wav_path.relative_to(clips_root)
        except ValueError as e:
            raise HTTPException(403, "clip outside allowed root") from e
        if not wav_path.is_file():
            raise HTTPException(404, "clip file missing")

        species = [(r.scientific_name, r.common_name) for r in siblings]
        try:
            markers = locate_species_in_clip(wav_path, species)
        except Exception as e:
            raise HTTPException(500, f"failed to locate: {e}") from e

        conf_by_sci = {r.scientific_name: float(r.confidence) for r in siblings}
        for m in markers:
            m["confidence"] = round(conf_by_sci.get(m["scientific_name"], 0.0), 3)

        return JSONResponse(
            {
                "markers": markers,
                # Visual frequency range used by the spectrogram image. The
                # client needs these to map Hz → y%.
                "spec_freq_lo_hz": 80.0,
                "spec_freq_hi_hz": 12000.0,
            }
        )

    @app.get("/api/detections/{detection_id}/site")
    def detection_site(detection_id: int) -> JSONResponse:
        """Where this detection was recorded: the site name + coordinates,
        so the review modal can show a small location map and a clear site
        label. Coords are stamped on every detection row; ``site`` is unused
        in this single-site-per-source deployment, so the source name *is*
        the site label."""
        with db.session() as s:
            row = s.get(DetectionRow, detection_id)
            if row is None:
                raise HTTPException(404, "no such detection")
            return JSONResponse(
                {
                    "site": row.site or row.source_name,
                    "source_name": row.source_name,
                    "lat": row.latitude,
                    "lon": row.longitude,
                }
            )

    SPEC_SIZES = {
        "small": "240x60",   # inline conf-bar background
        "large": "900x220",  # popout modal
    }

    # Most birds live below ~10 kHz. ffmpeg's default showspectrumpic axis is
    # 0..Nyquist (24 kHz for our 48 kHz audio), so a Hadada Ibis at ~1 kHz
    # gets squashed into the bottom 1/24. Cap at 12 kHz and use log frequency
    # scaling for extra emphasis on the low band.
    SPEC_FILTER_BASE = "legend=0:scale=log:fscale=log:start=80:stop=12000"

    # ffmpeg showspectrumpic color palettes per species-note tag — at-a-glance
    # status colour without changing the audio data. 'fire' is the default
    # warm palette used for untagged species and matches what we generated
    # before notes existed.
    SPEC_PALETTE_FOR_TAG: dict[str | None, str] = {
        "reliable": "green",
        "suspect":  "fiery",  # warm yellow-orange-red
        "rare":     "cool",   # blues
        None:       "fire",
    }

    # Xeno-Canto reference fetch (proxied + cached so the browser doesn't hit
    # the public API directly — gives us caching, error normalization, and no
    # CORS surprises).
    _xc_cache: dict[str, tuple[float, list[dict]]] = {}
    _XC_TTL_SECONDS = 6 * 3600

    def _https(url: str | None) -> str | None:
        """Xeno-Canto returns protocol-relative URLs (``//xeno-canto.org/...``).
        Force https so the browser doesn't downgrade or block them."""
        if not url:
            return None
        return "https:" + url if url.startswith("//") else url

    # Wikipedia thumbnail cache: scientific name → (expiry, payload).
    # Wikipedia pages change rarely, so 24h TTL is fine and keeps load off
    # Wikimedia's REST API.
    _wp_cache: dict[str, tuple[float, dict]] = {}
    _WP_TTL_SECONDS = 24 * 3600
    # Durable DB-backed media cache: how long a persisted lookup stays fresh
    # before the sweeper/endpoint will re-fetch from Wikipedia.
    _MEDIA_DB_TTL_DAYS = 30

    # Open-Meteo hourly archive cache: (lat, lon, past_days, tz) → (expiry, json).
    # Their data updates hourly at most; 1h TTL is generous given how stale
    # the diurnal view's lookback window is. Free API, no key required.
    # Open-Meteo lookups moved to birdbrain.weather (shared with the notes
    # worker). Aliased here so call sites below don't change.
    _open_meteo_hourly = weather_module.fetch_open_meteo_hourly
    _weather_at_hour = weather_module.weather_at_hour

    # Time-of-day band fills shared by every chart that paints solar bands.
    # Night = deep navy (almost black); twilight ramps through warm pinks
    # and oranges around sunrise/sunset; day = sky blue. Stars are added on
    # top of the night band in the dial only.
    # Dial collapses to three visual bands: night, twilight (dawn/dusk), day.
    # The sunrise/sunset glow strips are folded into day so the dial doesn't
    # have to render the orange wedges that kept reading as detection bars.
    # Bar chart in diurnal.html keeps the full warm palette.
    _DIAL_BAND_FILL = {
        "night":   "#020617",  # slate-950
        "dawn":    "#7f1d1d",  # red-900 — deep muted rose
        "sunrise": "#38bdf8",  # collapsed into day
        "day":     "#38bdf8",  # sky-400
        "sunset":  "#38bdf8",  # collapsed into day
        "dusk":    "#7f1d1d",
    }
    _DIAL_BAND_OPACITY = {
        "night":   0.85,
        "dawn":    0.30,
        "sunrise": 0.18,
        "day":     0.18,
        "sunset":  0.18,
        "dusk":    0.30,
    }
    _DIAL_DEFAULT_FILL = "#10b981"  # emerald-500 — wedge bars / no-band fallback

    # Emblematic Southern-Hemisphere May-evening constellations. Stars are
    # (x_norm, y_norm, magnitude_scale) in their own [0,1]² bounding box;
    # ``lines`` connect star indices to suggest the figure. Scorpius is
    # high in the morning sky in May; Crux is high in the evening.
    _CONSTELLATIONS: dict[str, dict] = {
        "crux": {
            "stars": (
                (0.50, 0.08, 1.4),  # Gacrux (top)
                (0.20, 0.45, 1.2),  # Mimosa (left)
                (0.78, 0.40, 1.1),  # Delta (right)
                (0.48, 0.92, 1.6),  # Acrux (bottom, brightest)
                (0.62, 0.68, 0.7),  # Epsilon (faint)
            ),
            "lines": ((0, 3), (1, 2)),
        },
        "scorpius": {
            "stars": (
                # Pincers (chelae) — two claw arms branching off the head.
                (0.10, 0.08, 0.9),  # 0 — β1 (Graffias, upper claw tip)
                (0.22, 0.20, 0.7),  # 1 — upper claw joint
                (0.50, 0.05, 0.9),  # 2 — ω (lower claw tip)
                (0.40, 0.20, 0.7),  # 3 — lower claw joint
                # Head & body.
                (0.30, 0.30, 1.0),  # 4 — δ Dschubba (head)
                (0.32, 0.42, 0.9),  # 5 — σ
                (0.35, 0.54, 1.7),  # 6 — α Antares (brightest, body)
                (0.38, 0.64, 0.9),  # 7 — τ
                # Tail hooking right.
                (0.46, 0.74, 0.9),  # 8 — ε
                (0.56, 0.80, 0.9),  # 9 — μ
                (0.68, 0.82, 1.0),  # 10 — ζ
                (0.80, 0.72, 0.8),  # 11 — η
                (0.88, 0.55, 1.3),  # 12 — λ Shaula (stinger)
            ),
            "lines": (
                (0, 1), (1, 4),          # upper pincer → head
                (2, 3), (3, 4),          # lower pincer → head
                (4, 5), (5, 6), (6, 7),  # body
                (7, 8), (8, 9), (9, 10),
                (10, 11), (11, 12),      # tail curling to stinger
            ),
        },
    }

    def _render_constellation_svg(
        name: str, cx: float, cy: float, size: float,
        color: str = "#e4e4e7",
    ) -> str:
        """Place a constellation centered at (cx, cy), scaled to ``size`` px
        on its longest axis. Lines render first so dots draw on top."""
        const = _CONSTELLATIONS.get(name)
        if not const:
            return ""
        stars = const["stars"]
        parts: list[str] = []
        for i, j in const.get("lines", ()):
            x1n, y1n, _ = stars[i]
            x2n, y2n, _ = stars[j]
            x1 = cx + (x1n - 0.5) * size
            y1 = cy + (y1n - 0.5) * size
            x2 = cx + (x2n - 0.5) * size
            y2 = cy + (y2n - 0.5) * size
            parts.append(
                f'<line x1="{x1:.2f}" y1="{y1:.2f}" '
                f'x2="{x2:.2f}" y2="{y2:.2f}" '
                f'stroke="{color}" stroke-width="0.4" '
                f'stroke-opacity="0.35"/>'
            )
        for nx, ny, mag in stars:
            sx = cx + (nx - 0.5) * size
            sy = cy + (ny - 0.5) * size
            r = 0.85 * mag
            parts.append(
                f'<circle cx="{sx:.2f}" cy="{sy:.2f}" r="{r:.2f}" '
                f'fill="{color}" fill-opacity="0.85"/>'
            )
        return "".join(parts)

    def _radial_dial_svg(
        hours: list[int],
        highlight: int | None = None,
        bands: list[dict] | None = None,
        compact: bool = False,
        interactive: bool = False,
    ) -> str:
        """Render a 24-hour radial bar dial as inline SVG.

        Background = a translucent annulus segmented by the time-of-day
        bands (night, dawn, sunrise, day, sunset, dusk) at their actual
        fractional-hour edges. Foreground = one emerald wedge per hour with
        length ∝ detection count; the ``highlight`` hour gets an amber
        stroke. All geometry is computed here so the template stays
        declarative.

        ``compact`` drops the night-sky constellation and the hour labels and
        tightens the viewBox — for small map markers where that detail just
        reads as noise.

        ``interactive`` adds click-handle hooks for the dashboard hour
        drill-down modal: every hour wedge (including silent ones) gets
        ``data-hour`` + ``class="dial-wedge"`` so the JS event delegate can
        route clicks, and the centre hub is replaced with a clickable
        ``data-dial-centre`` circle. Off by default so the existing species
        diurnal popup renders byte-identically."""
        W, H = 220, 220
        cx, cy = W / 2.0, H / 2.0
        r_in, r_out = 26.0, 90.0
        max_v = max(hours) if hours else 0
        if max_v <= 0:
            max_v = 1
        slice_rad = math.pi / 12.0  # 15° per hour

        # Layout: 12:00 anchors the top of the dial and hours advance
        # clockwise — putting sunrise (~06:00) on the left and sunset
        # (~18:00) on the right, with night arcing across the bottom.
        def _dial_angle(h: float) -> float:
            return -math.pi / 2 + (h - 12.0) * slice_rad

        # Compact crops the label margin so the dial fills the marker box.
        view_box = "16 16 188 188" if compact else f"0 0 {W} {H}"
        parts: list[str] = [
            f'<svg viewBox="{view_box}" '
            f'width="{W}" height="{H}" '
            f'class="block mx-auto" '
            f'style="max-width:220px;height:auto">',
        ]

        # Background: annulus segments for each time-of-day band, drawn at
        # the band's actual fractional-hour edges. Night gets a starfield
        # sprinkled on top.
        if bands:
            for b in bands:
                start_h = max(0.0, b["start"])
                end_h = min(24.0, b["end"])
                if end_h - start_h < 0.01:
                    continue
                a1 = _dial_angle(start_h)
                a2 = _dial_angle(end_h)
                large_arc = 1 if (a2 - a1) > math.pi else 0
                x1o = cx + math.cos(a1) * r_out
                y1o = cy + math.sin(a1) * r_out
                x2o = cx + math.cos(a2) * r_out
                y2o = cy + math.sin(a2) * r_out
                x2i = cx + math.cos(a2) * r_in
                y2i = cy + math.sin(a2) * r_in
                x1i = cx + math.cos(a1) * r_in
                y1i = cy + math.sin(a1) * r_in
                d = (
                    f"M {x1o:.2f} {y1o:.2f} "
                    f"A {r_out:.2f} {r_out:.2f} 0 {large_arc} 1 {x2o:.2f} {y2o:.2f} "
                    f"L {x2i:.2f} {y2i:.2f} "
                    f"A {r_in:.2f} {r_in:.2f} 0 {large_arc} 0 {x1i:.2f} {y1i:.2f} Z"
                )
                fill = _DIAL_BAND_FILL.get(b["name"], "#27272a")
                opacity = _DIAL_BAND_OPACITY.get(b["name"], 0.30)
                parts.append(
                    f'<path d="{d}" fill="{fill}" '
                    f'fill-opacity="{opacity:.2f}"/>'
                )
                if b.get("name") == "night" and not compact:
                    # Pick the constellation that's high in the sky during
                    # this part of the night for southern Africa in May:
                    # Scorpius dominates the morning hours, Crux dominates
                    # the evening sky.
                    mid_h = (start_h + end_h) / 2
                    const_name = "scorpius" if mid_h < 12 else "crux"
                    a_mid = _dial_angle(mid_h)
                    r_mid = (r_in + r_out) / 2
                    center_x = cx + math.cos(a_mid) * r_mid
                    center_y = cy + math.sin(a_mid) * r_mid
                    # Conservative size so the figure stays inside the
                    # annulus even for short night spans.
                    size = min((r_out - r_in) - 6, 36)
                    parts.append(
                        _render_constellation_svg(
                            const_name, center_x, center_y, size
                        )
                    )

        # Inner hub: gains ``data-dial-centre`` + a pointer cursor when the
        # dial is interactive so the dashboard's hour-drill-down JS can
        # capture a centre click without needing a separate overlay shape.
        centre_attr = (
            ' data-dial-centre="1" style="cursor:pointer"' if interactive else ""
        )
        parts.extend([
            # Faint outer reference ring at max radius.
            f'<circle cx="{cx}" cy="{cy}" r="{r_out}" '
            f'fill="none" stroke="#27272a" stroke-width="1"/>',
            # Inner hub.
            f'<circle cx="{cx}" cy="{cy}" r="{r_in}" '
            f'fill="#0a0a0a" stroke="#27272a" stroke-width="1"'
            f'{centre_attr}/>',
        ])

        for h in range(24):
            v = hours[h] / max_v
            r = r_in + (r_out - r_in) * v
            a1 = _dial_angle(h)
            a2 = a1 + slice_rad
            cos1, sin1 = math.cos(a1), math.sin(a1)
            cos2, sin2 = math.cos(a2), math.sin(a2)
            x1i, y1i = cx + cos1 * r_in, cy + sin1 * r_in
            x2i, y2i = cx + cos2 * r_in, cy + sin2 * r_in
            x1o, y1o = cx + cos1 * r, cy + sin1 * r
            x2o, y2o = cx + cos2 * r, cy + sin2 * r
            # Silent hours: when interactive we still need an invisible click
            # target so every hour from 00–23 is selectable. A 4 px-thick
            # ring slice at r_in is invisible against the inner hub but big
            # enough to land a click.
            if hours[h] == 0:
                if not interactive:
                    continue
                r_silent = r_in + 4.0
                xso1, yso1 = cx + cos1 * r_silent, cy + sin1 * r_silent
                xso2, yso2 = cx + cos2 * r_silent, cy + sin2 * r_silent
                d = (
                    f"M {x1i:.2f} {y1i:.2f} "
                    f"L {xso1:.2f} {yso1:.2f} "
                    f"A {r_silent:.2f} {r_silent:.2f} 0 0 1 {xso2:.2f} {yso2:.2f} "
                    f"L {x2i:.2f} {y2i:.2f} "
                    f"A {r_in:.2f} {r_in:.2f} 0 0 0 {x1i:.2f} {y1i:.2f} Z"
                )
                parts.append(
                    f'<path d="{d}" fill="transparent" '
                    f'class="dial-wedge" data-hour="{h}" '
                    f'style="cursor:pointer"/>'
                )
                continue
            fill = _DIAL_DEFAULT_FILL
            opacity = 0.90
            stroke_attr = (
                ' stroke="#fbbf24" stroke-width="1.5"'
                if h == highlight else ""
            )
            d = (
                f"M {x1i:.2f} {y1i:.2f} "
                f"L {x1o:.2f} {y1o:.2f} "
                f"A {r:.2f} {r:.2f} 0 0 1 {x2o:.2f} {y2o:.2f} "
                f"L {x2i:.2f} {y2i:.2f} "
                f"A {r_in:.2f} {r_in:.2f} 0 0 0 {x1i:.2f} {y1i:.2f} Z"
            )
            title_el = (
                "" if compact else f"<title>{h:02d}:00 · {hours[h]} detections</title>"
            )
            interact_attr = (
                f' class="dial-wedge" data-hour="{h}" style="cursor:pointer"'
                if interactive else ""
            )
            parts.append(
                f'<path d="{d}" fill="{fill}" '
                f'fill-opacity="{opacity:.2f}"{stroke_attr}{interact_attr}>'
                f'{title_el}</path>'
            )

        if not compact:
            for h, label in ((0, "00"), (6, "06"), (12, "12"), (18, "18")):
                # Labels sit at the hour boundary (not wedge center) so the
                # cardinal hours land exactly at top/right/bottom/left.
                a = _dial_angle(h)
                x = cx + math.cos(a) * (r_out + 12)
                y = cy + math.sin(a) * (r_out + 12)
                parts.append(
                    f'<text x="{x:.2f}" y="{y:.2f}" '
                    f'text-anchor="middle" dominant-baseline="middle" '
                    f'fill="#71717a" font-size="10" '
                    f'font-family="ui-sans-serif, system-ui">{label}</text>'
                )

        parts.append("</svg>")
        return "".join(parts)

    _wmo_summary = weather_module.wmo_summary

    def _wp_fetch(title: str) -> dict | None:
        """Fetch the Wikipedia REST summary for a page title and return the
        thumbnail/page URL pair, or None if the page doesn't exist / has no
        image. Wikipedia accepts both scientific and common bird names; the
        scientific name is more reliable so we try that first."""
        if not title:
            return None
        title_enc = urllib.parse.quote(title.strip().replace(" ", "_"))
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title_enc}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "birdbrain/0.1"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
                data = json.load(resp)
        except Exception:
            return None
        thumb = (data.get("thumbnail") or {}).get("source")
        if not thumb:
            return None
        page = ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
        return {
            "thumbnail": thumb,
            "page_url": page,
            # Prefer plain ``title`` over ``displaytitle`` (the latter contains
            # HTML markup like <span lang="en">…</span>).
            "wp_title": data.get("title") or title,
            "extract": (data.get("extract") or "")[:280],
            # Wikidata QID for this page; lets us fall back to wdt:P181 for
            # species whose en.wiki article doesn't transclude a range map.
            "wikidata_qid": data.get("wikibase_item"),
        }

    def _wikidata_range_map(qid: str | None) -> dict | None:
        """Last-resort range-map lookup: read wdt:P181 (range map image) from
        Wikidata's per-entity REST endpoint, bypassing the SPARQL service
        (which is rate-limited during outages). Returns the same shape as
        ``_pick_range_map`` or None when no P181 claim is set."""
        if not qid or not qid.startswith("Q") or not qid[1:].isdigit():
            return None
        url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
        req = urllib.request.Request(
            url, headers={"User-Agent": "birdbrain/0.1"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
                data = json.load(resp)
        except Exception:
            return None
        entity = (data.get("entities") or {}).get(qid) or {}
        claims = (entity.get("claims") or {}).get("P181") or []
        for c in claims:
            val = (c.get("mainsnak") or {}).get("datavalue", {}).get("value")
            if isinstance(val, str) and val.strip():
                fname = val.strip()
                enc = urllib.parse.quote(fname.replace(" ", "_"))
                return {
                    # Special:FilePath redirects to the actual Commons URL and
                    # supports ?width=N for a sized thumbnail.
                    "range_map": (
                        f"https://commons.wikimedia.org/wiki/Special:FilePath/"
                        f"{enc}?width=330"
                    ),
                    "range_map_page": (
                        f"https://commons.wikimedia.org/wiki/File:{enc}"
                    ),
                }
        return None

    def _gbif_range_map(scientific: str) -> dict | None:
        """GBIF occurrence-density tile (CC-BY 4.0) — the geo-referenced range
        layer we always try, since it's the basemap our site dots sit on.
        Two cheap HTTPs — species/match to resolve the scientific name
        to a usageKey, then occurrence/count so we don't store a deterministic
        URL that renders as an empty world tile for taxa GBIF has no records
        for. Returns ``{range_map, range_map_page}`` or None.

        Tile params: classic.poly style fills hex-bins so range reads at a
        glance from the zoom-0 (whole-world) tile rather than as sparse pixel
        points; @2x suffix gives a retina-sharp 512×512 PNG."""
        name = (scientific or "").strip()
        if not name:
            return None
        try:
            req = urllib.request.Request(
                "https://api.gbif.org/v1/species/match?name="
                + urllib.parse.quote(name),
                headers={"User-Agent": "birdbrain/0.1"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
                data = json.load(resp)
        except Exception:
            return None
        key = data.get("usageKey")
        if not isinstance(key, int):
            return None
        if data.get("matchType") not in ("EXACT", "FUZZY"):
            return None
        try:
            req = urllib.request.Request(
                f"https://api.gbif.org/v1/occurrence/count?taxonKey={key}",
                headers={"User-Agent": "birdbrain/0.1"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
                n_records = int(resp.read().decode().strip() or "0")
        except Exception:
            n_records = 0
        if n_records < 1:
            return None
        tile = (
            f"https://api.gbif.org/v2/map/occurrence/density/0/0/0@2x.png"
            f"?taxonKey={key}&style=classic.poly&bin=hex&hexPerTile=30"
        )
        return {
            "range_map": tile,
            "range_map_page": f"https://www.gbif.org/species/{key}",
        }

    def _inat_range_map(scientific: str) -> dict | None:
        """iNaturalist tile fallback for the geo-referenced range layer: when
        GBIF has nothing, iNat sometimes does (citizen-science observations
        cover taxa where research-grade records are sparse). API is open,
        rate-limited ~100 req/min, content
        is CC BY-NC which composes cleanly with BirdNET's CC BY-NC-SA.

        Two HTTPs: /v1/taxa search to resolve a scientific name to a numeric
        taxon_id (we require an exact case-insensitive match on the result's
        ``name`` to avoid genus-only hits stealing the slot), then we trust
        the tile URL — iNat returns a transparent PNG for taxa with zero
        observations so the client-side rendering will simply look blank if
        the species has no data, which is harmless."""
        name = (scientific or "").strip()
        if not name:
            return None
        try:
            req = urllib.request.Request(
                "https://api.inaturalist.org/v1/taxa"
                "?rank=species&per_page=5&q="
                + urllib.parse.quote(name),
                headers={"User-Agent": "birdbrain/0.1"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
                data = json.load(resp)
        except Exception:
            return None
        wanted = name.lower()
        taxon_id: int | None = None
        for r in data.get("results", []):
            rname = (r.get("name") or "").strip().lower()
            if rname == wanted and isinstance(r.get("id"), int):
                taxon_id = r["id"]
                break
        if taxon_id is None:
            return None
        tile = (
            f"https://api.inaturalist.org/v1/colored_heatmap/0/0/0.png"
            f"?taxon_id={taxon_id}"
        )
        return {
            "range_map": tile,
            "range_map_page": f"https://www.inaturalist.org/taxa/{taxon_id}",
        }

    def _resolve_species_media(scientific: str, common: str = "") -> dict:
        """Photo + range-map + IUCN status for a species, behind two cache
        layers: a process-local 24h cache and a durable per-species row in the
        DB (``media_fetched_at``). Only a live Wikipedia hit reaches the
        network; the background sweeper warms the DB layer for every species."""
        key = scientific.strip().lower()
        now = time.monotonic()
        cached = _wp_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        # Durable DB layer — skip Wikipedia entirely when we looked recently
        # (even if the lookup found no map: media_fetched_at is stamped either
        # way so we don't re-hit Wikipedia for species that simply have none).
        row = db.get_species_note(scientific)
        if row is not None and row.media_fetched_at is not None:
            ts = row.media_fetched_at
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts > datetime.now(UTC) - timedelta(days=_MEDIA_DB_TTL_DAYS):
                payload = {
                    "thumbnail": row.image_url,
                    "page_url": row.image_page_url,
                    "range_map": row.range_map_url,
                    "range_map_page": row.range_map_page_url,
                    "range_tile": row.range_tile_url,
                    "range_tile_page": row.range_tile_page_url,
                    "conservation_status": row.conservation_status,
                }
                _wp_cache[key] = (now + _WP_TTL_SECONDS, payload)
                return payload
        payload = (
            _wp_fetch(scientific)
            or _wp_fetch(common)
            or {"thumbnail": None, "page_url": None}
        )
        # Augment with the Wikipedia range/distribution map and IUCN status
        # icon. One image-list fetch covers both — resolved against the same
        # page title we landed on (or the input scientific/common name).
        for title in (payload.get("wp_title"), scientific, common):
            if not title:
                continue
            images = _wp_article_images(title)
            if not images:
                continue
            map_payload = _pick_range_map(images)
            if map_payload:
                payload = {**payload, **map_payload}
            status = _pick_status(images)
            if status:
                payload["conservation_status"] = status
            break
        # Static natural-range image (Wikipedia → Wikidata). This is the clean
        # IUCN/BirdLife range polygon, shown as a small reference thumbnail.
        # Wikidata fallback: many species (Barn Owl, Common Bulbul, …) have a
        # P181 range-map image on their Wikidata item even though it isn't
        # transcluded in the en.wiki article. One extra HTTP per species, only
        # when Wikipedia didn't provide one.
        if not payload.get("range_map"):
            wd = _wikidata_range_map(payload.get("wikidata_qid"))
            if wd:
                payload = {**payload, **wd}
        # Geo-referenced occurrence tile (GBIF → iNat), ALWAYS attempted and
        # stored separately from the static image: this is the live Leaflet map
        # that carries our site dots, so we want it even when a clean IUCN
        # polygon image already exists. GBIF (CC-BY 4.0) covers ~all bird
        # species via the occurrence-density tile; iNat (CC BY-NC) is the
        # fallback for taxa GBIF has no records for.
        tile = _gbif_range_map(scientific) or _inat_range_map(scientific)
        if tile:
            payload["range_tile"] = tile["range_map"]
            payload["range_tile_page"] = tile["range_map_page"]
        # Persist only when we actually reached Wikipedia and got something —
        # otherwise a transient hiccup would poison both caches (and stamp
        # media_fetched_at, suppressing retries for 30 days).
        useful = (
            payload.get("thumbnail")
            or payload.get("range_map")
            or payload.get("range_tile")
            or payload.get("conservation_status")
        )
        if useful:
            if payload.get("conservation_status"):
                db.set_species_status(scientific, payload["conservation_status"])
            db.set_species_media(
                scientific,
                common_name=common,
                image_url=payload.get("thumbnail"),
                image_page_url=payload.get("page_url"),
                range_map_url=payload.get("range_map"),
                range_map_page_url=payload.get("range_map_page"),
                range_tile_url=payload.get("range_tile"),
                range_tile_page_url=payload.get("range_tile_page"),
            )
            _wp_cache[key] = (now + _WP_TTL_SECONDS, payload)
        return payload

    @app.get("/api/species_image")
    def species_image(
        scientific: str = Query(..., min_length=2, max_length=200),
        common: str = Query(default="", max_length=200),
    ) -> JSONResponse:
        return JSONResponse(_resolve_species_media(scientific, common))

    # Tokens (split on non-letter chars) that mark an image as a species range
    # map. We tokenize so 'range' doesn't match 'orange' and 'map' doesn't
    # match within unrelated filenames.
    _RANGE_TOKENS = {"distribution", "range", "rangemap", "map", "habitatmap"}
    _RANGE_EXCLUDES = ("status_iucn", "commons-logo", "ooui_icon", "oojs_ui")

    _RANGE_SPLIT = re.compile(r"[^a-z0-9]+")
    # BirdLife/IUCN range maps are commonly named "<Species>IUCNver2018.png" or
    # "<Species>IUCN2019-2.png" — no 'range'/'map' token, so token-matching
    # alone misses them. Catch "iucn" followed by "ver" or a 4-digit year. The
    # small status icon ("Status iucn3.1 LC") has only "iucn3.1" after iucn, so
    # it won't match (and is also skipped explicitly in _pick_range_map).
    _RANGE_IUCN_RE = re.compile(r"iucn[ _]?(?:ver|\d{4})", re.IGNORECASE)
    # IUCN status icon filenames: "Status_iucn3.1_LC.svg", "Status_iucn_VU.svg",
    # legacy "Status_LC.svg". Code is the last all-caps token before .svg.
    _IUCN_STATUS_RE = re.compile(
        r"Status[_ ](?:iucn[\d.]*[_ ])?([A-Z]{2,3})\.svg",
        re.IGNORECASE,
    )
    # Canonical codes — guard against accidental matches on unrelated filenames.
    _IUCN_CODES = {"LC", "NT", "VU", "EN", "CR", "EW", "EX", "DD", "NE"}
    # Extinct codes. A species we're detecting from live audio is by definition
    # not extinct, so an EX/EW status icon in its article always belongs to an
    # extinct subspecies or relative shown on the same page (e.g. the Common
    # Buttonquail article carries both EX, for its extinct Andalusian race, and
    # LC, for the species). The Wikipedia images API returns them alphabetically,
    # so EX would otherwise win — skip extinct icons entirely.
    _EXTINCT_CODES = {"EX", "EW"}

    def _wp_article_images(title: str) -> list[str]:
        """Fetch the list of image File: titles for a Wikipedia article. We
        reuse this list to find both a range map and an IUCN status icon in
        a single round-trip."""
        if not title:
            return []
        title_enc = urllib.parse.quote(title.strip().replace(" ", "_"))
        list_url = (
            "https://en.wikipedia.org/w/api.php"
            f"?action=query&prop=images&titles={title_enc}"
            "&redirects=1&format=json&imlimit=80"
        )
        req = urllib.request.Request(
            list_url, headers={"User-Agent": "birdbrain/0.1"}
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
                data = json.load(resp)
        except Exception:
            return []
        out: list[str] = []
        for page in (data.get("query") or {}).get("pages", {}).values():
            for img in page.get("images") or []:
                t = img.get("title")
                if t:
                    out.append(t)
        return out

    def _pick_status(images: list[str]) -> str | None:
        for t in images:
            m = _IUCN_STATUS_RE.search(t)
            if m:
                code = m.group(1).upper()
                if code in _IUCN_CODES and code not in _EXTINCT_CODES:
                    return code
        return None

    def _pick_range_map(images: list[str]) -> dict | None:
        candidates: list[str] = []
        for t in images:
            tl = t.lower()
            if any(x in tl for x in _RANGE_EXCLUDES):
                continue
            if _IUCN_STATUS_RE.search(t):
                continue  # the small conservation-status icon, not a range map
            tokens = {tk for tk in _RANGE_SPLIT.split(tl) if tk}
            if (tokens & _RANGE_TOKENS) or _RANGE_IUCN_RE.search(tl):
                candidates.append(t)
        if not candidates:
            return None
        # Prefer 'distribution' over 'range' over an IUCN range map over plain 'map'.
        def _rank(t: str) -> int:
            tl = t.lower()
            if "distribution" in tl:
                return 0
            if "range" in tl:
                return 1
            if _RANGE_IUCN_RE.search(tl):
                return 2
            return 3
        candidates.sort(key=_rank)
        fname = candidates[0]
        fname_enc = urllib.parse.quote(fname.replace(" ", "_"))
        info_url = (
            "https://en.wikipedia.org/w/api.php"
            f"?action=query&titles={fname_enc}"
            "&prop=imageinfo&iiprop=url&iiurlwidth=320&format=json"
        )
        req2 = urllib.request.Request(
            info_url, headers={"User-Agent": "birdbrain/0.1"}
        )
        try:
            with urllib.request.urlopen(req2, timeout=8) as resp:  # noqa: S310
                ii_data = json.load(resp)
        except Exception:
            return None
        for page in (ii_data.get("query") or {}).get("pages", {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            thumb = info.get("thumburl")
            desc = info.get("descriptionurl")
            if thumb:
                return {"range_map": thumb, "range_map_page": desc}
        return None

    @app.get("/api/species_notes/{scientific_name:path}")
    def get_species_note(scientific_name: str) -> JSONResponse:
        row = db.get_species_note(scientific_name)
        if row is None:
            return JSONResponse(
                {
                    "scientific_name": scientific_name,
                    "note": "",
                    "tag": None,
                    "min_confidence": None,
                }
            )
        return JSONResponse(
            {
                "scientific_name": row.scientific_name,
                "common_name": row.common_name,
                "note": row.note,
                "tag": row.tag,
                "min_confidence": row.min_confidence,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )

    @app.post("/api/species_notes/{scientific_name:path}")
    async def post_species_note(request: Request, scientific_name: str) -> JSONResponse:
        form = await request.form()
        note = (form.get("note") or "").strip()
        common_name = (form.get("common_name") or "").strip()
        tag_raw = (form.get("tag") or "").strip()
        tag = tag_raw or None
        if tag is not None and tag not in ("reliable", "suspect", "rare"):
            raise HTTPException(400, "tag must be reliable/suspect/rare or empty")

        # Optional per-species min_confidence override. Only applied when the
        # field is present in the form (else we leave whatever was there).
        # Empty string clears it.
        mc_raw = form.get("min_confidence")
        mc_supplied = mc_raw is not None
        mc_value: float | None = None
        if mc_supplied and (mc_raw or "").strip():
            try:
                mc_value = float(mc_raw)
            except ValueError as e:
                raise HTTPException(400, "min_confidence must be a number") from e
            if not (0.0 <= mc_value <= 1.0):
                raise HTTPException(400, "min_confidence must be in [0, 1]")

        # When there's no note text AND no threshold to set, treat as delete.
        if not note and (not mc_supplied or mc_value is None):
            existing = db.get_species_note(scientific_name)
            if existing is not None and existing.min_confidence is not None and not mc_supplied:
                # The user is clearing just the note; keep the threshold.
                pass
            else:
                db.delete_species_note(scientific_name)
                return JSONResponse({"deleted": True, "scientific_name": scientific_name})

        if note:
            row = db.set_species_note(
                scientific_name, common_name=common_name, note=note, tag=tag
            )
        else:
            # Note empty but we have a threshold to persist — keep existing
            # note/tag if any.
            existing = db.get_species_note(scientific_name)
            if existing is None:
                row = db.set_species_note(
                    scientific_name, common_name=common_name, note="", tag=tag
                )
            else:
                row = existing
        if mc_supplied:
            db.set_species_min_confidence(scientific_name, mc_value)
            row = db.get_species_note(scientific_name)
        return JSONResponse(
            {
                "scientific_name": row.scientific_name,
                "common_name": row.common_name,
                "note": row.note,
                "tag": row.tag,
                "min_confidence": row.min_confidence,
                "updated_at": row.updated_at.isoformat(),
            }
        )

    # Distribution view — aggregates lat/lng of XC recordings into a points
    # payload the modal renders as an inline SVG dot-map. Separate cache from
    # the comparison fetch above so a long page-load doesn't block the audio
    # compare panel.
    _xc_dist_cache: dict[str, tuple[float, dict]] = {}
    _XC_DIST_TTL_SECONDS = 24 * 3600

    @app.get("/api/xeno_canto/distribution")
    def xeno_canto_distribution(
        species: str = Query(..., min_length=2, max_length=200),
        max_points: int = Query(default=400, ge=10, le=1500),
    ) -> JSONResponse:
        """Lat/lng + country aggregates for XC recordings of a species.
        Used by the audition modal to draw a small dot-map of where the
        species has actually been recorded by XC contributors — fills the
        gap when Wikipedia doesn't have a range map."""
        species_clean = species.strip()
        api_key = (cfg.xeno_canto_key or "").strip()
        if not api_key:
            return JSONResponse({"points": [], "top_countries": [], "total": 0, "needs_key": True})

        cache_key = species_clean.lower()
        now = time.monotonic()
        cached = _xc_dist_cache.get(cache_key)
        if cached and cached[0] > now:
            payload = cached[1]
            return JSONResponse({**payload, "points": payload["points"][:max_points]})

        tokens = species_clean.split()
        if (
            len(tokens) == 2
            and tokens[0][:1].isupper()
            and tokens[1][:1].islower()
        ):
            tag_query = f"gen:{tokens[0]} sp:{tokens[1]}"
        else:
            tag_query = f'en:"{species_clean}"'

        params = urllib.parse.urlencode({"query": tag_query, "key": api_key})
        url = f"https://xeno-canto.org/api/3/recordings?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "birdbrain/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                data = json.load(resp)
        except Exception as e:
            raise HTTPException(502, f"xeno-canto fetch failed: {e}") from e

        points: list[list[float]] = []
        country_counts: dict[str, int] = {}
        for r in data.get("recordings") or []:
            # XC v3 uses 'lon'; the older API examples often show 'lng'. Try
            # both so we don't silently drop everything if the field name
            # ever shifts back.
            lat_raw = r.get("lat")
            lon_raw = r.get("lon", r.get("lng"))
            try:
                if lat_raw is None or lon_raw is None:
                    continue
                lat = float(lat_raw)
                lon = float(lon_raw)
            except (TypeError, ValueError):
                continue
            # Drop nonsensical coords (lat must be -90..90, lon -180..180; XC
            # uploaders sometimes mis-enter and we don't want 0,0 stragglers
            # dominating the view).
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                continue
            if lat == 0.0 and lon == 0.0:
                continue
            points.append([round(lat, 3), round(lon, 3)])
            cnt = r.get("cnt") or ""
            if cnt:
                country_counts[cnt] = country_counts.get(cnt, 0) + 1

        top_countries = sorted(
            ({"name": c, "count": n} for c, n in country_counts.items()),
            key=lambda x: -x["count"],
        )[:8]
        payload = {
            "total": int(data.get("numRecordings") or 0),
            "plotted": len(points),
            "points": points,
            "top_countries": top_countries,
        }
        # Only cache positive results — keeps a transient hiccup from poisoning
        # the cache for a day.
        if points or top_countries:
            _xc_dist_cache[cache_key] = (now + _XC_DIST_TTL_SECONDS, payload)
        return JSONResponse({**payload, "points": payload["points"][:max_points]})

    @app.get("/api/xeno_canto")
    def xeno_canto(
        species: str = Query(..., min_length=2, max_length=200),
        limit: int = Query(default=3, ge=1, le=8),
    ) -> JSONResponse:
        """Top reference recordings for a species from xeno-canto.org. Cached
        in-memory for 6h per species string. Always returns a ``search_url``
        the UI can link to; ``recordings`` is only populated when an API key
        is configured via ``BIRDBRAIN_XENO_CANTO_KEY``."""
        species_clean = species.strip()
        search_url = (
            "https://xeno-canto.org/explore?"
            + urllib.parse.urlencode({"query": species_clean})
        )
        api_key = (cfg.xeno_canto_key or "").strip()
        if not api_key:
            return JSONResponse(
                {"recordings": [], "search_url": search_url, "needs_key": True}
            )

        cache_key = species_clean.lower()
        now = time.monotonic()
        cached = _xc_cache.get(cache_key)
        if cached and cached[0] > now:
            return JSONResponse(
                {
                    "recordings": cached[1][:limit],
                    "search_url": search_url,
                    "needs_key": False,
                }
            )

        # XC v3 only accepts tagged queries (no free text). Build one from the
        # input: "Genus species" → gen:Genus sp:species; anything else is
        # treated as an English-name search (en:).
        tokens = species_clean.split()
        if (
            len(tokens) == 2
            and tokens[0][:1].isupper()
            and tokens[1][:1].islower()
        ):
            tag_query = f"gen:{tokens[0]} sp:{tokens[1]} q_gt:C"
        else:
            tag_query = f'en:"{species_clean}" q_gt:C'

        params = urllib.parse.urlencode({"query": tag_query, "key": api_key})
        url = f"https://xeno-canto.org/api/3/recordings?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "birdbrain/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (https only)
                data = json.load(resp)
        except Exception as e:  # network, timeout, JSON, etc.
            raise HTTPException(502, f"xeno-canto fetch failed: {e}") from e

        recs_in = data.get("recordings") or []
        recs_out: list[dict] = []
        for r in recs_in:
            sono = r.get("sono") or {}
            recs_out.append(
                {
                    "id": r.get("id"),
                    "en": r.get("en") or "",
                    "sci": f"{r.get('gen','')} {r.get('sp','')}".strip(),
                    "rec": r.get("rec") or "",
                    "cnt": r.get("cnt") or "",
                    "loc": r.get("loc") or "",
                    "type": r.get("type") or "",
                    "length": r.get("length") or "",
                    "q": r.get("q") or "",
                    "url": _https(r.get("url")),
                    "file": _https(r.get("file")),
                    "sono_small": _https(sono.get("small")),
                    "sono_med": _https(sono.get("med")),
                }
            )
        _xc_cache[cache_key] = (now + _XC_TTL_SECONDS, recs_out)
        return JSONResponse(
            {
                "recordings": recs_out[:limit],
                "search_url": search_url,
                "needs_key": False,
            }
        )

    @app.get("/api/sites/{name}/live.mp3")
    async def site_live_audio(request: Request, name: str) -> StreamingResponse:
        """Proxy a site's LIVE audio as a streaming MP3 so the admin page can
        audition it (and tap it with Web Audio for a spectrogram — only
        same-origin audio can be analysed in-browser). Resolves the stream URL
        via yt-dlp (off the event loop), then pipes it through ffmpeg to mono
        MP3. Async so client disconnect cancels the generator and kills ffmpeg
        promptly; a 10-min ffmpeg cap is the backstop for a forgotten tab."""
        # LAN-only: each listener spawns a dedicated ffmpeg (+ a yt-dlp resolve
        # for YouTube sources) on the Pi, so this is real per-request load — and
        # it's purely an /admin auditioning tool, which is itself LAN-only. The
        # restrict_public middleware lets anonymous public GETs through, so gate
        # it here against the tunnel. 404 (not 403) to mirror the admin block.
        if getattr(request.state, "is_public", False):
            raise HTTPException(404, "Not found")
        cfg_src = _all_sources()[1].get(name)
        if cfg_src is None:
            raise HTTPException(404, "No such source")

        if cfg_src.kind == "youtube":
            from birdbrain.audio.youtube import YouTubeSource

            def _resolve() -> str:
                return YouTubeSource(
                    name=cfg_src.name,
                    url=cfg_src.url,
                    cookies_file=str(cfg_src.cookies_file) if cfg_src.cookies_file else None,
                ).current_url()

            try:
                # Bound the yt-dlp resolve so a slow/rate-limited lookup fails
                # the request fast instead of hanging it.
                stream_url = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=25)
            except TimeoutError as e:
                raise HTTPException(504, "stream resolve timed out (try again)") from e
            except Exception as e:
                raise HTTPException(502, f"could not resolve stream: {e}") from e
            input_args = [
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5", "-i", stream_url,
            ]
        elif cfg_src.kind == "device":
            # Local mic. A PipeWire/pulse source ("pulse:…") can be read live
            # alongside the detection worker (PipeWire shares the capture); raw
            # ALSA is exclusive, so audition only works for the pulse form.
            if cfg_src.url.startswith("pulse:"):
                input_args = ["-f", "pulse", "-i", cfg_src.url[len("pulse:"):]]
            else:
                raise HTTPException(
                    409, "mic is on raw ALSA (exclusive) — route it via pulse: to audition"
                )
        elif cfg_src.url.startswith("tbb://"):
            # External TinyBirdBrain unit. We can't reach its mic directly, so we
            # proxy the unit's own LAN /live.mp3 (it shares its mic via dsnoop).
            # The unit's address is held in an app_setting keyed by source name so
            # it's editable without a schema change / redeploy. ffmpeg reconnects
            # if the unit blips; the same mp3-pump below re-serves it same-origin
            # (needed for the in-browser spectrogram tap).
            live_url = db.get_setting(f"live_audio_url:{name}")
            if not live_url:
                raise HTTPException(
                    409, "no live-audio URL configured for this unit (set live_audio_url:<name>)"
                )
            input_args = [
                "-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5", "-i", live_url,
            ]
        else:
            raise HTTPException(404, "No live audio for this source")

        # Mirror the source's high-pass cutoff so the audition sounds like what
        # the detector hears (and so the cutoff slider previews live on re-fetch).
        filter_args: list[str] = []
        if cfg_src.kind == "device":
            hp_raw = db.get_setting(f"highpass_hz:{name}")
            try:
                hp = int(float(hp_raw)) if hp_raw else 0
            except (TypeError, ValueError):
                hp = 0
            if hp > 0:
                filter_args = ["-af", f"highpass=f={hp}"]

        # Plain Popen (asyncio subprocesses are unreliable under uvicorn), read
        # off the event loop via to_thread so the request can still be cancelled
        # on disconnect. The finally kills ffmpeg, which closes the pipe and
        # unblocks the reader thread — no orphaned ffmpeg.
        proc = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-t", "600",  # 10-min safety cap so a forgotten tab can't stream forever
                *input_args,
                *filter_args,
                "-vn", "-ac", "1", "-ar", "44100", "-b:a", "96k",
                "-f", "mp3", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        async def _pump():
            try:
                while True:
                    # Explicit disconnect check each loop — deterministic
                    # cleanup rather than relying on cancellation propagating
                    # through the read thread.
                    if await request.is_disconnected():
                        break
                    chunk = await asyncio.to_thread(proc.stdout.read, 8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if proc.poll() is None:
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass

        return StreamingResponse(
            _pump(),
            media_type="audio/mpeg",
            headers={"Cache-Control": "no-store"},
        )

    def _unit_base_url(source: str) -> str | None:
        """Base http://host:port for a push-fed unit, derived from its configured
        live-audio URL (the unit serves /clips on the same port). None if the
        source has no live-audio URL set.

        Clips only — units no longer render spectrograms; central regenerates
        them from the clip it pulls through here."""
        u = db.get_setting(f"live_audio_url:{source}")
        if not u:
            return None
        p = urllib.parse.urlsplit(u)
        return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None

    def _materialize_external_clip(detection_id: int) -> Path | None:
        """For a synced unit (TBB) detection with no local clip, fetch the clip
        from the unit on demand, cache it under clips_root, and set clip_path so
        the normal clip/spectrogram machinery then works. Returns the local Path,
        or None if there's no client_id, the unit is unreachable, or the clip was
        pruned. Clips are only pulled when a detection is actually auditioned."""
        with db.session() as s:
            row = s.get(DetectionRow, detection_id)
            if row is None or row.clip_path is not None or not row.client_id:
                return None
            source, client_id = row.source_name, row.client_id
        base = _unit_base_url(source)
        if not base:
            return None
        local_id = client_id.rsplit(":", 1)[-1]
        dest_dir = clips_root / "_units" / re.sub(r"[^A-Za-z0-9_.-]", "_", source)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{detection_id}.ogg"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", f"{base}/clips/{local_id}",
                 "-c:a", "libvorbis", "-q:a", "4", str(dest)],
                check=True, capture_output=True, timeout=20,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if not dest.is_file() or dest.stat().st_size == 0:
            return None
        db.set_clip_path(detection_id, str(dest))
        return dest

    @app.get("/spectrograms/{detection_id}.png")
    def spectrogram(
        detection_id: int,
        size: str = Query(default="small"),
    ) -> Response:
        """PNG spectrogram of a detection's clip. Generated lazily on first
        request via ffmpeg's showspectrumpic filter and cached next to the
        WAV. ``size=small`` is used inline; ``large`` for the popout view.
        For push-fed (TBB) detections the clip is fetched from the unit on the
        first request, then this works exactly as for a local clip."""
        if size not in SPEC_SIZES:
            raise HTTPException(400, "size must be 'small' or 'large'")
        with db.session() as s:
            row = s.get(DetectionRow, detection_id)
            if row is None:
                raise HTTPException(404, "no clip for this detection")
            clip_path = row.clip_path
            note = s.get(SpeciesNoteRow, row.scientific_name)
            tag = note.tag if note else None
        if clip_path is None:
            materialized = _materialize_external_clip(detection_id)
            if materialized is None:
                raise HTTPException(404, "no clip for this detection")
            clip_path = str(materialized)
        wav = Path(clip_path).resolve()
        try:
            wav.relative_to(clips_root)
        except ValueError as e:
            raise HTTPException(403, "clip outside allowed root") from e
        if not wav.is_file():
            raise HTTPException(404, "clip file missing")

        palette = SPEC_PALETTE_FOR_TAG.get(tag, "fire")
        size_suffix = "" if size == "small" else f".{size}"
        # Cache by palette so re-tagging a species regenerates the colour
        # without colliding with the old file.
        png = wav.parent / f"{wav.stem}.{palette}{size_suffix}.png"
        if not png.exists():
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        "-i", str(wav),
                        "-lavfi",
                        f"showspectrumpic=s={SPEC_SIZES[size]}:{SPEC_FILTER_BASE}:color={palette}",
                        str(png),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=15,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
                raise HTTPException(500, f"failed to render spectrogram: {e}") from e
        return FileResponse(
            png, media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},  # CDN-cacheable; recoloured only on rare re-tag
        )

    @app.get("/clips/{detection_id}")
    def clip(detection_id: int, fmt: str = Query(default="auto")) -> FileResponse:
        """Serve a detection clip. iOS Safari's OGG playback is jerky and
        currentTime resolution is poor, which breaks the spectrogram
        playhead sync; transcode to MP3 (cached on disk next to the OGG)
        and serve that by default so audio works the same across browsers.
        Use ``?fmt=original`` for the underlying OGG if you really want it.
        """
        with db.session() as s:
            row = s.get(DetectionRow, detection_id)
            if row is None:
                raise HTTPException(status_code=404, detail="No clip for this detection")
            clip_path = row.clip_path
        if clip_path is None:
            materialized = _materialize_external_clip(detection_id)
            if materialized is None:
                raise HTTPException(status_code=404, detail="No clip for this detection")
            clip_path = str(materialized)
        path = Path(clip_path).resolve()
        try:
            path.relative_to(clips_root)
        except ValueError as e:
            raise HTTPException(status_code=403, detail="Clip outside allowed root") from e
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Clip file missing")

        if fmt == "auto" and path.suffix.lower() == ".ogg":
            mp3 = path.parent / f"{path.stem}.mp3"
            if not mp3.exists():
                try:
                    subprocess.run(
                        [
                            "ffmpeg", "-y", "-loglevel", "error",
                            "-i", str(path),
                            "-c:a", "libmp3lame", "-q:a", "4",
                            str(mp3),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=15,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                    # Fall back to OGG if transcode fails for any reason.
                    mp3 = None
            if mp3 and mp3.is_file():
                return FileResponse(
                    mp3, media_type="audio/mpeg",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"},
                )

        media_types = {".wav": "audio/wav", ".ogg": "audio/ogg", ".flac": "audio/flac", ".mp3": "audio/mpeg"}
        return FileResponse(
            path, media_type=media_types.get(path.suffix.lower(), "audio/wav"),
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # --- TBB ingest: token-authed detection upload from capture units (Phase 2) ---
    # Public-reachable but token-gated (see the restrict_public allowlist above).
    _INGEST_MAX_BODY = 2_000_000  # bytes
    _INGEST_RATE_PER_MIN = 30
    _ingest_hits: dict[str, list[float]] = {}

    def _ingest_rate_ok(unit_id: str) -> bool:
        now = time.monotonic()
        hits = [t for t in _ingest_hits.get(unit_id, []) if now - t < 60.0]
        if len(hits) >= _INGEST_RATE_PER_MIN:
            _ingest_hits[unit_id] = hits
            return False
        hits.append(now)
        _ingest_hits[unit_id] = hits
        return True

    @app.post("/ingest/detections")
    def ingest(request: Request, body: IngestBody) -> dict:
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > _INGEST_MAX_BODY:
            raise HTTPException(413, "payload too large")
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        device = db.device_by_token(hash_token(auth[len("Bearer "):].strip()))
        if device is None or not device.sync_enabled:
            raise HTTPException(403, "invalid token or sync disabled")
        if not _ingest_rate_ok(device.unit_id):
            raise HTTPException(429, "rate limit exceeded")
        try:
            return ingest_batch(db, device, body)
        except UnsupportedSchemaError as e:
            # 409, not 400. A unit must be able to tell "you and I disagree
            # about the format" from "that payload was malformed": the first is
            # permanent until somebody updates one side, so retrying is
            # pointless and the unit stops and says so instead (tbb_sync).
            log.warning(
                "ingest.unsupported_schema", unit=device.unit_id, got=e.got,
                supported=sorted(SUPPORTED_SCHEMAS),
            )
            raise HTTPException(SCHEMA_CONFLICT_STATUS, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    _enroll_hits: dict[str, list[float]] = {}
    _ENROLL_RATE_PER_MIN = 10

    def _enroll_rate_ok(client_ip: str) -> bool:
        now = time.monotonic()
        hits = [t for t in _enroll_hits.get(client_ip, []) if now - t < 60.0]
        if len(hits) >= _ENROLL_RATE_PER_MIN:
            _enroll_hits[client_ip] = hits
            return False
        hits.append(now)
        _enroll_hits[client_ip] = hits
        return True

    # --- Visitor analytics: anonymous client beacon (path + dwell on tab-hide) ---
    _TRACK_MAX_BODY = 4_000  # bytes — a tiny JSON beacon
    _TRACK_RATE_PER_MIN = 120
    _track_hits: dict[str, list[float]] = {}
    # Paths that are operator/plumbing, not visitor-facing content — never logged.
    _TRACK_SKIP_PREFIXES = (
        "/admin", "/api/", "/auth/", "/partials/", "/static/",
        "/clips/", "/spectrograms/", "/login", "/signup",
    )

    def _track_rate_ok(ip: str) -> bool:
        now = time.monotonic()
        hits = [t for t in _track_hits.get(ip, []) if now - t < 60.0]
        if len(hits) >= _TRACK_RATE_PER_MIN:
            _track_hits[ip] = hits
            return False
        hits.append(now)
        _track_hits[ip] = hits
        return True

    @app.post("/api/track")
    async def track(request: Request) -> Response:
        """Record one anonymous public page view from the client beacon. Always
        returns 204 (never errors the beacon); silently drops anything invalid,
        non-public, oversized, or rate-limited. Only public (Cloudflare) traffic
        is counted, so the operator's own LAN browsing isn't logged."""
        # Only count real external visitors (LAN/operator views carry no cf header).
        ip = request.headers.get("cf-connecting-ip")
        if ip is None:
            return Response(status_code=204)
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > _TRACK_MAX_BODY:
            return Response(status_code=204)
        if not _track_rate_ok(ip):
            return Response(status_code=204)
        try:
            data = await request.json()
        except Exception:
            return Response(status_code=204)
        path = str(data.get("path") or "")[:256]
        visitor = str(data.get("visitor") or "")[:64]
        if not path.startswith("/") or not visitor or path.startswith(_TRACK_SKIP_PREFIXES):
            return Response(status_code=204)
        referrer = str(data.get("referrer"))[:256] if data.get("referrer") else None
        try:
            dwell_ms: int | None = int(data.get("dwell_ms"))
            if dwell_ms < 0 or dwell_ms > 3_600_000:
                dwell_ms = None
        except (TypeError, ValueError):
            dwell_ms = None
        db.add_pageview(visitor=visitor, path=path, referrer=referrer, dwell_ms=dwell_ms)
        return Response(status_code=204)

    @app.post("/enroll")
    def enroll_route(request: Request, body: EnrollBody) -> dict:
        # Rate-limit by client IP (no unit identity yet) to blunt code guessing.
        client_ip = request.headers.get("cf-connecting-ip") or (
            request.client.host if request.client else "?"
        )
        if not _enroll_rate_ok(client_ip):
            raise HTTPException(429, "rate limit exceeded")
        try:
            return enroll(db, body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    def _media_sweep_loop() -> None:
        """Warm the durable media cache: fetch Wikipedia photo + range map for
        every detected species that's missing or stale, then re-sweep on an
        interval so newly-detected species get picked up too. Rate-limited to
        stay polite to Wikimedia; all the heavy lifting (and the don't-refetch
        stamping) lives in _resolve_species_media / set_species_media."""
        time.sleep(20)  # let the app settle before reaching out to Wikipedia
        batch = 25
        while True:
            drained = True
            try:
                todo = db.species_needing_media(
                    limit=batch, max_age_days=_MEDIA_DB_TTL_DAYS
                )
                if todo:
                    log.info("media_sweep.batch", count=len(todo))
                for sci, common in todo:
                    try:
                        _resolve_species_media(sci, common)
                    except Exception as e:  # one bad species shouldn't stall the sweep
                        log.warning("media_sweep.species_failed", sci=sci, error=str(e)[:200])
                    time.sleep(1.5)  # be gentle on Wikimedia's API
                # A full batch means more backlog likely remains — keep draining
                # promptly; otherwise idle and just re-check for new species.
                drained = len(todo) < batch
            except Exception as e:
                log.warning("media_sweep.tick_failed", error=str(e)[:200])
            time.sleep(1800 if drained else 30)

    if cfg.media_cache_enabled:
        # Run the sweeper in only ONE web worker (advisory flock in the data dir)
        # so multiple uvicorn workers don't all hammer Wikimedia or double the
        # DB writes / load spikes.
        _sweep_lock = open(clips_root.parent / ".media-sweeper.lock", "w")
        if _acquire_singleton_lock(_sweep_lock):
            _SINGLETON_LOCKS.append(_sweep_lock)  # hold the lock for process lifetime
            threading.Thread(
                target=_media_sweep_loop, name="media-sweeper", daemon=True
            ).start()
        else:
            _sweep_lock.close()  # another worker already runs the sweeper
            log.info("media_sweep.not_leader")

    return app


# Module-level app instance for `uvicorn birdbrain.web.app:app` and reload mode.
app = create_app()
