from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

from africam.config import AppConfig, SourceConfig, load_sources
from africam.site_resolver import state_to_resolved
from africam.sites import Site, load_sites
from africam.storage import Database, DetectionRow

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# Captures the 11-char YouTube video id from /watch?v=, youtu.be/ or /embed/ URLs.
_YT_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")

# Default duration of a manual site override before the resolver clears it.
DEFAULT_OVERRIDE_MINUTES = 60


def _youtube_video_id(url: str) -> str | None:
    m = _YT_VIDEO_ID.search(url)
    return m.group(1) if m else None


@dataclass(slots=True)
class LiveTile:
    name: str
    kind: str
    url: str
    video_id: str | None
    multisite: bool

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
        configured_sources = load_sources(cfg.sources_file)
    except FileNotFoundError:
        configured_sources = []
    sources_by_name: dict[str, SourceConfig] = {s.name: s for s in configured_sources}
    tiles = _build_tiles(configured_sources)
    sites: dict[str, Site] = load_sites(cfg.sites_file)

    app = FastAPI(title="Africam Bird Recognition", version="0.1.0")
    app.state.db = db
    app.state.clips_root = clips_root
    app.state.tiles = tiles
    app.state.sites = sites
    app.state.sources_by_name = sources_by_name
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    def _site_states() -> dict[str, dict]:
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

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
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
            }
            for t in tiles
        ]
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
                "site_states": _site_states(),
            },
        )

    @app.get("/partials/detections", response_class=HTMLResponse)
    def detections_partial(
        request: Request,
        source: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> HTMLResponse:
        with db.session() as s:
            stmt = (
                select(DetectionRow)
                .order_by(desc(DetectionRow.started_at))
                .limit(limit)
            )
            if source:
                stmt = stmt.where(DetectionRow.source_name == source)
            rows = list(s.scalars(stmt))
        return TEMPLATES.TemplateResponse(
            request,
            "_detection_rows.html",
            {"rows": rows, "selected_source": source},
        )

    @app.get("/partials/site/{name}", response_class=HTMLResponse)
    def site_panel(request: Request, name: str) -> HTMLResponse:
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
    def clear_site(name: str) -> JSONResponse:
        if name not in sources_by_name:
            raise HTTPException(404, f"Unknown source: {name}")
        db.clear_manual_override(name)
        return JSONResponse({"ok": True})

    @app.get("/api/sources/{name}/site")
    def get_site(name: str) -> JSONResponse:
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
        return FileResponse(path, media_type="audio/wav")

    return app


# Module-level app instance for `uvicorn africam.web.app:app` and reload mode.
app = create_app()
