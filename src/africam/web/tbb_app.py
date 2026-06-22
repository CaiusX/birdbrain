"""Minimal LAN-only web UI for a TinyBirdBrain (TBB) capture unit.

Deliberately *not* the central dashboard — a unit shows "what's singing now"
and little else (see docs/tbb-architecture.md §5). Two pages plus a setup page,
served on the LAN only, designed to render on a phone:

* ``/``       Now   — live detections feed (auto-refreshing), header with unit
                      name, today's unique-species count, listening state, sync dot.
* ``/today``  Today — today's species with counts (+ link to central if synced).
* ``/setup``  Setup — detected ALSA mic devices, unit name, sync toggle.

Heavy central-only views (maps, media, AI commentary, audition, anomalies,
weather) are intentionally absent — that's what keeps the unit comfortable on
512 MB. Auto-refresh uses a tiny vanilla fetch poll rather than a vendored/CDN
HTMX so the unit needs no internet.
"""
from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from africam.config import AppConfig
from africam.logging import get_logger
from africam.storage import Database, DetectionRow, WorkerHeartbeatRow
from africam.tbb_sync import start_sync_agent

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# Inline spectrogram thumbnail (small only — no popout/large on the unit).
SPEC_SIZE = "240x60"
SPEC_FILTER = "legend=0:scale=log:fscale=log:start=80:stop=12000"
SPEC_PALETTE = "fire"

FEED_LIMIT = 60
# A pipeline worker that has heartbeat within this is "listening". Generous vs.
# the 15 s heartbeat cadence so a slow chunk doesn't flip the indicator.
HEARTBEAT_FRESH_S = 90.0


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tz on read; we always write UTC, so re-attach it."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def list_alsa_devices() -> list[dict[str, str]]:
    """Parse ``arecord -l`` into pickable capture devices. Returns [] when ALSA
    isn't present (e.g. the x86 dev box) so the setup page degrades gracefully."""
    try:
        out = subprocess.run(
            ["arecord", "-l"], capture_output=True, text=True, timeout=5, check=True
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    devices: list[dict[str, str]] = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line.startswith("card "):
            continue
        # e.g. "card 0: Headset [Logitech USB Headset], device 0: USB Audio ..."
        try:
            card = int(line.split("card ", 1)[1].split(":", 1)[0])
            device = int(line.split("device ", 1)[1].split(":", 1)[0])
        except (IndexError, ValueError):
            continue
        devices.append({"device": f"plughw:{card},{device}", "label": line})
    return devices


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set ``KEY=value`` entries in a dotenv file, preserving other lines and
    comments. Existing keys are replaced in place; new keys are appended."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def create_tbb_app(cfg: AppConfig | None = None) -> FastAPI:  # noqa: PLR0915 (route closures)
    cfg = cfg or AppConfig()
    db = Database(cfg.db_url)
    clips_root = cfg.clips_dir.resolve()
    try:
        tz = ZoneInfo(cfg.tbb_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = UTC

    app = FastAPI(title=f"TinyBirdBrain · {cfg.tbb_unit_id}", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    def _today_start_utc() -> datetime:
        """Start of the unit's local 'today', expressed in UTC for querying."""
        local_now = datetime.now(tz)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.astimezone(UTC)

    def _listening() -> tuple[bool, datetime | None, str]:
        """(is the pipeline actively capturing, last heartbeat, raw state)."""
        with db.session() as s:
            row = s.get(WorkerHeartbeatRow, cfg.tbb_unit_id)
        if row is None:
            return (False, None, "no pipeline yet")
        last = _as_utc(row.last_heartbeat_at)
        fresh = (
            row.state == "running"
            and last is not None
            and (datetime.now(UTC) - last).total_seconds() < HEARTBEAT_FRESH_S
        )
        return (fresh, last, row.state)

    def _sync_state() -> str:
        """synced | offline | disabled — sync is off until Phase 2."""
        if not cfg.tbb_sync_enabled:
            return "disabled"
        return "offline"  # Phase 2 will flip this to 'synced' on a recent ack.

    def _header_ctx() -> dict:
        today_start = _today_start_utc()
        with db.session() as s:
            unique_species = s.scalar(
                select(func.count(func.distinct(DetectionRow.scientific_name)))
                .where(DetectionRow.started_at >= today_start)
            ) or 0
            last_at = s.scalar(select(func.max(DetectionRow.started_at)))
        listening, _last_hb, _state = _listening()
        last_det = _as_utc(last_at)
        return {
            "unit_id": cfg.tbb_unit_id,
            "unique_species_today": int(unique_species),
            "listening": listening,
            "last_detection_local": last_det.astimezone(tz) if last_det else None,
            "sync_state": _sync_state(),
            "central_url": cfg.tbb_central_url,
        }

    def _recent_rows(limit: int = FEED_LIMIT) -> list[dict]:
        with db.session() as s:
            rows = list(
                s.scalars(
                    select(DetectionRow)
                    .order_by(DetectionRow.started_at.desc())
                    .limit(limit)
                )
            )
        out = []
        for r in rows:
            at = _as_utc(r.started_at)
            out.append({
                "id": r.id,
                "common_name": r.common_name,
                "scientific_name": r.scientific_name,
                "confidence": r.confidence,
                "local_time": at.astimezone(tz) if at else None,
                "has_clip": r.clip_path is not None,
            })
        return out

    def _species_today() -> list[dict]:
        today_start = _today_start_utc()
        with db.session() as s:
            rows = list(s.execute(
                select(
                    DetectionRow.scientific_name,
                    func.max(DetectionRow.common_name),
                    func.count(DetectionRow.id),
                    func.max(DetectionRow.confidence),
                )
                .where(DetectionRow.started_at >= today_start)
                .group_by(DetectionRow.scientific_name)
                .order_by(func.count(DetectionRow.id).desc())
            ))
        return [
            {
                "scientific_name": sci,
                "common_name": common,
                "count": int(count),
                "max_confidence": float(max_conf or 0),
            }
            for sci, common, count, max_conf in rows
        ]

    @app.get("/", response_class=HTMLResponse)
    def now(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "tbb_now.html",
            {"header": _header_ctx(), "detections": _recent_rows()},
        )

    @app.get("/feed", response_class=HTMLResponse)
    def feed(request: Request) -> HTMLResponse:
        """HTML fragment the Now page polls for auto-refresh."""
        return TEMPLATES.TemplateResponse(
            request,
            "_tbb_feed.html",
            {"header": _header_ctx(), "detections": _recent_rows()},
        )

    @app.get("/today", response_class=HTMLResponse)
    def today(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "tbb_today.html",
            {"header": _header_ctx(), "species": _species_today()},
        )

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, saved: bool = Query(default=False)) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "tbb_setup.html",
            {
                "header": _header_ctx(),
                "devices": list_alsa_devices(),
                "cfg": {
                    "unit_id": cfg.tbb_unit_id,
                    "mic_device": cfg.tbb_mic_device,
                    "lat": cfg.tbb_lat,
                    "lon": cfg.tbb_lon,
                    "retention_days": cfg.tbb_clip_retention_days,
                    "sync_enabled": cfg.tbb_sync_enabled,
                },
                "saved": saved,
            },
        )

    @app.post("/setup")
    def setup_save(
        unit_id: str = Form(...),
        mic_device: str = Form(...),
        sync_enabled: bool = Form(default=False),
    ) -> RedirectResponse:
        """Persist the picked device / unit name / sync toggle to the unit's
        .env. Takes effect on the next `tbb-pipeline` / `tbb-web` restart."""
        update_env_file(
            Path(".env"),
            {
                "AFRICAM_TBB_UNIT_ID": unit_id.strip(),
                "AFRICAM_TBB_MIC_DEVICE": mic_device.strip(),
                "AFRICAM_TBB_SYNC_ENABLED": "true" if sync_enabled else "false",
            },
        )
        log.info("tbb.setup_saved", unit=unit_id, device=mic_device, sync=sync_enabled)
        return RedirectResponse(url="/setup?saved=1", status_code=303)

    @app.get("/setup/mic-sample")
    def mic_sample(
        seconds: int = Query(default=4, ge=1, le=10),
        gain_db: float = Query(default=0.0, ge=-30.0, le=30.0),
        denoise: bool = Query(default=False),
        highpass: bool = Query(default=False),
    ) -> Response:
        """Record a few seconds from the configured mic and return it as OGG for
        the Setup page to play back — a quick "is the mic working / does it sound
        clean?" check, with optional cleanup filters.

        Filters (all ffmpeg, so device-agnostic): ``highpass`` cuts low rumble/
        mains hum, ``denoise`` runs an FFT denoiser for broadband hiss, ``gain_db``
        adjusts level. They shape this *preview* only — see the note on /setup
        about lowering the mic's capture gain for a permanent fix.

        The pipeline holds the ALSA device exclusively, so this works when the
        pipeline is paused / during first-boot setup; while it's capturing, this
        returns 409 with a clear message rather than fighting for the device."""
        filters: list[str] = []
        if highpass:
            filters.append("highpass=f=120")
        if denoise:
            filters.append("afftdn=nr=12")
        if abs(gain_db) > 0.01:
            filters.append(f"volume={gain_db}dB")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "alsa", "-i", cfg.tbb_mic_device,
            "-t", str(seconds), "-ac", "1", "-ar", "48000",
        ]
        if filters:
            cmd += ["-af", ",".join(filters)]
        cmd += ["-c:a", "libvorbis", "-f", "ogg", "pipe:1"]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=seconds + 15, check=False)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise HTTPException(503, f"mic capture failed: {e}") from e
        if proc.returncode != 0 or not proc.stdout:
            err = (proc.stderr or b"").decode("utf-8", "replace").lower()
            if "busy" in err:
                raise HTTPException(
                    409, "Microphone is in use by the pipeline. Stop tbb-pipeline to test it here."
                )
            raise HTTPException(
                503, "Microphone capture failed — check the device selection and `arecord -l`."
            )
        return Response(content=proc.stdout, media_type="audio/ogg")

    @app.get("/spectrograms/{detection_id}.png")
    def spectrogram(detection_id: int) -> Response:
        """Lazy PNG spectrogram thumbnail, cached next to the clip."""
        with db.session() as s:
            row = s.get(DetectionRow, detection_id)
            if row is None or row.clip_path is None:
                raise HTTPException(404, "no clip for this detection")
            clip = Path(row.clip_path).resolve()
        try:
            clip.relative_to(clips_root)
        except ValueError as e:
            raise HTTPException(403, "clip outside allowed root") from e
        if not clip.is_file():
            raise HTTPException(404, "clip file missing")
        png = clip.parent / f"{clip.stem}.{SPEC_PALETTE}.png"
        if not png.exists():
            try:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error", "-i", str(clip),
                        "-lavfi",
                        f"showspectrumpic=s={SPEC_SIZE}:{SPEC_FILTER}:color={SPEC_PALETTE}",
                        str(png),
                    ],
                    check=True, capture_output=True, timeout=15,
                )
            except (
                subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError
            ) as e:
                raise HTTPException(500, f"failed to render spectrogram: {e}") from e
        return FileResponse(png, media_type="image/png")

    @app.get("/clips/{detection_id}")
    def clip(detection_id: int) -> FileResponse:
        """Serve the detection's audio clip as-is (no transcode — keep it light)."""
        with db.session() as s:
            row = s.get(DetectionRow, detection_id)
            if row is None or row.clip_path is None:
                raise HTTPException(404, "no clip for this detection")
            path = Path(row.clip_path).resolve()
        try:
            path.relative_to(clips_root)
        except ValueError as e:
            raise HTTPException(403, "clip outside allowed root") from e
        if not path.is_file():
            raise HTTPException(404, "clip file missing")
        media = {".ogg": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac"}
        return FileResponse(path, media_type=media.get(path.suffix.lower(), "audio/ogg"))

    @app.get("/healthz")
    def healthz() -> dict:
        listening, _last_hb, state = _listening()
        return {"ok": True, "unit": cfg.tbb_unit_id, "listening": listening, "worker_state": state}

    # Sync agent runs in the web process (arch §10.1). No-op unless sync is
    # configured, so tests/imports and offline units never touch the network.
    app.state.sync_stop = threading.Event()
    start_sync_agent(db, cfg, app.state.sync_stop)

    return app


# Module-level instance for `uvicorn africam.web.tbb_app:app`.
app = create_tbb_app()
