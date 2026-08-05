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
from datetime import UTC
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from birdbrain.logging import get_logger
from birdbrain.storage import Database, DeviceRow
from birdbrain.wire import (
    SCHEMA_CONFLICT_STATUS,
    SUPPORTED_SCHEMAS,
    UnsupportedSchemaError,
    WireAudioQuality,
    WireBatch,
    WireDetection,
    check_schema,
)

# The wire format is the contract now that a unit builds from its own
# repository; see birdbrain.wire. These aliases keep the central-side names
# that web/app.py and the tests already use.
IngestBody = WireBatch
IngestDetection = WireDetection
IngestAudioQuality = WireAudioQuality

# Re-exported so the FastAPI route can answer a schema conflict without adding
# another import to web/app.py, whose import block already sits after code.
__all__ = [
    "SCHEMA_CONFLICT_STATUS",
    "SUPPORTED_SCHEMAS",
    "IngestAudioQuality",
    "IngestBody",
    "IngestDetection",
    "UnsupportedSchemaError",
    "hash_token",
    "ingest_batch",
]

log = get_logger(__name__)


def hash_token(token: str) -> str:
    """SHA-256 hex of a device token. Central stores only this, never the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
    summary.

    Raises :class:`~birdbrain.wire.UnsupportedSchemaError` if the payload declares a
    wire version this build cannot parse, and ValueError if its unit doesn't
    match the token's (a token may only write its own source_name).

    The schema is checked before anything else and before anything is written.
    An unknown version means a field may have changed meaning, and guessing
    would put wrong rows in the database — worse than the stalled backlog a
    rejection causes, because the stall is visible on the unit's own page and
    the wrong data is not.
    """
    check_schema(body.schema_version)
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
    # The unit measured this on its own audio; central only stores it (stamping
    # its own updated_at, so the freshness the UI greys out on stays our clock).
    # Keyed to the token's unit, never to body.unit — same rule as detections.
    if body.audio_quality is not None:
        db.upsert_audio_quality(device.unit_id, body.audio_quality.model_dump())
    log.info(
        "ingest.batch",
        unit=device.unit_id,
        received=len(body.detections),
        inserted=inserted,
        quality=body.audio_quality.score if body.audio_quality else None,
    )
    return {
        "unit": device.unit_id,
        "received": len(body.detections),
        "accepted": inserted,
        "duplicate": len(body.detections) - inserted,
    }
