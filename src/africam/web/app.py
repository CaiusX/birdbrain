from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

from africam.config import AppConfig, SourceConfig, load_sources
from africam.storage import Database, DetectionRow

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# Captures the 11-char YouTube video id from /watch?v=, youtu.be/ or /embed/ URLs.
_YT_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")


def _youtube_video_id(url: str) -> str | None:
    m = _YT_VIDEO_ID.search(url)
    return m.group(1) if m else None


@dataclass(slots=True)
class LiveTile:
    name: str
    kind: str
    url: str
    video_id: str | None  # YouTube video id, if embeddable

    @property
    def embed_url(self) -> str | None:
        if self.kind == "youtube" and self.video_id:
            return (
                f"https://www.youtube.com/embed/{self.video_id}"
                "?autoplay=1&mute=1&controls=1&rel=0"
            )
        return None


def _build_tiles(sources: list[SourceConfig]) -> list[LiveTile]:
    out: list[LiveTile] = []
    for s in sources:
        vid = _youtube_video_id(s.url) if s.kind == "youtube" else None
        out.append(LiveTile(name=s.name, kind=s.kind, url=s.url, video_id=vid))
    return out


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or AppConfig()
    db = Database(cfg.db_url)
    clips_root = cfg.clips_dir.resolve()

    # Source list is optional — the dashboard still works without it (just no
    # embedded video tiles). Read once at startup; reload by restarting.
    try:
        configured_sources = load_sources(cfg.sources_file)
    except FileNotFoundError:
        configured_sources = []
    tiles = _build_tiles(configured_sources)

    app = FastAPI(title="Africam Bird Recognition", version="0.1.0")
    app.state.db = db
    app.state.clips_root = clips_root
    app.state.tiles = tiles
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

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
            sources = list(
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

        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "rows": rows,
                "sources": sources,
                "top_recent": top_recent,
                "selected_source": None,
                "tiles": tiles,
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
        # Defence in depth: never serve a path outside the configured clips dir.
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
