"""Central-side enrollment: redeem a one-time claim code for a device token.

A unit POSTs its printed-on-the-box claim code (plus an owner-chosen name and
optional lat/lon) to ``/enroll``; central validates the code, assigns a
``unit_id``, mints a bearer token, registers the device, and burns the code.
The claim code is the only secret on this path — the FastAPI route rate-limits
and size-caps it; this module is the pure logic.
"""
from __future__ import annotations

import re
import secrets

from pydantic import BaseModel, Field

from birdbrain.ingest import hash_token
from birdbrain.logging import get_logger
from birdbrain.storage import Database

log = get_logger(__name__)


class EnrollBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=64)
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    public: bool = False


def _slug(name: str) -> str:
    """A dashboard-friendly source_name fragment from an owner-typed name."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:40]


def _unique_unit_id(db: Database, display_name: str | None) -> str:
    base = _slug(display_name) if display_name else ""
    if not base:
        base = f"tbb-{secrets.token_hex(3)}"
    candidate = base
    n = 2
    while db.get_device(candidate) is not None:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def enroll(db: Database, body: EnrollBody) -> dict:
    """Redeem a claim code → register a device → return {unit_id, token}.

    Raises ValueError for a missing/already-used code. The returned token is
    shown to the unit exactly once (central stores only its hash)."""
    code = body.code.strip()
    existing = db.get_claim_code(code)
    if existing is None or existing.claimed_at is not None:
        raise ValueError("invalid or already-used claim code")

    unit_id = _unique_unit_id(db, body.display_name)
    # Burn the code first (atomic single-use); if we lose the race, abort.
    if not db.redeem_claim_code(code, unit_id):
        raise ValueError("claim code was just used")

    token = secrets.token_urlsafe(32)
    db.upsert_device(
        unit_id,
        hash_token(token),
        display_name=body.display_name,
        lat=body.lat,
        lon=body.lon,
        sync_enabled=True,
        public=body.public,
    )
    log.info("enroll.success", unit=unit_id, public=body.public)
    return {"unit_id": unit_id, "token": token}
