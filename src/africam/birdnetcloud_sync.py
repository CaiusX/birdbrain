"""TinyBirdBrain → BirdNET-Cloud (PixCams) bridge.

BirdNET-Cloud's own station path installs a *second* complete BirdNET pipeline
(recorder + model + analyzer + uploader) under /opt/birdnet-cloud. A TBB unit
already runs one against the single USB mic, and on a Pi Zero 2 W (415MB usable)
two models and two exclusive ALSA capture clients do not coexist. So instead of
running their agent we speak their ingest API directly: our detections, their
dashboard, one recorder.

Their API (verified live 2026-07-29 against edge agent v1.4.60):

    POST {endpoint}/api/v1/detections               -> 201 {"id": <uuid>, ...}
    POST {endpoint}/api/v1/detections/{id}/media/audio   multipart file=
    POST {endpoint}/api/v1/devices/heartbeat

all authenticated with ``Authorization: Bearer <station token>``; the station is
identified by the token alone.

Two properties of that API shape this module:

* **No batch endpoint** — one HTTP request per detection, so the tick is capped
  (``birdnetcloud_max_per_tick``) rather than draining an unbounded backlog.
* **No idempotency key and no dedupe** — a resend silently duplicates. The
  high-water mark is therefore the *only* thing preventing double-posting, and
  it is advanced past every row we have decided about (sent or skipped), never
  past a row whose POST failed.

Timestamps always carry an explicit UTC offset. Their own agent sends a bare
naive ``datetime.isoformat()``; the server is timezone-aware, so a naive local
timestamp from a UTC+2 station would land two hours off with nothing to warn
you. We store naive UTC, so we attach ``+00:00`` on the way out.

Placement: a background thread in ``tbb-web``, alongside the central sync agent.
"""
from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from datetime import UTC
from pathlib import Path

import requests
from sqlalchemy import func, select

from africam.config import AppConfig
from africam.logging import get_logger
from africam.storage import Database, DetectionRow

log = get_logger(__name__)

# Reported to their dashboard as the station firmware.
FIRMWARE_VERSION = "birdbrain-bridge/1.0"


class CloudState:
    """Persisted high-water mark of the last detection id we have decided about.

    JSON on disk rather than a DB column so the unit's schema stays identical to
    central's (same reasoning as tbb_sync.SyncState).
    """

    def __init__(self, path: Path, last_pushed_id: int = 0) -> None:
        self.path = path
        self.last_pushed_id = last_pushed_id

    @classmethod
    def load(cls, path: Path) -> CloudState | None:
        """Return None when there is no state yet, so the caller can decide how
        to seed it — the difference between 'start from here' and 'start from
        zero' is 18k duplicate detections in someone else's database."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return None
        return cls(path, int(data.get("last_pushed_id", 0)))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"last_pushed_id": self.last_pushed_id}), encoding="utf-8"
        )


def resolve_token(cfg: AppConfig) -> str | None:
    """Inline token wins; otherwise read the file. A file keeps the secret out
    of the unit's .env and out of shell history."""
    if cfg.birdnetcloud_token:
        return cfg.birdnetcloud_token.strip()
    if cfg.birdnetcloud_token_file:
        try:
            tok = Path(cfg.birdnetcloud_token_file).expanduser().read_text(encoding="utf-8")
        except OSError:
            return None
        return tok.strip() or None
    return None


def current_max_id(db: Database) -> int:
    with db.session() as s:
        return int(s.scalar(select(func.coalesce(func.max(DetectionRow.id), 0))) or 0)


def seed_state(db: Database, path: Path) -> CloudState:
    """First run: start from the newest existing detection.

    Live forwarding must not replay history — that is the backfill's job, with
    its own marker and its own rate limiting. Seeding from zero here would push
    every historical row through the live path and duplicate anything the
    backfill later sends.
    """
    state = CloudState(path, current_max_id(db))
    state.save()
    log.info("birdnetcloud.state_seeded", last_pushed_id=state.last_pushed_id)
    return state


def fetch_batch(db: Database, since_id: int, limit: int) -> list[DetectionRow]:
    with db.session() as s:
        return list(
            s.scalars(
                select(DetectionRow)
                .where(DetectionRow.id > since_id)
                .order_by(DetectionRow.id.asc())
                .limit(limit)
            )
        )


def detection_payload(row: DetectionRow, cfg: AppConfig) -> dict:
    """Map a birdbrain row onto their detection shape."""
    started = row.started_at
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    payload = {
        "common_name": row.common_name,
        "scientific_name": row.scientific_name,
        "confidence": float(row.confidence),
        "detected_at": started.isoformat() if started else None,
        "kind": "bird",
    }
    lat = row.latitude if row.latitude is not None else cfg.tbb_lat
    lon = row.longitude if row.longitude is not None else cfg.tbb_lon
    if lat is not None and lon is not None:
        payload["latitude"] = float(lat)
        payload["longitude"] = float(lon)
    return payload


class AuthRejected(Exception):
    """Token refused (401/403). Distinct from a transient failure: retrying will
    not help until a human fixes the token, so the caller stops the tick rather
    than burning through the backlog."""


def make_session(token: str) -> requests.Session:
    """One connection, reused for the unit's whole lifetime.

    Without keep-alive every POST pays a fresh TLS handshake — measured at 1.4s
    of a 1.6s request from a Pi Zero, and roughly 9MB/day of handshake traffic at
    this detection rate. On a field unit that is both bandwidth and battery.

    Deliberately NO urllib3 Retry. Their API has no idempotency key and no
    dedupe, so a transparently-retried POST that actually reached the server
    duplicates the detection. Failures are already handled safely one level up:
    the high-water mark does not advance, and the next tick retries.
    """
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


def post_detection(
    endpoint: str,
    token: str,
    payload: dict,
    *,
    session: requests.Session | None = None,
    timeout: float = 20.0,
) -> str | None:
    """POST one detection. Returns the cloud uuid on 201, None on a transient
    failure. Raises AuthRejected on 401/403."""
    url = endpoint.rstrip("/") + "/api/v1/detections"
    http = session or requests
    try:
        resp = http.post(
            url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=timeout
        )
    except requests.RequestException as e:
        log.warning("birdnetcloud.post_failed", error=str(e)[:200])
        return None
    if resp.status_code in (401, 403):
        raise AuthRejected(f"http {resp.status_code}: {resp.text[:120]}")
    if resp.status_code != 201:
        log.warning(
            "birdnetcloud.rejected", status=resp.status_code, body=resp.text[:200]
        )
        return None
    try:
        return resp.json().get("id")
    except ValueError:
        return None


def upload_clip(
    endpoint: str,
    token: str,
    detection_id: str,
    clip_path: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 60.0,
) -> bool:
    """Attach the clip. Failure is non-fatal — the detection is already in, and
    a missing clip is worth less than a stalled queue. Their endpoint accepts
    ogg despite their own uploader hardcoding audio/wav, so no transcode."""
    if not clip_path:
        return False
    p = Path(clip_path)
    if not p.exists():
        return False
    ctype = "audio/ogg" if p.suffix.lower() == ".ogg" else "audio/wav"
    url = f"{endpoint.rstrip('/')}/api/v1/detections/{detection_id}/media/audio"
    http = session or requests
    try:
        with p.open("rb") as fh:
            resp = http.post(
                url,
                files={"file": (p.name, fh, ctype)},
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
    except (requests.RequestException, OSError) as e:
        log.warning("birdnetcloud.clip_failed", error=str(e)[:200])
        return False
    return resp.status_code == 200


def _default_route() -> tuple[str | None, str | None]:
    """(gateway, interface) from /proc/net/route, or (None, None)."""
    try:
        with open("/proc/net/route", encoding="utf-8") as fh:
            next(fh, None)
            for line in fh:
                f = line.split()
                if len(f) > 2 and f[1] == "00000000":
                    gw = int(f[2], 16).to_bytes(4, "little")
                    return ".".join(str(b) for b in gw), f[0]
    except (OSError, ValueError):
        pass
    return None, None


def _local_ip() -> str | None:
    """Address of the interface that would reach the internet. The connect() is
    on a UDP socket, so nothing is actually sent."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.0)
            s.connect(("1.1.1.1", 53))
            return s.getsockname()[0]
    except OSError:
        return None


def host_info(db: Database | None = None, state: CloudState | None = None) -> dict:
    """Station facts for their dashboard's station card and Network tab.

    Field names must match their edge agent exactly (``firmware_version``, not
    ``version``): the API accepts anything with a 204, so a wrong key is not an
    error, it is an empty panel on the dashboard and a station that looks like
    it never registered.

    Every lookup is best-effort — a heartbeat that reports a little is better
    than one that raises.
    """
    info: dict = {"firmware_version": FIRMWARE_VERSION}

    gw, iface = _default_route()
    info["hostname"] = socket.gethostname() or None
    info["local_ip"] = _local_ip()
    info["gateway"] = gw
    if iface:
        try:
            info["mac_address"] = (
                Path(f"/sys/class/net/{iface}/address").read_text(encoding="utf-8").strip()
            )
        except OSError:
            pass

    try:
        model = Path("/proc/device-tree/model").read_text(encoding="utf-8")
        info["hardware_detected"] = model.replace("\x00", "").strip() or None
    except OSError:
        pass

    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                gb = int(line.split()[1]) / 1024 / 1024
                # Snap up to the nearest board size — a 512MB Pi Zero reports
                # ~0.4GB usable once the GPU takes its share.
                for size in (1, 2, 4, 8, 16, 32):
                    if gb <= size * 1.06:
                        info["ram_gb"] = size
                        break
                else:
                    info["ram_gb"] = round(gb)
                break
    except (OSError, ValueError):
        pass

    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                info["os_name"] = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass

    try:
        info["free_disk_gb"] = round(shutil.disk_usage("/").free / 1024**3, 1)
    except OSError:
        pass

    # Real backlog, so the dashboard can show the bridge falling behind.
    if db is not None and state is not None:
        try:
            info["queue_depth"] = max(0, current_max_id(db) - state.last_pushed_id)
        except Exception:
            info["queue_depth"] = 0
    else:
        info["queue_depth"] = 0

    return {k: v for k, v in info.items() if v is not None}


def post_heartbeat(
    endpoint: str,
    token: str,
    *,
    db: Database | None = None,
    state: CloudState | None = None,
    session: requests.Session | None = None,
    timeout: float = 15.0,
) -> bool:
    """Keep the station reading 'live' in their dashboard between detections.
    A quiet night is not a dead station.

    Their API answers 204 No Content on success.
    """
    url = endpoint.rstrip("/") + "/api/v1/devices/heartbeat"
    http = session or requests
    try:
        resp = http.post(
            url,
            json=host_info(db, state),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        log.warning("birdnetcloud.heartbeat_failed", error=str(e)[:200])
        return False
    if resp.status_code // 100 != 2:
        log.warning(
            "birdnetcloud.heartbeat_rejected",
            status=resp.status_code,
            body=resp.text[:200],
        )
        return False
    return True


def sync_once(
    db: Database,
    cfg: AppConfig,
    state: CloudState,
    token: str,
    session: requests.Session | None = None,
) -> tuple[int, int]:
    """One tick. Returns (sent, skipped).

    Stops at the first transient failure without advancing past the offending
    row, so nothing is lost and nothing is duplicated on the retry.
    """
    sent = skipped = 0
    rows = fetch_batch(db, state.last_pushed_id, cfg.birdnetcloud_max_per_tick)
    for row in rows:
        if (row.confidence or 0.0) < cfg.birdnetcloud_min_confidence:
            # Decided about: advance past it so it is never reconsidered.
            skipped += 1
            state.last_pushed_id = row.id
            state.save()
            continue
        det_id = post_detection(
            cfg.birdnetcloud_endpoint, token, detection_payload(row, cfg), session=session
        )
        if det_id is None:
            break  # transient — leave the mark, retry next tick
        if cfg.birdnetcloud_upload_clips and row.clip_path:
            upload_clip(
                cfg.birdnetcloud_endpoint, token, det_id, row.clip_path, session=session
            )
        sent += 1
        state.last_pushed_id = row.id
        state.save()
    return sent, skipped


def _loop(db: Database, cfg: AppConfig, token: str, stop_event: threading.Event) -> None:
    state = CloudState.load(cfg.birdnetcloud_state_file) or seed_state(
        db, cfg.birdnetcloud_state_file
    )
    session = make_session(token)
    log.info(
        "birdnetcloud.start",
        endpoint=cfg.birdnetcloud_endpoint,
        last_pushed_id=state.last_pushed_id,
        min_confidence=cfg.birdnetcloud_min_confidence,
        upload_clips=cfg.birdnetcloud_upload_clips,
        heartbeat_seconds=cfg.birdnetcloud_heartbeat_seconds,
    )
    auth_warned = False
    last_heartbeat = 0.0
    while True:
        try:
            sent, skipped = sync_once(db, cfg, state, token, session)
            auth_warned = False
            if sent or skipped:
                log.info(
                    "birdnetcloud.flushed",
                    sent=sent,
                    skipped=skipped,
                    last_pushed_id=state.last_pushed_id,
                )
            # Heartbeat on its own clock, not once per poll. A field unit wants
            # to notice new detections promptly but has no reason to spend a
            # request every minute saying nothing changed — that was ~2MB/day
            # plus a TLS handshake each on metered links.
            now = time.monotonic()
            if now - last_heartbeat >= cfg.birdnetcloud_heartbeat_seconds:
                if post_heartbeat(
                    cfg.birdnetcloud_endpoint, token, db=db, state=state, session=session
                ):
                    last_heartbeat = now
        except AuthRejected as e:
            # Log once per outage rather than every tick — a rotated token would
            # otherwise fill the journal until someone notices.
            if not auth_warned:
                log.warning("birdnetcloud.auth_rejected", error=str(e))
                auth_warned = True
        except Exception:
            log.exception("birdnetcloud.tick_failed")
        if stop_event.wait(cfg.birdnetcloud_interval_seconds):
            session.close()
            return


def start_cloud_agent(
    db: Database, cfg: AppConfig, stop_event: threading.Event
) -> threading.Thread | None:
    """Start the bridge iff it is configured. Returns None when disabled, so a
    unit without a token is completely unaffected — this matters because every
    TBB unit tracks origin/tbb and would otherwise inherit the behaviour."""
    token = resolve_token(cfg)
    if not (cfg.birdnetcloud_enabled and token):
        log.info("birdnetcloud.disabled", enabled=cfg.birdnetcloud_enabled)
        return None
    t = threading.Thread(
        target=_loop, args=(db, cfg, token, stop_event), name="birdnetcloud-sync", daemon=True
    )
    t.start()
    return t
