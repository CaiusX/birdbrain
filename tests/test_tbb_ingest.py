from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from birdbrain.config import AppConfig
from birdbrain.ingest import IngestBody, hash_token, ingest_batch
from birdbrain.storage import Database, DetectionRow
from birdbrain.web.app import create_app


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


# --- audio quality riding along with the batch -------------------------------
# A push-fed unit keeps its audio local, so central can never measure the feed.
# The unit ships its own snapshot instead; these pin the trust boundary.

_QUALITY = {
    "score": 74,
    "level_score": 0.62,
    "avail_score": 0.91,
    "structure_score": 0.44,
    "level_dbfs": -31.5,
    "silence_fraction": 0.09,
    "clip_fraction": 0.0012,
    "flatness": 0.33,
    "fraction_good": 0.81,
    "issue_label": "good",
    "band_hz_low": 120,
    "band_hz_high": 11000,
}


def test_ingest_stores_unit_reported_audio_quality(tmp_path):
    app, db = _central(tmp_path)
    db.upsert_device("tbb-a1b2", hash_token("good-token"))
    client = TestClient(app)

    r = client.post("/ingest/detections", json={**_body(), "audio_quality": _QUALITY},
                    headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 200

    snap = db.audio_quality_snapshot("tbb-a1b2")
    assert snap is not None
    assert snap["score"] == 74
    assert snap["issue_label"] == "good"
    assert snap["band_hz_high"] == 11000
    assert snap["level_dbfs"] == -31.5


def test_audio_quality_is_optional(tmp_path):
    """An older unit, or one whose accumulator hasn't warmed up, omits the key
    entirely — the detections must still ingest and no row is invented."""
    app, db = _central(tmp_path)
    db.upsert_device("tbb-a1b2", hash_token("good-token"))
    client = TestClient(app)

    r = client.post("/ingest/detections", json=_body(),
                    headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    assert db.audio_quality_snapshot("tbb-a1b2") is None


def test_audio_quality_is_written_even_for_an_empty_keep_alive(tmp_path):
    """The silent-mic case: no detections is exactly when the measurement that
    explains the silence matters most, so an empty batch still updates it."""
    app, db = _central(tmp_path)
    db.upsert_device("tbb-a1b2", hash_token("good-token"))
    client = TestClient(app)

    silent = {**_QUALITY, "score": 3, "issue_label": "mostly silent"}
    r = client.post(
        "/ingest/detections",
        json={"unit": "tbb-a1b2", "schema": 1, "detections": [], "audio_quality": silent},
        headers={"Authorization": "Bearer good-token"},
    )
    assert r.status_code == 200 and r.json()["accepted"] == 0
    assert db.audio_quality_snapshot("tbb-a1b2")["issue_label"] == "mostly silent"


@pytest.mark.parametrize("bad", [
    {"score": 101},              # composite is 0..100
    {"score": -1},
    {"level_score": 1.4},        # sub-scores are 0..1
    {"silence_fraction": -0.2},
    {"level_dbfs": 9999.0},      # dBFS is bounded, not free-form
    {"issue_label": "x" * 33},   # column is String(32)
])
def test_ingest_rejects_out_of_range_audio_quality(tmp_path, bad):
    """This arrives over the public tunnel, so every bound is enforced before it
    reaches columns that /admin and the site page render."""
    app, db = _central(tmp_path)
    db.upsert_device("tbb-a1b2", hash_token("good-token"))
    client = TestClient(app)

    r = client.post("/ingest/detections", json={**_body(), "audio_quality": {**_QUALITY, **bad}},
                    headers={"Authorization": "Bearer good-token"})
    assert r.status_code == 422
    assert db.audio_quality_snapshot("tbb-a1b2") is None


def test_audio_quality_is_keyed_to_the_token_not_the_body(tmp_path):
    """A token may only ever describe its own unit — same rule as detections."""
    app, db = _central(tmp_path)
    db.upsert_device("tbb-a1b2", hash_token("good-token"))
    db.upsert_device("tbb-victim", hash_token("victim-token"))
    client = TestClient(app)

    # body.unit must match the token's unit, so the mismatch is refused outright.
    r = client.post(
        "/ingest/detections",
        json={**_body(unit="tbb-victim"), "audio_quality": {**_QUALITY, "score": 0}},
        headers={"Authorization": "Bearer good-token"},
    )
    assert r.status_code == 400
    assert db.audio_quality_snapshot("tbb-victim") is None


def test_ingest_tolerates_version_skew_in_both_directions(tmp_path):
    """Units and central update independently, so neither rollout order may
    break sync: a new unit's extra keys must be ignored by an older central
    (not 422 it into a stuck backlog), and a new central must accept an older
    unit's body that has no audio_quality at all."""
    # New unit → older central: unknown keys are ignored, not rejected.
    body = IngestBody(**{**_body(), "some_future_field": {"x": 1}})
    assert body.unit == "tbb-a1b2"
    # Older unit → new central: the field is simply absent.
    assert IngestBody(**_body()).audio_quality is None
