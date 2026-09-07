"""Clip push: an ingest node uploads the audio behind rows central already
holds, and central files it exactly as it files its own clips."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from birdbrain import ingest as ingest_mod
from birdbrain.config import AppConfig
from birdbrain.ingest import IngestBody, hash_token, ingest_batch, ingest_clips
from birdbrain.storage import Database, DetectionRow
from birdbrain.web.app import create_app
from birdbrain.wire import WireClipManifest

UNIT = "Nkorho Bush Lodge"
AT = "2026-09-07T05:14:03.250000+00:00"
OGG = b"OggS" + bytes(range(256)) * 4


def _central(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'central.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope-sources.toml",
        sites_file=tmp_path / "nope-sites.toml",
        media_cache_enabled=False,
    )
    db = Database(cfg.db_url)
    db.upsert_device(UNIT, hash_token("tok"), lat=-24.7, lon=31.6)
    return create_app(cfg), db, cfg


def _rows(db, *, n=2, at=AT):
    """Ingest ``n`` detections from one 3 s window (they share a clip)."""
    device = db.get_device(UNIT)
    body = IngestBody.model_validate({
        "unit": UNIT, "schema": 1,
        "detections": [{
            "client_id": f"{UNIT}:{100 + i}", "started_at": at, "duration_s": 3.0,
            "scientific_name": f"Species {i}", "common_name": f"sp{i}",
            "confidence": 0.7, "has_clip": True,
        } for i in range(n)],
    })
    ingest_batch(db, device, body)
    return [f"{UNIT}:{100 + i}" for i in range(n)]


def _manifest(client_ids, part="clip0", fmt="ogg"):
    return json.dumps({"unit": UNIT, "schema": 1,
                       "clips": [{"part": part, "client_ids": client_ids, "fmt": fmt}]})


def _post(app, manifest, files, token="tok"):
    return TestClient(app).post(
        "/ingest/clips", data={"manifest": manifest}, files=files,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_clip_is_stored_under_centrals_own_name_and_attached_to_every_row(tmp_path):
    app, db, cfg = _central(tmp_path)
    ids = _rows(db)
    r = _post(app, _manifest(ids), {"clip0": ("whatever.ogg", OGG, "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json() == {"stored": 1, "attached": 2, "skipped": 0, "unknown": []}
    with db.session() as s:
        rows = list(s.scalars(select(DetectionRow)))
    paths = {row.clip_path for row in rows}
    assert len(paths) == 1  # both rows share the one file
    path = paths.pop()
    # Central's layout, not the node's file name: <clips>/<unit>/<day>/<started_at>.ogg
    assert path == str(cfg.clips_dir / UNIT / "2026-09-07" / "20260907T051403_250000Z.ogg")
    assert Path(path).read_bytes() == OGG
    # And the ordinary clip route now serves it, as it serves a local clip.
    got = TestClient(app).get(f"/clips/{rows[0].id}?fmt=original")
    assert got.status_code == 200 and got.content == OGG


def test_resend_is_harmless(tmp_path):
    app, db, _ = _central(tmp_path)
    ids = _rows(db)
    files = {"clip0": ("x.ogg", OGG, "application/octet-stream")}
    _post(app, _manifest(ids), files)
    again = _post(app, _manifest(ids), files).json()
    assert again == {"stored": 0, "attached": 0, "skipped": 2, "unknown": []}


def test_unknown_client_ids_are_reported_not_stored(tmp_path):
    """A row central filtered at ingest (species floor, suppression) has no
    home for its clip. The node needs to hear that so it can move on."""
    app, _, cfg = _central(tmp_path)
    files = {"clip0": ("x.ogg", OGG, "application/octet-stream")}
    r = _post(app, _manifest([f"{UNIT}:999"]), files)
    assert r.status_code == 200
    assert r.json() == {"stored": 0, "attached": 0, "skipped": 0, "unknown": [f"{UNIT}:999"]}
    assert not (cfg.clips_dir / UNIT).exists()


def test_a_token_only_files_clips_for_its_own_unit(tmp_path):
    app, db, _ = _central(tmp_path)
    db.upsert_device("other", hash_token("tok2"), lat=0, lon=0)
    ids = _rows(db)
    files = {"clip0": ("x.ogg", OGG, "application/octet-stream")}
    r = _post(app, _manifest(ids), files, token="tok2")
    assert r.status_code == 400 and "does not match" in r.text


def test_auth_and_shape_errors(tmp_path):
    app, db, _ = _central(tmp_path)
    ids = _rows(db)
    files = {"clip0": ("x.ogg", OGG, "application/octet-stream")}
    assert _post(app, _manifest(ids), files, token="nope").status_code == 403
    assert TestClient(app).post("/ingest/clips", data={"manifest": "{}"}).status_code == 401
    # manifest names a part that was not sent
    assert _post(app, _manifest(ids, part="clip7"), files).status_code == 400
    # manifest is not JSON
    assert _post(app, "not json", files).status_code == 400
    # a wire version central cannot parse is a 409, like detections
    bad = json.dumps({"unit": UNIT, "schema": 99, "clips": []})
    assert _post(app, bad, {}).status_code == 409


def test_oversized_part_is_refused(tmp_path, monkeypatch):
    app, db, _ = _central(tmp_path)
    ids = _rows(db)
    monkeypatch.setattr(ingest_mod, "CLIP_MAX_BYTES", 16)
    r = _post(app, _manifest(ids), {"clip0": ("x.ogg", OGG, "application/octet-stream")})
    assert r.status_code == 400 and "bytes" in r.text


def test_ingest_clips_never_uses_manifest_strings_as_paths(tmp_path):
    """Neither the part name nor the fmt can steer the file anywhere: the
    part is a dict key and fmt is validated to a known extension."""
    with pytest.raises(ValueError):
        WireClipManifest.model_validate(
            {"unit": UNIT, "clips": [{"part": "p", "client_ids": ["a"], "fmt": "../x"}]}
        )
    _, db, cfg = _central(tmp_path)
    ids = _rows(db)
    device = db.get_device(UNIT)
    m = WireClipManifest.model_validate(
        {"unit": UNIT, "clips": [{"part": "../../etc/passwd", "client_ids": ids}]}
    )
    out = ingest_clips(db, device, cfg.clips_dir, m, {"../../etc/passwd": OGG})
    assert out["stored"] == 1
    stored = next(p for p in (cfg.clips_dir / UNIT).rglob("*.ogg"))
    assert stored.name == "20260907T051403_250000Z.ogg"


def test_safe_dir_rejects_separators_and_dot_runs():
    assert ingest_mod._safe_dir("Jack's Camp") == "Jack's Camp"
    assert ingest_mod._safe_dir("a/b") == "a_b"
    with pytest.raises(ValueError):
        ingest_mod._safe_dir("..")


def test_earliest_row_names_a_shared_file(tmp_path):
    """Two species from one window at slightly different started_at: the file
    takes the earlier stamp, like a local clip takes its chunk's."""
    _, db, cfg = _central(tmp_path)
    device = db.get_device(UNIT)
    later = datetime(2026, 9, 7, 5, 14, 6, tzinfo=UTC).isoformat()
    body = IngestBody.model_validate({
        "unit": UNIT, "schema": 1,
        "detections": [
            {"client_id": f"{UNIT}:1", "started_at": later, "duration_s": 3.0,
             "scientific_name": "B", "common_name": "b", "confidence": 0.5},
            {"client_id": f"{UNIT}:2", "started_at": AT, "duration_s": 3.0,
             "scientific_name": "A", "common_name": "a", "confidence": 0.5},
        ],
    })
    ingest_batch(db, device, body)
    m = WireClipManifest.model_validate(
        {"unit": UNIT, "clips": [{"part": "c", "client_ids": [f"{UNIT}:1", f"{UNIT}:2"]}]}
    )
    ingest_clips(db, device, cfg.clips_dir, m, {"c": OGG})
    assert (cfg.clips_dir / UNIT / "2026-09-07" / "20260907T051403_250000Z.ogg").exists()
