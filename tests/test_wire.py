"""The unit → central wire contract.

Once the unit builds from its own repository, this is the only thing the two
sides agree on. These tests pin both halves of that: what must keep working
across a version skew, and what must fail loudly instead of guessing.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from birdbrain import tbb_sync
from birdbrain.config import AppConfig
from birdbrain.ingest import hash_token
from birdbrain.storage import Database, DetectionRow
from birdbrain.sync_status import STATUS
from birdbrain.web.app import create_app
from birdbrain.wire import (
    SCHEMA_CONFLICT_STATUS,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMAS,
    UnsupportedSchemaError,
    WireBatch,
    WireDetection,
    check_schema,
)


def _central(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'central.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope-sources.toml",
        sites_file=tmp_path / "nope-sites.toml",
        media_cache_enabled=False,
    )
    db = Database(cfg.db_url)
    db.upsert_device("tbb-a1b2", hash_token("good-token"))
    return TestClient(create_app(cfg)), db


def _body(**kw):
    base = {
        "unit": "tbb-a1b2",
        "schema": SCHEMA_VERSION,
        "detections": [{
            "client_id": "tbb-a1b2:1",
            "started_at": "2026-08-05T05:14:03+00:00",
            "duration_s": 3.0,
            "scientific_name": "Pycnonotus tricolor",
            "common_name": "Dark-capped Bulbul",
            "confidence": 0.83,
            "has_clip": True,
        }],
    }
    base.update(kw)
    return base


HDR = {"Authorization": "Bearer good-token"}


# --- the version is load-bearing -------------------------------------------

def test_the_current_version_is_accepted(tmp_path):
    client, _ = _central(tmp_path)
    r = client.post("/ingest/detections", json=_body(), headers=HDR)
    assert r.status_code == 200 and r.json()["accepted"] == 1


def test_an_unknown_version_is_refused_before_anything_is_written(tmp_path):
    """It used to be decorative: the unit sent `schema: 1` and central declared
    the field but never read it. An unknown version means a field may have
    changed meaning, and guessing puts wrong rows in the database — worse than
    a stalled backlog, because the stall is visible and the wrong data is not.
    """
    client, db = _central(tmp_path)
    r = client.post("/ingest/detections", json=_body(schema=99), headers=HDR)

    assert r.status_code == SCHEMA_CONFLICT_STATUS
    assert "99" in r.text
    with db.session() as s:
        assert s.query(DetectionRow).count() == 0, "nothing may be written"


def test_a_schema_conflict_is_distinguishable_from_a_bad_payload(tmp_path):
    """409 vs 400 is the whole point: one is permanent until somebody updates a
    side, the other might clear on retry. The unit branches on exactly this."""
    client, _ = _central(tmp_path)

    conflict = client.post("/ingest/detections", json=_body(schema=99), headers=HDR)
    mismatch = client.post(
        "/ingest/detections", json=_body(unit="somebody-else"), headers=HDR
    )
    assert conflict.status_code == SCHEMA_CONFLICT_STATUS
    assert mismatch.status_code == 400
    assert conflict.status_code != mismatch.status_code


def test_check_schema_raises_a_valueerror_subclass():
    """web/app.py catches UnsupportedSchemaError before its ValueError handler;
    if the inheritance changed, the 409 would silently become a 400."""
    assert issubclass(UnsupportedSchemaError, ValueError)
    check_schema(SCHEMA_VERSION)
    with pytest.raises(UnsupportedSchemaError) as exc:
        check_schema(max(SUPPORTED_SCHEMAS) + 1)
    assert exc.value.got == max(SUPPORTED_SCHEMAS) + 1


# --- what must survive a skew ----------------------------------------------

def test_additive_fields_do_not_break_either_side(tmp_path):
    """Adding a field is not a breaking change, and that tolerance is
    load-bearing: units update on a daily timer and can be offline for weeks,
    so the two sides WILL run different builds. If this stopped holding, every
    additive change would need a lockstep fleet update."""
    client, _ = _central(tmp_path)
    payload = _body()
    payload["some_future_field"] = {"x": 1}
    payload["detections"][0]["future_per_row"] = "ignored"

    r = client.post("/ingest/detections", json=payload, headers=HDR)
    assert r.status_code == 200 and r.json()["accepted"] == 1


def test_an_older_unit_omitting_optional_fields_still_ingests(tmp_path):
    """No timezone, no audio_quality, no client_id — a build predating them."""
    client, _ = _central(tmp_path)
    r = client.post("/ingest/detections", json={
        "unit": "tbb-a1b2",
        "schema": 1,
        "detections": [{
            "started_at": "2026-08-05T05:14:03+00:00",
            "scientific_name": "Pycnonotus tricolor",
            "confidence": 0.83,
        }],
    }, headers=HDR)
    assert r.status_code == 200 and r.json()["accepted"] == 1


def test_the_version_defaults_to_current_when_absent():
    """A sender that omits it entirely is assumed to speak this build's version
    — otherwise `schema` missing would read as 0 and be refused."""
    assert WireBatch(unit="u", detections=[]).schema_version == SCHEMA_VERSION


def test_an_empty_batch_is_a_valid_keep_alive():
    assert WireBatch(**{"unit": "u", "schema": SCHEMA_VERSION}).detections == []


# --- the sender's half ------------------------------------------------------

def test_the_unit_sends_the_shared_version_constant():
    """Not a hardcoded 1 — the constant is what makes a bump take effect on
    both sides of the same build."""
    payload = tbb_sync.detections_payload("tbb-test", [])
    assert payload["schema"] == SCHEMA_VERSION


def test_a_schema_rejection_blocks_the_unit_instead_of_looping(monkeypatch):
    """Retrying a 409 forever produces the identical answer and hides the
    problem. The unit stops and surfaces it the same way an unreadable state
    file does — as `blocked`, not as `offline`."""
    class _Resp:
        status_code = SCHEMA_CONFLICT_STATUS
        text = "unsupported schema 99"

    class _Session:
        def post(self, *a, **kw):
            return _Resp()

    STATUS.central.blocked = None
    try:
        ok = tbb_sync.post_batch("http://central.test", "tok", {}, session=_Session())
        assert ok is False
        assert STATUS.central.blocked is not None
        assert STATUS.central.state == "blocked"
    finally:
        STATUS.central.blocked = None
        STATUS.central.enabled = False


def test_a_unit_unblocks_itself_once_central_is_updated(monkeypatch):
    """The operator updates central; the unit should recover on its next tick
    without needing a restart."""
    class _OK:
        status_code = 200
        text = "{}"

    class _Session:
        def post(self, *a, **kw):
            return _OK()

    STATUS.central.block("wire schema 1 rejected by central")
    try:
        assert tbb_sync.post_batch("http://c.test", "tok", {}, session=_Session()) is True
        assert STATUS.central.blocked is None
    finally:
        STATUS.central.blocked = None
        STATUS.central.enabled = False


def test_an_ordinary_failure_does_not_block(monkeypatch):
    """A 500 or a dropped link is transient — it must stay `offline` so the
    unit keeps retrying and drains when the link returns."""
    class _Resp:
        status_code = 500
        text = "boom"

    class _Session:
        def post(self, *a, **kw):
            return _Resp()

    STATUS.central.blocked = None
    try:
        assert tbb_sync.post_batch("http://c.test", "tok", {}, session=_Session()) is False
        assert STATUS.central.blocked is None, "a 500 must not be treated as permanent"
    finally:
        STATUS.central.enabled = False


# --- the shape of the contract itself --------------------------------------

def test_the_wire_detection_is_not_centrals_row():
    """Central's table carries labels, scores, site resolution, audio hashes and
    media URLs. None of that comes from a unit, and the wire format is what
    stops the two schemas being welded together."""

    wire_fields = set(WireDetection.model_fields)
    row_fields = {c.name for c in DetectionRow.__table__.columns}
    assert wire_fields < row_fields | {"has_clip"}, "the wire must stay a subset"
    for central_only in ("label", "sound_rating", "audio_hash", "site", "suggested_species"):
        assert central_only not in wire_fields
