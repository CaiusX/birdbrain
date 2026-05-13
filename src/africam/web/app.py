from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, case, desc, func, or_, select

from africam.config import AppConfig, SourceConfig, load_sources
from africam.site_resolver import state_to_resolved
from africam.sites import Site, load_sites
from africam.storage import Database, DetectionRow, SpeciesNoteRow, WorkerHeartbeatRow

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


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
    return dt.astimezone(tz).strftime(fmt)


def _tz_abbr(tz_name: str | None) -> str:
    """Short label like SAST/UTC/EST for a column header. Datetime-derived so
    it stays correct across DST."""
    if not tz_name:
        return "UTC"
    try:
        return datetime.now(ZoneInfo(tz_name)).tzname() or tz_name
    except ZoneInfoNotFoundError:
        return tz_name


TEMPLATES.env.filters["localtime"] = _localtime
TEMPLATES.env.filters["tz_abbr"] = _tz_abbr


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
    cd_morning = _solar_local_hour(d, lat, lon, tz, -6.0, morning=True)   # civil dawn
    cd_evening = _solar_local_hour(d, lat, lon, tz, -6.0, morning=False)  # civil dusk
    if None in (sr, ss, cd_morning, cd_evening):
        return []
    # ~15 min strips bracket sunrise/sunset so the "moment" gets its own band.
    strip = 0.25
    bands = [
        # night wraps: two rects
        {"name": "night", "start": 0.0, "end": cd_morning},
        {"name": "dawn",     "start": cd_morning,  "end": sr - strip},
        {"name": "sunrise",  "start": sr - strip,  "end": sr + strip},
        {"name": "day",      "start": sr + strip,  "end": ss - strip},
        {"name": "sunset",   "start": ss - strip,  "end": ss + strip},
        {"name": "dusk",     "start": ss + strip,  "end": cd_evening},
        {"name": "night", "start": cd_evening, "end": 24.0},
    ]
    # Drop zero/negative-width bands (e.g. if dawn strip overlaps sunrise).
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


def _get_reanalyze_detector():
    if _reanalyze_state["detector"] is None:
        with _reanalyze_lock:
            if _reanalyze_state["detector"] is None:
                from africam.detector.birdnet import BirdNetDetector
                _reanalyze_state["detector"] = BirdNetDetector()
    return _reanalyze_state["detector"]


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or AppConfig()
    db = Database(cfg.db_url)
    clips_root = cfg.clips_dir.resolve()

    try:
        static_sources = load_sources(cfg.sources_file)
    except FileNotFoundError:
        static_sources = []
    sites: dict[str, Site] = load_sites(cfg.sites_file)

    app = FastAPI(title="Africam Bird Recognition", version="0.1.0")
    app.state.db = db
    app.state.clips_root = clips_root
    app.state.sites = sites
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    def _runtime_to_cfg(row) -> SourceConfig:
        from africam.config import OcrConfig
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
        """Static (toml) + runtime (DB) sources merged; runtime wins on name clash."""
        merged: dict[str, SourceConfig] = {s.name: s for s in static_sources}
        runtime_names: set[str] = set()
        for row in db.list_runtime_sources():
            merged[row.name] = _runtime_to_cfg(row)
            runtime_names.add(row.name)
        ordered = list(merged.values())
        tiles = _build_tiles(ordered)
        for t in tiles:
            t.is_runtime = t.name in runtime_names  # type: ignore[attr-defined]
        return ordered, merged, tiles

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

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        _, sources_by_name, tiles = _all_sources()
        # Initial render uses default grouping; JS swaps the URL on toggle.
        group_minutes = 5
        with db.session() as s:
            # Pull a wider window than ``limit`` so we have enough raw rows to
            # form ``limit`` groups when grouping is on.
            raw = list(
                s.scalars(
                    select(DetectionRow)
                    .order_by(desc(DetectionRow.started_at))
                    .limit(500)
                )
            )
            rows = _group_detections(raw, group_minutes * 60)[:50]
            row_sources = list(
                s.scalars(
                    select(DetectionRow.source_name)
                    .group_by(DetectionRow.source_name)
                    .order_by(DetectionRow.source_name)
                )
            )
            since = datetime.now(UTC) - timedelta(hours=1)
            top_recent = list(
                s.execute(
                    select(
                        DetectionRow.common_name,
                        DetectionRow.scientific_name,
                        func.count().label("n"),
                        func.max(DetectionRow.confidence).label("max_conf"),
                    )
                    .where(DetectionRow.started_at >= since)
                    .group_by(DetectionRow.common_name, DetectionRow.scientific_name)
                    .order_by(desc("n"))
                    .limit(10)
                )
            )

        sites_for_map = [
            {"name": s.name, "lat": s.lat, "lon": s.lon}
            for s in sorted(sites.values(), key=lambda s: s.name)
        ]
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
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "rows": rows,
                "sources": row_sources,
                "top_recent": top_recent,
                "selected_source": None,
                "tiles": tiles,
                "tiles_json": tiles_for_js,
                "sites": sorted(sites.values(), key=lambda s: s.name),
                "sites_json": sites_for_map,
                "site_states": _site_states(tiles, sources_by_name),
                "source_tz": source_tz,
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
        with db.session() as s:
            stmt = (
                select(DetectionRow)
                .order_by(desc(DetectionRow.started_at))
                .limit(raw_limit)
            )
            if sources_filter:
                stmt = stmt.where(DetectionRow.source_name.in_(sources_filter))
            if last_minutes > 0:
                cutoff = datetime.now(UTC) - timedelta(minutes=last_minutes)
                stmt = stmt.where(DetectionRow.started_at >= cutoff)
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
    ) -> JSONResponse:
        with db.session() as s:
            stmt = (
                select(DetectionRow)
                .order_by(desc(DetectionRow.started_at))
                .limit(limit)
            )
            if source:
                stmt = stmt.where(DetectionRow.source_name == source)
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
        if kind not in ("youtube", "rtsp"):
            raise HTTPException(400, "kind must be youtube or rtsp")
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

    @app.post("/api/sources/{name}/enable")
    def enable_source(request: Request, name: str) -> Response:
        """Restore a soft-deleted runtime source. Supervisor picks it up within
        SUPERVISOR_INTERVAL (15s) and starts a worker."""
        ok = db.enable_runtime_source(name)
        if not ok:
            raise HTTPException(404, f"Runtime source not found: {name}")
        if request.headers.get("hx-request"):
            return admin_partial(request)
        return JSONResponse({"ok": True, "name": name})

    @app.post("/api/sources/{name}/disable")
    def disable_source(request: Request, name: str) -> Response:
        """Soft-delete a runtime source so the supervisor stops its worker.
        Equivalent to DELETE /api/sources/{name} but returns the admin
        partial so the /admin table can re-render in place."""
        ok = db.soft_delete_runtime_source(name)
        if not ok:
            raise HTTPException(404, f"Runtime source not found: {name}")
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
        sort: str = Query(default="last_seen"),  # last_seen | first_seen | count | max_conf | name
    ) -> HTMLResponse:
        _, sources_by_name, _ = _all_sources()
        # One row per (scientific_name, common_name) with aggregates.
        # SQLite's group_concat handles the per-species source list cheaply.
        with db.session() as s:
            rows = list(
                s.execute(
                    select(
                        DetectionRow.scientific_name,
                        DetectionRow.common_name,
                        func.count().label("n"),
                        func.min(DetectionRow.started_at).label("first_seen"),
                        func.max(DetectionRow.started_at).label("last_seen"),
                        func.max(DetectionRow.confidence).label("max_conf"),
                        func.avg(DetectionRow.confidence).label("avg_conf"),
                        func.group_concat(DetectionRow.source_name.distinct()).label("sources"),
                    )
                    .group_by(DetectionRow.scientific_name, DetectionRow.common_name)
                )
            )
        species_list = [
            {
                "scientific_name": r.scientific_name,
                "common_name": r.common_name,
                "n": r.n,
                "first_seen": r.first_seen,
                "last_seen": r.last_seen,
                "max_conf": r.max_conf,
                "avg_conf": r.avg_conf,
                "sources": sorted((r.sources or "").split(",")),
            }
            for r in rows
        ]
        sort_keys = {
            "last_seen":  lambda r: r["last_seen"],
            "first_seen": lambda r: r["first_seen"],
            "count":      lambda r: r["n"],
            "max_conf":   lambda r: r["max_conf"],
            "name":       lambda r: r["common_name"].lower(),
        }
        key = sort_keys.get(sort, sort_keys["last_seen"])
        # name sorts ascending; everything else descending (most recent / most / highest first).
        species_list.sort(key=key, reverse=(sort != "name"))

        # Pre-compute timezone per source so the template's localtime filter works.
        source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
        return TEMPLATES.TemplateResponse(
            request,
            "species.html",
            {
                "species_list": species_list,
                "sort": sort,
                "source_tz": source_tz,
            },
        )

    @app.get("/species/{scientific:path}", response_class=HTMLResponse)
    def species_detail(request: Request, scientific: str) -> HTMLResponse:
        from collections import Counter
        _, sources_by_name, _ = _all_sources()
        with db.session() as s:
            all_rows = list(
                s.scalars(
                    select(DetectionRow)
                    .where(DetectionRow.scientific_name == scientific)
                    .order_by(desc(DetectionRow.started_at))
                )
            )
            note = s.get(SpeciesNoteRow, scientific)
        if not all_rows and note is None:
            raise HTTPException(404, f"No detections or note for {scientific!r}")

        common_name = (
            all_rows[0].common_name
            if all_rows
            else (note.common_name if note else scientific)
        )

        # Per-source breakdown.
        by_source: dict[str, list[DetectionRow]] = {}
        for r in all_rows:
            by_source.setdefault(r.source_name, []).append(r)
        source_summary = []
        for src in sorted(by_source, key=lambda x: -len(by_source[x])):
            srows = by_source[src]
            source_summary.append({
                "source": src,
                "count": len(srows),
                "max_conf": max(r.confidence for r in srows),
                "first_seen": min(r.started_at for r in srows),
                "last_seen": max(r.started_at for r in srows),
            })

        # Daily timeline (last DAYS_BACK days, oldest → newest).
        DAYS_BACK = 14
        today_utc = datetime.now(UTC).date()
        day_counts = [0] * DAYS_BACK
        for r in all_rows:
            ts = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=UTC)
            delta = (today_utc - ts.date()).days
            if 0 <= delta < DAYS_BACK:
                day_counts[DAYS_BACK - 1 - delta] += 1
        # Y-axis labels: dates from oldest to today.
        daily = [
            {
                "date": today_utc - timedelta(days=DAYS_BACK - 1 - i),
                "count": c,
            }
            for i, c in enumerate(day_counts)
        ]
        peak_day = max(day_counts) or 1

        # Hourly activity in each row's source-local tz.
        hours = [0] * 24
        for r in all_rows:
            ts = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=UTC)
            cfg_src = sources_by_name.get(r.source_name)
            tz_name = cfg_src.timezone if cfg_src else "UTC"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = UTC
            hours[ts.astimezone(tz).hour] += 1
        peak_hour = max(hours) or 1

        # Label and suggested-species tallies.
        label_counts_raw = Counter(r.label for r in all_rows)
        label_counts = {
            "good": label_counts_raw.get("good", 0),
            "bad": label_counts_raw.get("bad", 0),
            "unsure": label_counts_raw.get("unsure", 0),
            "unreviewed": label_counts_raw.get(None, 0),
        }
        suggested_counter = Counter(
            r.suggested_species for r in all_rows if r.suggested_species
        )

        # Confidence histogram (10 buckets, 0.0..1.0).
        conf_bins = [0] * 10
        for r in all_rows:
            i = min(9, int(r.confidence * 10))
            conf_bins[i] += 1
        peak_conf_bin = max(conf_bins) or 1

        # Sample clips: top by max conf, plus a spread (low/mid/high), plus labeled good.
        with_clips = [r for r in all_rows if r.clip_path]
        top_clips = sorted(with_clips, key=lambda r: -r.confidence)[:5]
        if len(with_clips) >= 6:
            sorted_by_conf = sorted(with_clips, key=lambda r: r.confidence)
            step = max(1, len(sorted_by_conf) // 6)
            spread_clips = sorted_by_conf[::step][:6]
        else:
            spread_clips = []
        good_clips = [r for r in all_rows if r.label == "good" and r.clip_path][:8]

        return TEMPLATES.TemplateResponse(
            request,
            "species_detail.html",
            {
                "scientific": scientific,
                "common_name": common_name,
                "note": note,
                "total": len(all_rows),
                "max_conf": max((r.confidence for r in all_rows), default=0),
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
                "source_tz": {
                    name: cfg.timezone for name, cfg in sources_by_name.items()
                },
                **_note_tag_context(),
            },
        )

    def _admin_view() -> dict:
        """Build the per-source admin payload: status + knobs + heartbeat info.
        Combines file-defined sources (sources.toml), live runtime sources
        (runtime_sources where deleted_at IS NULL), and soft-deleted runtime
        sources (deleted_at NOT NULL) so the operator can re-enable them."""
        ordered, sources_by_name, _ = _all_sources()
        runtime_rows = db.list_runtime_sources(include_deleted=True)
        runtime_by_name = {r.name: r for r in runtime_rows}
        heartbeats = {h.source_name: h for h in db.list_worker_heartbeats()}

        now = datetime.now(UTC)
        rows: list[dict] = []
        seen: set[str] = set()
        for cfg_src in ordered:
            seen.add(cfg_src.name)
            rt = runtime_by_name.get(cfg_src.name)
            hb = heartbeats.get(cfg_src.name)
            rows.append(_admin_row(cfg_src, rt, hb, deleted=False, now=now))
        # Soft-deleted runtime sources — still listed so they can be re-enabled.
        for rt in runtime_rows:
            if rt.name in seen or rt.deleted_at is None:
                continue
            cfg_src = _runtime_to_cfg(rt)
            hb = heartbeats.get(rt.name)
            rows.append(_admin_row(cfg_src, rt, hb, deleted=True, now=now))

        rows.sort(key=lambda r: r["name"].lower())
        running = sum(1 for r in rows if r["status"] == "running")
        return {"rows": rows, "running": running, "total": len(rows), "now": now}

    def _admin_row(
        cfg_src: SourceConfig,
        runtime_row,
        heartbeat: WorkerHeartbeatRow | None,
        *,
        deleted: bool,
        now: datetime,
    ) -> dict:
        is_runtime = runtime_row is not None
        if deleted:
            status = "disabled"
            since_s = None
            error = None
            state = None
        elif heartbeat is None:
            status = "never"
            since_s = None
            error = None
            state = None
        else:
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
        }

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "admin.html", _admin_view())

    @app.get("/partials/admin", response_class=HTMLResponse)
    def admin_partial(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "_admin_table.html", _admin_view()
        )

    @app.get("/rollup", response_class=HTMLResponse)
    def rollup(
        request: Request,
        hours: int = Query(default=24, ge=1, le=24 * 14),
        top: int = Query(default=8, ge=1, le=50),
    ) -> HTMLResponse:
        _, sources_by_name, _ = _all_sources()
        since = datetime.now(UTC) - timedelta(hours=hours)
        with db.session() as s:
            rows = list(
                s.scalars(
                    select(DetectionRow)
                    .where(DetectionRow.started_at >= since)
                )
            )

        by_source: dict[str, list[DetectionRow]] = {}
        for r in rows:
            by_source.setdefault(r.source_name, []).append(r)

        # Per-source rollup with hourly histogram.
        rollup_rows = []
        per_source_top = []
        for src_name in sorted(by_source):
            src_rows = by_source[src_name]
            species = {r.scientific_name for r in src_rows}
            mean_conf = sum(r.confidence for r in src_rows) / len(src_rows)
            buckets = [0] * hours
            for r in src_rows:
                idx = int((r.started_at.replace(tzinfo=UTC) - since).total_seconds() // 3600)
                if 0 <= idx < hours:
                    buckets[idx] += 1
            peak = max(buckets) or 1
            rollup_rows.append(
                {
                    "source": src_name,
                    "count": len(src_rows),
                    "species": len(species),
                    "mean_conf": mean_conf,
                    "buckets": buckets,
                    "peak": peak,
                }
            )

            # Top species for this source, with a 24-hour-of-day histogram
            # computed in the source's local timezone (so dawn-chorus peaks
            # land on the morning hours regardless of where the camera is).
            cfg_for_src = sources_by_name.get(src_name)
            tz_name = cfg_for_src.timezone if cfg_for_src else "UTC"
            try:
                src_tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                src_tz = UTC
            stats: dict[str, dict] = {}
            for r in src_rows:
                d = stats.setdefault(
                    r.scientific_name,
                    {
                        "common": r.common_name,
                        "scientific": r.scientific_name,
                        "count": 0,
                        "max_conf": 0.0,
                        "best_id": r.id,
                        "best_at": r.started_at,
                        "hours": [0] * 24,
                    },
                )
                d["count"] += 1
                if r.confidence > d["max_conf"]:
                    d["max_conf"] = r.confidence
                    d["best_id"] = r.id
                    d["best_at"] = r.started_at
                ts = r.started_at
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                d["hours"][ts.astimezone(src_tz).hour] += 1
            ordered = sorted(stats.values(), key=lambda d: d["count"], reverse=True)[:top]
            for d in ordered:
                d["peak_hour"] = max(d["hours"]) or 1
            per_source_top.append(
                {
                    "source": src_name,
                    "tz": tz_name,
                    "species": ordered,
                }
            )

        source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
        return TEMPLATES.TemplateResponse(
            request,
            "rollup.html",
            {
                "hours": hours,
                "top": top,
                "since": since,
                "total": len(rows),
                "rollup_rows": rollup_rows,
                "per_source_top": per_source_top,
                "source_tz": source_tz,
                "windows": [1, 6, 24, 48, 168],
            },
        )

    @app.get("/audition", response_class=HTMLResponse)
    def audition(
        request: Request,
        source: str | None = Query(default=None),
        species: str | None = Query(default=None, description="case-insensitive substring of common name"),
        min_conf: float = Query(default=0.0, ge=0.0, le=1.0),
        max_conf: float = Query(default=1.0, ge=0.0, le=1.0),
        label_filter: str = Query(default="unreviewed"),  # unreviewed | good | bad | unsure | all
        note_tag: str = Query(default="any"),  # any | reliable | suspect | rare | untagged
        order: str = Query(default="conf_desc"),  # conf_desc | conf_asc | recent
        limit: int = Query(default=50, ge=1, le=500),
    ) -> HTMLResponse:
        _, sources_by_name, _ = _all_sources()

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
        return TEMPLATES.TemplateResponse(
            request,
            "audition.html",
            {
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
            },
        )

    @app.get("/soundscape", response_class=HTMLResponse)
    def soundscape(
        request: Request,
        source: str | None = Query(default=None),
        min_species: int = Query(default=2, ge=2, le=10),
        order: str = Query(default="recent"),  # recent | n_species | max_conf
        limit: int = Query(default=50, ge=1, le=200),
        bucket: str = Query(default="genuine"),  # genuine | confusions
    ) -> HTMLResponse:
        """Browse chunks where the live pipeline detected multiple species at
        once. Every DetectionRow sharing a ``clip_path`` came from the same
        3 s window, so a clip_path with >=2 rows means BirdNET heard several
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
        return TEMPLATES.TemplateResponse(
            request,
            "soundscape.html",
            {
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
            },
        )

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

        # Cache per-source ZoneInfo so we don't re-parse for every row.
        tz_cache: dict[str, ZoneInfo] = {}

        def _tz_for(name: str) -> ZoneInfo:
            tz = tz_cache.get(name)
            if tz is not None:
                return tz
            cfg = sources_by_name.get(name)
            tz_name = cfg.timezone if cfg else "UTC"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = UTC  # type: ignore[assignment]
            tz_cache[name] = tz
            return tz

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

        tz_cache: dict[str, ZoneInfo] = {}

        def _tz_for(name: str) -> ZoneInfo:
            tz = tz_cache.get(name)
            if tz is not None:
                return tz
            cfg = sources_by_name.get(name)
            tz_name = cfg.timezone if cfg else "UTC"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                tz = UTC  # type: ignore[assignment]
            tz_cache[name] = tz
            return tz

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
                data = _open_meteo_hourly(cfg.lat, cfg.lon, weather_days, tz_name)
                if data:
                    weather = _weather_at_hour(data, hour) or None
                if weather is None and data is None:
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
        ok = db.set_detection_label(detection_id, new_label, **kwargs)
        if not ok:
            raise HTTPException(404, "detection not found")
        if request.headers.get("hx-request"):
            with db.session() as s:
                row = s.get(DetectionRow, detection_id)
                note = s.get(SpeciesNoteRow, row.scientific_name) if row else None
            _, sources_by_name, _ = _all_sources()
            source_tz = {name: cfg.timezone for name, cfg in sources_by_name.items()}
            return TEMPLATES.TemplateResponse(
                request,
                "_audition_row.html",
                {
                    "r": row,
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

        from africam.audio.source import AudioChunk

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
        from africam.audio.locator import locate_species_in_clip

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

    # Open-Meteo hourly archive cache: (lat, lon, past_days, tz) → (expiry, json).
    # Their data updates hourly at most; 1h TTL is generous given how stale
    # the diurnal view's lookback window is. Free API, no key required.
    _om_cache: dict[tuple, tuple[float, dict]] = {}
    _OM_TTL_SECONDS = 3600

    def _open_meteo_hourly(
        lat: float, lon: float, past_days: int, tz_name: str
    ) -> dict | None:
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
                "timezone": tz_name,
            }
        )
        url = f"https://api.open-meteo.com/v1/forecast?{params}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "africam-bird/0.1"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                data = json.load(resp)
        except Exception:
            return None
        _om_cache[key] = (now + _OM_TTL_SECONDS, data)
        return data

    def _weather_at_hour(data: dict, hour: int) -> dict:
        """Aggregate hourly weather values for one hour-of-day across the
        Open-Meteo response window. Returns ``{}`` if no samples landed on
        the requested hour."""
        hourly = (data or {}).get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            return {}
        idxs = []
        for i, t in enumerate(times):
            # times look like "2026-05-13T07:00"; the hour lives at offset 11:13.
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
        codes = [
            int(c) for c in _gather("weather_code") if c is not None
        ]
        wmo_modal = None
        if codes:
            counts: dict[int, int] = {}
            for c in codes:
                counts[c] = counts.get(c, 0) + 1
            wmo_modal = max(counts, key=counts.get)
        icon, label = _wmo_summary(wmo_modal)
        return {
            "n_samples": len(idxs),
            "temp_mean": (sum(temps) / len(temps)) if temps else None,
            "wmo_code": wmo_modal,
            "icon": icon,
            "label": label,
        }

    # Time-of-day band fills shared by every chart that paints solar bands.
    # Night = deep navy (almost black); twilight ramps through warm pinks
    # and oranges around sunrise/sunset; day = sky blue. Stars are added on
    # top of the night band in the dial only.
    _DIAL_BAND_FILL = {
        "night":   "#020617",  # slate-950
        "dawn":    "#f43f5e",  # rose-500   — warm pre-sunrise glow
        "sunrise": "#fb923c",  # orange-400 — peak sunrise
        "day":     "#38bdf8",  # sky-400
        "sunset":  "#f97316",  # orange-500 — peak sunset
        "dusk":    "#e11d48",  # rose-600   — warm post-sunset glow
    }
    _DIAL_BAND_OPACITY = {
        "night":   0.85,
        "dawn":    0.50,
        "sunrise": 0.65,
        "day":     0.30,
        "sunset":  0.65,
        "dusk":    0.50,
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
    ) -> str:
        """Render a 24-hour radial bar dial as inline SVG.

        Background = a translucent annulus segmented by the time-of-day
        bands (night, dawn, sunrise, day, sunset, dusk) at their actual
        fractional-hour edges. Foreground = one emerald wedge per hour with
        length ∝ detection count; the ``highlight`` hour gets an amber
        stroke. All geometry is computed here so the template stays
        declarative."""
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

        parts: list[str] = [
            f'<svg viewBox="0 0 {W} {H}" '
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
                if b.get("name") == "night":
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

        parts.extend([
            # Faint outer reference ring at max radius.
            f'<circle cx="{cx}" cy="{cy}" r="{r_out}" '
            f'fill="none" stroke="#27272a" stroke-width="1"/>',
            # Inner hub.
            f'<circle cx="{cx}" cy="{cy}" r="{r_in}" '
            f'fill="#0a0a0a" stroke="#27272a" stroke-width="1"/>',
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
            if hours[h] == 0:
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
            title = f"{h:02d}:00 · {hours[h]} detections"
            parts.append(
                f'<path d="{d}" fill="{fill}" '
                f'fill-opacity="{opacity:.2f}"{stroke_attr}>'
                f"<title>{title}</title></path>"
            )

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

    def _wmo_summary(code: int | None) -> tuple[str, str]:
        """WMO weather code → (icon key, human label). The icon key is one
        of clear/partly/overcast/fog/rain/thunder; the template renders an
        inline SVG per key. Returns ('', '') when the code is unknown."""
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
            url, headers={"User-Agent": "africam-bird/0.1"}
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
        }

    @app.get("/api/species_image")
    def species_image(
        scientific: str = Query(..., min_length=2, max_length=200),
        common: str = Query(default="", max_length=200),
    ) -> JSONResponse:
        key = scientific.strip().lower()
        now = time.monotonic()
        cached = _wp_cache.get(key)
        if cached and cached[0] > now:
            return JSONResponse(cached[1])
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
        # Persist conservation status so row templates can show a badge
        # without hitting Wikipedia per page render.
        if payload.get("conservation_status"):
            db.set_species_status(scientific, payload["conservation_status"])
        # Only cache when we actually got *something* useful — otherwise a
        # transient Wikipedia hiccup poisons the cache for 24h.
        if (
            payload.get("thumbnail")
            or payload.get("range_map")
            or payload.get("conservation_status")
        ):
            _wp_cache[key] = (now + _WP_TTL_SECONDS, payload)
        return JSONResponse(payload)

    # Tokens (split on non-letter chars) that mark an image as a species range
    # map. We tokenize so 'range' doesn't match 'orange' and 'map' doesn't
    # match within unrelated filenames.
    _RANGE_TOKENS = {"distribution", "range", "rangemap", "map", "habitatmap"}
    _RANGE_EXCLUDES = ("status_iucn", "commons-logo", "ooui_icon", "oojs_ui")

    _RANGE_SPLIT = re.compile(r"[^a-z0-9]+")
    # IUCN status icon filenames: "Status_iucn3.1_LC.svg", "Status_iucn_VU.svg",
    # legacy "Status_LC.svg". Code is the last all-caps token before .svg.
    _IUCN_STATUS_RE = re.compile(
        r"Status[_ ](?:iucn[\d.]*[_ ])?([A-Z]{2,3})\.svg",
        re.IGNORECASE,
    )
    # Canonical codes — guard against accidental matches on unrelated filenames.
    _IUCN_CODES = {"LC", "NT", "VU", "EN", "CR", "EW", "EX", "DD", "NE"}

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
            "&format=json&imlimit=80"
        )
        req = urllib.request.Request(
            list_url, headers={"User-Agent": "africam-bird/0.1"}
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
                if code in _IUCN_CODES:
                    return code
        return None

    def _pick_range_map(images: list[str]) -> dict | None:
        candidates: list[str] = []
        for t in images:
            tl = t.lower()
            if any(x in tl for x in _RANGE_EXCLUDES):
                continue
            tokens = {tk for tk in _RANGE_SPLIT.split(tl) if tk}
            if tokens & _RANGE_TOKENS:
                candidates.append(t)
        if not candidates:
            return None
        # Prefer 'distribution' over 'range' over plain 'map'.
        def _rank(t: str) -> int:
            tl = t.lower()
            if "distribution" in tl:
                return 0
            if "range" in tl:
                return 1
            return 2
        candidates.sort(key=_rank)
        fname = candidates[0]
        fname_enc = urllib.parse.quote(fname.replace(" ", "_"))
        info_url = (
            "https://en.wikipedia.org/w/api.php"
            f"?action=query&titles={fname_enc}"
            "&prop=imageinfo&iiprop=url&iiurlwidth=320&format=json"
        )
        req2 = urllib.request.Request(
            info_url, headers={"User-Agent": "africam-bird/0.1"}
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
        req = urllib.request.Request(url, headers={"User-Agent": "africam-bird/0.1"})
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
        is configured via ``AFRICAM_XENO_CANTO_KEY``."""
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
        req = urllib.request.Request(url, headers={"User-Agent": "africam-bird/0.1"})
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

    @app.get("/spectrograms/{detection_id}.png")
    def spectrogram(
        detection_id: int,
        size: str = Query(default="small"),
    ) -> Response:
        """PNG spectrogram of a detection's clip. Generated lazily on first
        request via ffmpeg's showspectrumpic filter and cached next to the
        WAV. ``size=small`` is used inline; ``large`` for the popout view."""
        if size not in SPEC_SIZES:
            raise HTTPException(400, "size must be 'small' or 'large'")
        with db.session() as s:
            row = s.get(DetectionRow, detection_id)
            if row is None or row.clip_path is None:
                raise HTTPException(404, "no clip for this detection")
            wav = Path(row.clip_path).resolve()
            note = s.get(SpeciesNoteRow, row.scientific_name)
            tag = note.tag if note else None
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
        return FileResponse(png, media_type="image/png")

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
            if row is None or row.clip_path is None:
                raise HTTPException(status_code=404, detail="No clip for this detection")
            path = Path(row.clip_path).resolve()
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
                return FileResponse(mp3, media_type="audio/mpeg")

        media_types = {".wav": "audio/wav", ".ogg": "audio/ogg", ".flac": "audio/flac", ".mp3": "audio/mpeg"}
        return FileResponse(path, media_type=media_types.get(path.suffix.lower(), "audio/wav"))

    return app


# Module-level app instance for `uvicorn africam.web.app:app` and reload mode.
app = create_app()
