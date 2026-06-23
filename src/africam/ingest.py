"""Central-side ingest of TinyBirdBrain detection batches (Phase 2).

The one mutating endpoint reachable over the public Cloudflare tunnel. It is
*not* behind the LAN/admin gate — it authenticates a per-unit bearer token
instead, and a token authorises writes for exactly one ``source_name`` (the
unit's id). Upserts are idempotent on the natural detection key so retries and
overlapping batches never double-insert.

This module holds the pure validation + upsert logic; the FastAPI route in
``web/app.py`` handles transport (auth header, rate limit, body-size cap).
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from africam.logging import get_logger
from africam.storage import Database, DeviceRow

log = get_logger(__name__)


def hash_token(token: str) -> str:
    """SHA-256 hex of a device token. Central stores only this, never the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class IngestDetection(BaseModel):
    client_id: str | None = None
    started_at: datetime
    duration_s: float = 3.0
    scientific_name: str = Field(min_length=1, max_length=256)
    common_name: str = Field(default="", max_length=256)
    confidence: float = Field(ge=0.0, le=1.0)
    has_clip: bool = False


class IngestBody(BaseModel):
    # max_length caps the body so a single POST can't be unbounded.
    model_config = {"populate_by_name": True}

    unit: str = Field(min_length=1, max_length=64)
    schema_version: int = Field(default=1, alias="schema")
    # IANA tz the unit reports (e.g. "Africa/Johannesburg"). Used only to set a
    # new unit's source timezone at first registration; None = leave at UTC.
    timezone: str | None = Field(default=None, max_length=64)
    detections: list[IngestDetection] = Field(default_factory=list, max_length=2000)


def _valid_tz(name: str | None) -> str:
    """Coerce a unit-reported tz to a safe IANA name, falling back to UTC so a
    garbage value can never poison the stored source timezone."""
    if not name:
        return "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return name


def ingest_batch(db: Database, device: DeviceRow, body: IngestBody) -> dict:
    """Upsert a batch for ``device``. Stamps ``last_seen_at``. Returns a small
    summary. Raises ValueError if the payload's unit doesn't match the token's
    unit (a token may only write its own source_name)."""
    if body.unit != device.unit_id:
        raise ValueError(f"unit {body.unit!r} does not match token unit {device.unit_id!r}")

    inserted = 0
    for d in body.detections:
        started = (
            d.started_at.astimezone(UTC)
            if d.started_at.tzinfo
            else d.started_at.replace(tzinfo=UTC)
        )
        if db.upsert_detection(
            source_name=device.unit_id,
            started_at=started,
            duration_s=d.duration_s,
            scientific_name=d.scientific_name,
            common_name=d.common_name or d.scientific_name,
            confidence=d.confidence,
            latitude=device.lat,
            longitude=device.lon,
            client_id=d.client_id,
        ):
            inserted += 1

    # Auto-register the unit as a push-fed site (idempotent) so the map/site/
    # species/brief views pick it up, and stamp liveness — a unit that stops
    # POSTing then shows offline via the same stale-heartbeat logic as a stalled
    # YouTube source.
    db.register_tbb_source(
        device.unit_id, lat=device.lat, lon=device.lon, timezone=_valid_tz(body.timezone)
    )
    db.worker_heartbeat(device.unit_id)
    db.device_touch_seen(device.unit_id)
    log.info(
        "ingest.batch",
        unit=device.unit_id,
        received=len(body.detections),
        inserted=inserted,
    )
    return {
        "unit": device.unit_id,
        "received": len(body.detections),
        "accepted": inserted,
        "duplicate": len(body.detections) - inserted,
    }
