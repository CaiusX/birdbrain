from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

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

    def _note_tag_context() -> dict:
        """Shared context for any page that renders detection rows with
        palette-aware spectrograms. Keeps the template-side lookup tidy."""
        with db.session() as s:
            tag_by_sci = {
                n.scientific_name: n.tag for n in s.scalars(select(SpeciesNoteRow))
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
            "palette_for_tag": {
                k: v for k, v in SPEC_PALETTE_FOR_TAG.items() if k is not None
            },
            "default_palette": SPEC_PALETTE_FOR_TAG[None],
            "all_species": all_species,
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        _, sources_by_name, tiles = _all_sources()
        with db.session() as s:
            rows = list(
                s.scalars(
                    select(DetectionRow)
                    .order_by(desc(DetectionRow.started_at))
                    .limit(50)
                )
            )
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
        source: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> HTMLResponse:
        _, sources_by_name, _ = _all_sources()
        with db.session() as s:
            stmt = (
                select(DetectionRow)
                .order_by(desc(DetectionRow.started_at))
                .limit(limit)
            )
            if source:
                stmt = stmt.where(DetectionRow.source_name == source)
            rows = list(s.scalars(stmt))
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

            # Top species for this source.
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
                    },
                )
                d["count"] += 1
                if r.confidence > d["max_conf"]:
                    d["max_conf"] = r.confidence
                    d["best_id"] = r.id
                    d["best_at"] = r.started_at
            ordered = sorted(stats.values(), key=lambda d: d["count"], reverse=True)[:top]
            per_source_top.append(
                {
                    "source": src_name,
                    "tz": sources_by_name.get(src_name).timezone if sources_by_name.get(src_name) else "UTC",
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
            note_tag_by_sci = {
                n.scientific_name: n.tag for n in s.scalars(select(SpeciesNoteRow))
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
                    "palette_for_tag": {
                        k: v for k, v in SPEC_PALETTE_FOR_TAG.items() if k is not None
                    },
                    "default_palette": SPEC_PALETTE_FOR_TAG[None],
                },
            )
        return JSONResponse({"ok": True, "id": detection_id, "label": new_label})

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
        # Augment with a Wikipedia range/distribution map when we can find one.
        # Resolved against the same page title we landed on (or the input
        # scientific/common name), since the article's image list is the most
        # reliable source for the species' specific range map.
        for title in (payload.get("wp_title"), scientific, common):
            map_payload = _wp_range_map(title) if title else None
            if map_payload:
                payload = {**payload, **map_payload}
                break
        # Only cache when we actually got *something* useful — otherwise a
        # transient Wikipedia hiccup poisons the cache for 24h.
        if payload.get("thumbnail") or payload.get("range_map"):
            _wp_cache[key] = (now + _WP_TTL_SECONDS, payload)
        return JSONResponse(payload)

    # Tokens (split on non-letter chars) that mark an image as a species range
    # map. We tokenize so 'range' doesn't match 'orange' and 'map' doesn't
    # match within unrelated filenames.
    _RANGE_TOKENS = {"distribution", "range", "rangemap", "map", "habitatmap"}
    _RANGE_EXCLUDES = ("status_iucn", "commons-logo", "ooui_icon", "oojs_ui")

    _RANGE_SPLIT = re.compile(r"[^a-z0-9]+")

    def _wp_range_map(title: str) -> dict | None:
        if not title:
            return None
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
            return None
        candidates: list[str] = []
        for page in (data.get("query") or {}).get("pages", {}).values():
            for img in page.get("images") or []:
                t = img.get("title") or ""
                tl = t.lower()
                if any(x in tl for x in _RANGE_EXCLUDES):
                    continue
                # "File:..." prefix + extension contribute noise; just compare
                # tokens against the keyword set.
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
        fname_enc = urllib.parse.quote(candidates[0].replace(" ", "_"))
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
            return JSONResponse({"scientific_name": scientific_name, "note": "", "tag": None})
        return JSONResponse(
            {
                "scientific_name": row.scientific_name,
                "common_name": row.common_name,
                "note": row.note,
                "tag": row.tag,
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
        if not note:
            db.delete_species_note(scientific_name)
            return JSONResponse({"deleted": True, "scientific_name": scientific_name})
        row = db.set_species_note(
            scientific_name, common_name=common_name, note=note, tag=tag
        )
        return JSONResponse(
            {
                "scientific_name": row.scientific_name,
                "common_name": row.common_name,
                "note": row.note,
                "tag": row.tag,
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
    def clip(detection_id: int) -> FileResponse:
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
        media_types = {".wav": "audio/wav", ".ogg": "audio/ogg", ".flac": "audio/flac"}
        return FileResponse(path, media_type=media_types.get(path.suffix.lower(), "audio/wav"))

    return app


# Module-level app instance for `uvicorn africam.web.app:app` and reload mode.
app = create_app()
