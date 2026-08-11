"""TinyBirdBrain → central sync agent.

The **optional** reporting target. A unit's principal consumer is BirdNET-Cloud
(``birdnetcloud_sync``); this one is for operators who also run their own
birdbrain, and stays off until someone enrolls the unit from ``/setup``. The two
are independent — either, both, or neither.

It is also the safer of the two by construction, which is why its failure
handling looks relaxed next to the cloud bridge's: central upserts on
``(source_name, started_at, scientific_name)`` and every row carries a stable
``client_id``, so a resend is deduplicated rather than doubled. The cloud API
offers neither, so it has to be paranoid about its high-water mark.

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

import threading
import time
from pathlib import Path

import requests
from sqlalchemy import func, select

from birdbrain.config_core import UnitConfig
from birdbrain.logging import get_logger
from birdbrain.statefile import StateRead, read_json_state, write_json_atomic
from birdbrain.storage import Database, DetectionRow
from birdbrain.sync_status import STATUS, jittered
from birdbrain.tbb_capture import capture_is_live
from birdbrain.wire import SCHEMA_CONFLICT_STATUS, SCHEMA_VERSION

log = get_logger(__name__)


class SyncState:
    """Persisted high-water mark. A JSON file so the unit DB schema == central's."""

    def __init__(self, path: Path, last_synced_id: int = 0) -> None:
        self.path = path
        self.last_synced_id = last_synced_id

    @classmethod
    def load(cls, path: Path) -> SyncState:
        """Falling back to 0 is safe *here*, unlike on the BirdNET-Cloud side.

        Central upserts on the natural key and we send a stable ``client_id``,
        so replaying from zero costs traffic and nothing else. The cloud bridge
        has no such protection, which is why it refuses to guess instead — see
        ``birdnetcloud_sync.resolve_state``.
        """
        data, outcome = read_json_state(path)
        if data is None:
            if outcome is StateRead.CORRUPT:
                log.warning(
                    "tbb_sync.state_unreadable",
                    path=str(path),
                    action="replaying from 0; central dedupes, so this costs "
                           "bandwidth rather than duplicates",
                )
            return cls(path, 0)
        if outcome is StateRead.RECOVERED:
            log.warning("tbb_sync.state_recovered_from_backup", path=str(path))
        try:
            return cls(path, int(data.get("last_synced_id", 0)))
        except (TypeError, ValueError):
            return cls(path, 0)

    def save(self) -> None:
        write_json_atomic(self.path, {"last_synced_id": self.last_synced_id})


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
    unit_id: str,
    rows: list[DetectionRow],
    timezone: str | None = None,
    audio_quality: dict | None = None,
) -> dict:
    """Build the /ingest/detections body (see tbb-architecture.md §6.2). The
    unit's timezone rides along so central can register a new unit's source in
    its real local tz instead of defaulting to UTC (it's create-only on central,
    so this only sets the tz at first registration).

    ``audio_quality`` is the unit's own snapshot. Central can't measure a feed
    whose audio never leaves the unit, so the measurement has to travel with the
    batch; omitted (None) before the accumulator has warmed up."""
    return {
        "unit": unit_id,
        "schema": SCHEMA_VERSION,
        "timezone": timezone,
        "audio_quality": audio_quality,
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


def make_session(token: str) -> requests.Session:
    """One connection, reused for the unit's lifetime.

    Without keep-alive every tick pays a fresh TLS handshake — measured on the
    cloud path at 1.4s of a 1.6s request from a Pi Zero, and roughly 4MB/day
    here at a 45s interval, which is more than the payload it carries.

    Unlike the cloud bridge this side *could* safely take a urllib3 Retry:
    central upserts on the natural key and every row carries a stable
    ``client_id``, so a transparently-retried POST that did reach it is
    deduplicated rather than doubled. Left off anyway — the tick timer is
    already the retry, and a retry inside the request would just make a POST
    block for longer on a unit whose link is flapping.
    """
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


def post_batch(
    central_url: str,
    token: str,
    payload: dict,
    *,
    timeout: float = 30.0,
    session: requests.Session | None = None,
) -> bool:
    """POST one batch. Returns True only on a 2xx ack. Network errors and non-2xx
    return False (caller leaves the high-water mark put and retries next tick)."""
    url = central_url.rstrip("/") + "/ingest/detections"
    http = session or requests
    try:
        resp = http.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        log.warning("tbb_sync.post_failed", error=str(e)[:200])
        STATUS.central.failed(str(e))
        return False
    if resp.status_code == SCHEMA_CONFLICT_STATUS:
        # Central cannot parse this wire version. Unlike a network failure this
        # will not clear on its own — every retry costs a request and produces
        # the identical answer — so say so where a person will see it rather
        # than looping quietly until someone wonders why the backlog is growing.
        log.error(
            "tbb_sync.schema_rejected",
            status=resp.status_code, body=resp.text[:200], sent_schema=SCHEMA_VERSION,
            action="central and this unit disagree about the wire format; "
                   "update whichever is older",
        )
        STATUS.central.block(f"wire schema {SCHEMA_VERSION} rejected by central")
        return False
    if resp.status_code // 100 != 2:
        log.warning("tbb_sync.rejected", status=resp.status_code, body=resp.text[:200])
        STATUS.central.failed(f"http {resp.status_code}")
        return False
    # A previously-blocked unit recovers on its own once central is updated.
    STATUS.central.blocked = None
    return True


def sync_once(
    db: Database,
    cfg: UnitConfig,
    state: SyncState,
    session: requests.Session | None = None,
) -> int:
    """Drain the backlog in capped batches until empty or a POST fails. Returns
    the number of detections acked this run."""
    assert cfg.tbb_central_url and cfg.tbb_device_token  # guarded by start_sync_agent
    sent = 0
    # Read once per tick, not per batch: draining a long backlog is many POSTs
    # of the same instantaneous measurement, and central keeps only the latest.
    quality = db.audio_quality_snapshot(cfg.tbb_unit_id)
    while True:
        rows = fetch_batch(db, state.last_synced_id, cfg.tbb_sync_batch_size)
        if not rows:
            break
        payload = detections_payload(cfg.tbb_unit_id, rows, cfg.tbb_timezone, quality)
        if not post_batch(
            cfg.tbb_central_url, cfg.tbb_device_token, payload, session=session
        ):
            break  # offline / rejected — don't advance; retry next tick
        state.last_synced_id = rows[-1].id  # rows are id-ascending
        state.save()
        sent += len(rows)
        STATUS.central.succeeded(queue_depth=_backlog(db, state))
        if len(rows) < cfg.tbb_sync_batch_size:
            break  # fully drained
    return sent


def _backlog(db: Database, state: SyncState) -> int:
    with db.session() as s:
        newest = s.scalar(select(func.max(DetectionRow.id))) or 0
    return max(0, int(newest) - state.last_synced_id)


def _sync_loop(db: Database, cfg: UnitConfig, stop_event: threading.Event) -> None:
    state = SyncState.load(cfg.tbb_sync_state_file)
    session = make_session(cfg.tbb_device_token or "")
    log.info(
        "tbb_sync.start",
        unit=cfg.tbb_unit_id,
        central=cfg.tbb_central_url,
        last_synced_id=state.last_synced_id,
        keepalive_seconds=cfg.tbb_sync_keepalive_seconds,
    )
    last_keepalive: float | None = None   # None = due now (see birdnetcloud_sync)
    while True:
        try:
            n = sync_once(db, cfg, state, session)
            if n:
                log.info("tbb_sync.flushed", count=n, last_synced_id=state.last_synced_id)
            else:
                # Idle keep-alive: no backlog this tick, so post an empty batch
                # to refresh central's liveness. Central stamps the heartbeat on
                # every ingest (even empty), so a quiet unit reads "running"
                # instead of decaying to "stale" between bird detections.
                # It carries the quality snapshot too — a silent or dead mic
                # produces no detections, which is exactly when central most
                # needs the measurement that explains the silence.
                #
                # On its own clock, though. Sending one every tick cost ~2MB/day
                # on a metered link purely to say nothing had changed; 0 turns
                # it off entirely for a unit that only reports when it has news.
                #
                # Only while the mic is actually being read. Central stamps a
                # heartbeat on every ingest including empty ones, so a unit
                # whose capture loop has wedged would keep posting "I'm here"
                # and read as healthy on the dashboard while hearing nothing —
                # the one failure the dashboard exists to surface, made
                # invisible by the keep-alive that was meant to help. Staying
                # quiet lets central's ordinary stale-heartbeat logic mark the
                # unit offline, using machinery that already exists.
                #
                # Real detections are never gated on this: if there is backlog
                # to flush it goes out regardless, because data that was
                # captured before the wedge is still good.
                now = time.monotonic()
                due = cfg.tbb_sync_keepalive_seconds > 0 and (
                    last_keepalive is None
                    or now - last_keepalive >= cfg.tbb_sync_keepalive_seconds
                )
                if due and not capture_is_live(db, cfg):
                    log.warning(
                        "tbb_sync.keepalive_suppressed",
                        unit=cfg.tbb_unit_id,
                        reason="capture heartbeat is stale — letting central see us go offline",
                    )
                    due = False
                if due and post_batch(
                    cfg.tbb_central_url,
                    cfg.tbb_device_token,
                    detections_payload(
                        cfg.tbb_unit_id, [], cfg.tbb_timezone,
                        db.audio_quality_snapshot(cfg.tbb_unit_id),
                    ),
                    session=session,
                ):
                    last_keepalive = now
                    STATUS.central.succeeded(queue_depth=_backlog(db, state))
        except Exception as e:
            STATUS.central.failed(str(e))
            log.exception("tbb_sync.tick_failed")
        if stop_event.wait(jittered(cfg.tbb_sync_interval_seconds)):
            session.close()
            return


def start_sync_agent(
    db: Database, cfg: UnitConfig, stop_event: threading.Event
) -> threading.Thread | None:
    """Start the background sync thread iff sync is configured. Returns None
    (and does nothing) when disabled / unconfigured — so the unit is fully
    useful offline and tests/imports never reach the network."""
    if not (cfg.tbb_sync_enabled and cfg.tbb_central_url and cfg.tbb_device_token):
        log.info("tbb_sync.disabled", enabled=cfg.tbb_sync_enabled)
        STATUS.central.enabled = False
        return None
    STATUS.central.enabled = True
    t = threading.Thread(
        target=_sync_loop, args=(db, cfg, stop_event), name="tbb-sync", daemon=True
    )
    t.start()
    return t
