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

import asyncio
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from birdbrain.birdnetcloud_sync import start_cloud_agent
from birdbrain.config import AppConfig
from birdbrain.logging import get_logger
from birdbrain.storage import Database, DetectionRow, WorkerHeartbeatRow
from birdbrain.sync_status import STATUS
from birdbrain.tbb_sync import start_sync_agent

log = get_logger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# No spectrograms on a unit. The feed used to render one PNG per detection via
# a per-request ffmpeg fork, cached onto the SD card. That cost CPU and card
# writes on the smallest machine in the fleet to produce an image central can
# regenerate for free from the clip it already syncs — and with 60 lazy <img>
# tags and no concurrency limit it was failing ~1 request in 5 on a Zero 2 W.
# The feed serves the audio itself instead; see _tbb_feed.html.

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

    def _sync_targets() -> list[dict]:
        """Both destinations, cloud first — it is the principal consumer.

        Read live from what the agent threads publish. This used to be a single
        hardcoded "offline" left over from before Phase 2 shipped, so a unit
        that had been syncing for weeks still showed an offline dot to whoever
        walked up to it.
        """
        targets = [
            {"label": "cloud", **STATUS.cloud.as_dict()},
            {"label": "central", **STATUS.central.as_dict()},
        ]
        # Filtered here rather than in the template so the separator logic
        # stays trivial: a unit that only reports to the cloud should not
        # carry a permanent "central: disabled" on its own page.
        return [t for t in targets if t["enabled"] or t["blocked"]]

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
            "sync_targets": _sync_targets(),
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
    def setup(
        request: Request,
        saved: bool = Query(default=False),
        enrolled: str = Query(default=""),
        enroll_error: str = Query(default=""),
    ) -> HTMLResponse:
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
                    "central_url": cfg.tbb_central_url or "https://birdbrain.co.za",
                    # Enrolled == we hold a device token already.
                    "enrolled": bool(cfg.tbb_device_token),
                },
                "saved": saved,
                "enrolled": enrolled,
                "enroll_error": enroll_error,
            },
        )

    @app.post("/setup/enroll")
    def setup_enroll(
        central_url: str = Form(...),
        code: str = Form(...),
        display_name: str = Form(default=""),
        lat: str = Form(default=""),
        lon: str = Form(default=""),
    ) -> RedirectResponse:
        """Redeem a claim code against central, then save the issued unit_id +
        token to .env. Takes effect on the next service restart / power-cycle."""
        def _num(v: str) -> float | None:
            try:
                return float(v) if v.strip() else None
            except ValueError:
                return None

        base = central_url.strip().rstrip("/")
        payload = {
            "code": code.strip(),
            "display_name": display_name.strip() or None,
            "lat": _num(lat),
            "lon": _num(lon),
        }
        try:
            resp = requests.post(base + "/enroll", json=payload, timeout=30)
        except requests.RequestException as e:
            return RedirectResponse(f"/setup?enroll_error={quote(str(e)[:120])}", status_code=303)
        if resp.status_code // 100 != 2:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            return RedirectResponse(
                f"/setup?enroll_error={quote(str(detail)[:120])}", status_code=303
            )

        data = resp.json()
        updates = {
            "BIRDBRAIN_TBB_CENTRAL_URL": base,
            "BIRDBRAIN_TBB_DEVICE_TOKEN": data["token"],
            "BIRDBRAIN_TBB_UNIT_ID": data["unit_id"],
            "BIRDBRAIN_TBB_SYNC_ENABLED": "true",
        }
        if payload["lat"] is not None:
            updates["BIRDBRAIN_TBB_LAT"] = str(payload["lat"])
        if payload["lon"] is not None:
            updates["BIRDBRAIN_TBB_LON"] = str(payload["lon"])
        update_env_file(Path(".env"), updates)
        log.info("tbb.enrolled", unit=data["unit_id"], central=base)
        return RedirectResponse(f"/setup?enrolled={quote(data['unit_id'])}", status_code=303)

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
                "BIRDBRAIN_TBB_UNIT_ID": unit_id.strip(),
                "BIRDBRAIN_TBB_MIC_DEVICE": mic_device.strip(),
                "BIRDBRAIN_TBB_SYNC_ENABLED": "true" if sync_enabled else "false",
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

    @app.get("/live.mp3")
    async def live_audio(request: Request) -> StreamingResponse:
        """Live mic audio as a streaming MP3, for the central dashboard to proxy
        and audition. Reads the *shared* capture device (the dsnoop PCM set as
        ``tbb_mic_device``) so it runs alongside the detector instead of fighting
        it for the mic. A 10-min ffmpeg cap backstops a forgotten client, and a
        client disconnect kills ffmpeg promptly so a 512 MB unit never accrues
        orphaned encoders. LAN-only, like the rest of this app — the unit exposes
        no inbound internet ports."""
        proc = subprocess.Popen(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-t", "600",  # safety cap for a forgotten tab
                "-f", "alsa", "-i", cfg.tbb_mic_device,
                "-vn", "-ac", "1", "-ar", "44100", "-b:a", "96k",
                "-f", "mp3", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        async def _pump():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    chunk = await asyncio.to_thread(proc.stdout.read, 8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                proc.kill()
                proc.wait()

        return StreamingResponse(_pump(), media_type="audio/mpeg")

    @app.get("/healthz")
    def healthz() -> dict:
        listening, _last_hb, state = _listening()
        # `sync` is additive — tbb-update.sh gates rollback on `listening` and
        # `worker_state`, so those keys keep their exact meaning and position.
        # The backlog is here rather than only in the cloud heartbeat because a
        # unit that is falling behind should be able to say so to anyone who
        # asks it directly, including when the thing it is behind is the cloud.
        return {
            "ok": True,
            "unit": cfg.tbb_unit_id,
            "listening": listening,
            "worker_state": state,
            "sync": STATUS.as_dict(),
        }

    # Sync agent runs in the web process (arch §10.1). No-op unless sync is
    # configured, so tests/imports and offline units never touch the network.
    app.state.sync_stop = threading.Event()
    start_sync_agent(db, cfg, app.state.sync_stop)
    # Optional second target: BirdNET-Cloud. Shares the stop event; no-op
    # without a token, so units that don't opt in are untouched.
    start_cloud_agent(db, cfg, app.state.sync_stop)

    return app


# Module-level instance for `uvicorn birdbrain.web.tbb_app:app`.
app = create_tbb_app()
