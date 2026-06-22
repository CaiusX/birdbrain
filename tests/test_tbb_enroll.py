from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from africam.config import AppConfig
from africam.enroll import EnrollBody, enroll
from africam.ingest import hash_token
from africam.storage import Database
from africam.web.app import create_app


def _central(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'central.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope.toml",
        sites_file=tmp_path / "nope.toml",
        media_cache_enabled=False,
    )
    return create_app(cfg), Database(cfg.db_url)


def test_enroll_redeems_code_and_registers_device(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    db.create_claim_code("BIRD-1")
    out = enroll(db, EnrollBody(code="BIRD-1", display_name="Front Garden", lat=-25.7, lon=28.2))

    assert out["unit_id"] == "front-garden"  # slug of the display name
    assert len(out["token"]) > 20
    # Device registered, token authorises it, code is now burned.
    dev = db.device_by_token(hash_token(out["token"]))
    assert dev is not None and dev.unit_id == "front-garden" and dev.lat == -25.7
    assert db.get_claim_code("BIRD-1").claimed_at is not None


def test_enroll_rejects_used_or_unknown_code(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    db.create_claim_code("BIRD-1")
    enroll(db, EnrollBody(code="BIRD-1", display_name="Garden"))
    # Same code again → rejected (single use).
    with pytest.raises(ValueError, match="already-used"):
        enroll(db, EnrollBody(code="BIRD-1", display_name="Garden2"))
    # Unknown code → rejected.
    with pytest.raises(ValueError, match="invalid"):
        enroll(db, EnrollBody(code="NOPE"))


def test_enroll_unit_ids_are_unique(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    db.create_claim_code("A")
    db.create_claim_code("B")
    one = enroll(db, EnrollBody(code="A", display_name="Garden"))
    two = enroll(db, EnrollBody(code="B", display_name="Garden"))
    assert one["unit_id"] == "garden"
    assert two["unit_id"] == "garden-2"  # collision-suffixed


def test_enroll_route_then_ingest_with_issued_token(tmp_path):
    app, db = _central(tmp_path)
    db.create_claim_code("BIRD-XYZ")
    client = TestClient(app)

    # Bad code → 400.
    assert client.post("/enroll", json={"code": "WRONG"}).status_code == 400

    r = client.post(
        "/enroll",
        json={"code": "BIRD-XYZ", "display_name": "Patio", "lat": -25.7, "lon": 28.2},
    )
    assert r.status_code == 200
    issued = r.json()
    assert issued["unit_id"] == "patio"

    # The issued token works on /ingest immediately (full claim → sync path).
    ing = client.post(
        "/ingest/detections",
        headers={"Authorization": f"Bearer {issued['token']}"},
        json={"unit": "patio", "schema": 1, "detections": [{
            "started_at": "2026-06-22T09:00:00+00:00",
            "duration_s": 3.0,
            "scientific_name": "Pycnonotus tricolor",
            "common_name": "Bulbul",
            "confidence": 0.8,
        }]},
    )
    assert ing.status_code == 200 and ing.json()["accepted"] == 1


def test_revoke_blocks_ingest(tmp_path):
    app, db = _central(tmp_path)
    db.create_claim_code("C1")
    client = TestClient(app)
    token = client.post("/enroll", json={"code": "C1", "display_name": "Yard"}).json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}
    body = {"unit": "yard", "schema": 1, "detections": []}
    assert client.post("/ingest/detections", headers=hdr, json=body).status_code == 200

    db.set_device_sync("yard", False)  # revoke
    assert client.post("/ingest/detections", headers=hdr, json=body).status_code == 403
