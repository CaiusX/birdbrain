from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from africam.config import AppConfig
from africam.ingest import IngestBody, hash_token, ingest_batch
from africam.storage import Database, DetectionRow
from africam.web.app import create_app


def _central(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'central.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope-sources.toml",
        sites_file=tmp_path / "nope-sites.toml",
        media_cache_enabled=False,  # don't spawn the Wikipedia sweeper in tests
    )
    db = Database(cfg.db_url)
    return create_app(cfg), db


def _body(unit="tbb-a1b2", sci="Pycnonotus tricolor", at="2026-06-22T05:14:03+00:00"):
    return {
        "unit": unit,
        "schema": 1,
        "detections": [{
            "client_id": f"{unit}:1",
            "started_at": at,
            "duration_s": 3.0,
            "scientific_name": sci,
            "common_name": "Dark-capped Bulbul",
            "confidence": 0.83,
            "has_clip": True,
        }],
    }


def test_hash_token_is_stable_and_hex():
    h = hash_token("secret")
    assert h == hash_token("secret")
    assert len(h) == 64 and h != "secret"


def test_device_registry_roundtrip(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    db.upsert_device("tbb-x", hash_token("tok"), owner="alice", lat=-25.7, lon=28.2)
    assert db.device_by_token(hash_token("tok")).unit_id == "tbb-x"
    assert db.device_by_token(hash_token("wrong")) is None
    now = datetime.now(UTC)
    db.device_touch_seen("tbb-x", now)
    assert db.get_device("tbb-x").last_seen_at is not None


def test_ingest_batch_upserts_idempotently(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    db.upsert_device("tbb-a1b2", hash_token("tok"), lat=-25.7, lon=28.2)
    device = db.get_device("tbb-a1b2")
    body = IngestBody.model_validate(_body())

    first = ingest_batch(db, device, body)
    assert first["accepted"] == 1 and first["duplicate"] == 0
    # Re-send the same batch → no double insert.
    second = ingest_batch(db, device, body)
    assert second["accepted"] == 0 and second["duplicate"] == 1
    # Central stored it under the unit's source_name, with the device's coords.
    with db.session() as s:
        rows = list(s.scalars(select(DetectionRow)))
    assert len(rows) == 1
    assert rows[0].source_name == "tbb-a1b2"
    assert rows[0].latitude == -25.7

def test_ingest_batch_rejects_unit_mismatch(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    db.upsert_device("tbb-a1b2", hash_token("tok"))
    device = db.get_device("tbb-a1b2")
    body = IngestBody.model_validate(_body(unit="tbb-SOMEONE-ELSE"))
    with pytest.raises(ValueError, match="does not match"):
        ingest_batch(db, device, body)


def test_ingest_route_auth_and_idempotency(tmp_path):
    app, db = _central(tmp_path)
    db.upsert_device("tbb-a1b2", hash_token("good-token"), lat=-25.7, lon=28.2)
    client = TestClient(app)

    # No token → 401; bad token → 403.
    assert client.post("/ingest/detections", json=_body()).status_code == 401
    bad = client.post("/ingest/detections", json=_body(),
                      headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 403

    hdr = {"Authorization": "Bearer good-token"}
    ok = client.post("/ingest/detections", json=_body(), headers=hdr)
    assert ok.status_code == 200 and ok.json()["accepted"] == 1
    again = client.post("/ingest/detections", json=_body(), headers=hdr)
    assert again.json()["accepted"] == 0 and again.json()["duplicate"] == 1


def test_public_tunnel_allows_ingest_but_still_blocks_other_writes(tmp_path):
    app, db = _central(tmp_path)
    db.upsert_device("tbb-a1b2", hash_token("good-token"))
    client = TestClient(app)
    public = {"CF-Connecting-IP": "203.0.113.9"}  # simulate Cloudflare tunnel

    # Ingest is the one mutating route allowed publicly (token still enforced).
    ok = client.post("/ingest/detections", json=_body(),
                     headers={**public, "Authorization": "Bearer good-token"})
    assert ok.status_code == 200
    # A different mutating path over the public tunnel is still 404 (gate intact).
    blocked = client.post("/admin/anything", headers=public)
    assert blocked.status_code == 404
