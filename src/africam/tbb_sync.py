"""TinyBirdBrain → central sync agent.

On-device inference means we sync *detection rows*, not audio (a row is ~200
bytes; clips stay local in Phase 2 — only a ``has_clip`` flag goes up). The
agent batches new ``DetectionRow``s since a persisted high-water mark
(``last_synced_id``), POSTs them to central's ``/ingest/detections`` with a
per-unit bearer token, and advances the mark only on a successful ack. It never
drops local rows: if central is unreachable the mark doesn't move and the
backlog drains on the next reconnect. Retries are idempotent — central upserts
on ``(source_name, started_at, scientific_name)`` and we send a stable
``client_id`` per row.

Placement: a background thread in ``tbb-web`` (see tbb-architecture.md §10.1).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import requests
from sqlalchemy import select

from africam.config import AppConfig
from africam.logging import get_logger
from africam.storage import Database, DetectionRow

log = get_logger(__name__)


class SyncState:
    """Persisted high-water mark. A JSON file so the unit DB schema == central's."""

    def __init__(self, path: Path, last_synced_id: int = 0) -> None:
        self.path = path
        self.last_synced_id = last_synced_id

    @classmethod
    def load(cls, path: Path) -> SyncState:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(path, int(data.get("last_synced_id", 0)))
        except (FileNotFoundError, ValueError, OSError):
            return cls(path, 0)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"last_synced_id": self.last_synced_id}), encoding="utf-8"
        )


def fetch_batch(db: Database, since_id: int, limit: int) -> list[DetectionRow]:
    """New detections with id > since_id, oldest first (so the mark advances
    monotonically and a capped batch always drains the oldest backlog first)."""
    with db.session() as s:
        return list(
            s.scalars(
                select(DetectionRow)
                .where(DetectionRow.id > since_id)
                .order_by(DetectionRow.id.asc())
                .limit(limit)
            )
        )


def detections_payload(
    unit_id: str, rows: list[DetectionRow], timezone: str | None = None
) -> dict:
    """Build the /ingest/detections body (see tbb-architecture.md §6.2). The
    unit's timezone rides along so central can register a new unit's source in
    its real local tz instead of defaulting to UTC (it's create-only on central,
    so this only sets the tz at first registration)."""
    return {
        "unit": unit_id,
        "schema": 1,
        "timezone": timezone,
        "detections": [
            {
                # Stable across resends → idempotency key alongside the natural key.
                "client_id": f"{unit_id}:{r.id}",
                "started_at": (r.started_at.isoformat() if r.started_at else None),
                "duration_s": r.duration_s,
                "scientific_name": r.scientific_name,
                "common_name": r.common_name,
                "confidence": r.confidence,
                "has_clip": r.clip_path is not None,
            }
            for r in rows
        ],
    }


def post_batch(central_url: str, token: str, payload: dict, *, timeout: float = 30.0) -> bool:
    """POST one batch. Returns True only on a 2xx ack. Network errors and non-2xx
    return False (caller leaves the high-water mark put and retries next tick)."""
    url = central_url.rstrip("/") + "/ingest/detections"
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        log.warning("tbb_sync.post_failed", error=str(e)[:200])
        return False
    if resp.status_code // 100 != 2:
        log.warning("tbb_sync.rejected", status=resp.status_code, body=resp.text[:200])
        return False
    return True


def sync_once(db: Database, cfg: AppConfig, state: SyncState) -> int:
    """Drain the backlog in capped batches until empty or a POST fails. Returns
    the number of detections acked this run."""
    assert cfg.tbb_central_url and cfg.tbb_device_token  # guarded by start_sync_agent
    sent = 0
    while True:
        rows = fetch_batch(db, state.last_synced_id, cfg.tbb_sync_batch_size)
        if not rows:
            break
        payload = detections_payload(cfg.tbb_unit_id, rows, cfg.tbb_timezone)
        if not post_batch(cfg.tbb_central_url, cfg.tbb_device_token, payload):
            break  # offline / rejected — don't advance; retry next tick
        state.last_synced_id = rows[-1].id  # rows are id-ascending
        state.save()
        sent += len(rows)
        if len(rows) < cfg.tbb_sync_batch_size:
            break  # fully drained
    return sent


def _sync_loop(db: Database, cfg: AppConfig, stop_event: threading.Event) -> None:
    state = SyncState.load(cfg.tbb_sync_state_file)
    log.info(
        "tbb_sync.start",
        unit=cfg.tbb_unit_id,
        central=cfg.tbb_central_url,
        last_synced_id=state.last_synced_id,
    )
    while True:
        try:
            n = sync_once(db, cfg, state)
            if n:
                log.info("tbb_sync.flushed", count=n, last_synced_id=state.last_synced_id)
            else:
                # Idle keep-alive: no backlog this tick, so post an empty batch
                # to refresh central's liveness. Central stamps the heartbeat on
                # every ingest (even empty), so a quiet unit reads "running"
                # instead of decaying to "stale" between bird detections.
                post_batch(
                    cfg.tbb_central_url,
                    cfg.tbb_device_token,
                    detections_payload(cfg.tbb_unit_id, [], cfg.tbb_timezone),
                )
        except Exception:
            log.exception("tbb_sync.tick_failed")
        if stop_event.wait(cfg.tbb_sync_interval_seconds):
            return


def start_sync_agent(
    db: Database, cfg: AppConfig, stop_event: threading.Event
) -> threading.Thread | None:
    """Start the background sync thread iff sync is configured. Returns None
    (and does nothing) when disabled / unconfigured — so the unit is fully
    useful offline and tests/imports never reach the network."""
    if not (cfg.tbb_sync_enabled and cfg.tbb_central_url and cfg.tbb_device_token):
        log.info("tbb_sync.disabled", enabled=cfg.tbb_sync_enabled)
        return None
    t = threading.Thread(
        target=_sync_loop, args=(db, cfg, stop_event), name="tbb-sync", daemon=True
    )
    t.start()
    return t
