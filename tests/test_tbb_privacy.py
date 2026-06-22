from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from africam.config import AppConfig
from africam.detector.birdnet import Detection
from africam.ingest import hash_token
from africam.storage import Database
from africam.web.app import create_app

PUBLIC = {"CF-Connecting-IP": "203.0.113.7"}  # simulate a Cloudflare-tunnel visitor


def _central(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'central.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope.toml",
        sites_file=tmp_path / "nope.toml",
        media_cache_enabled=False,
    )
    db = Database(cfg.db_url)
    db.upsert_device("pub-unit", hash_token("p"), public=True)
    db.upsert_device("priv-unit", hash_token("q"), public=False)
    now = datetime.now(UTC)
    db.insert_detections([Detection("pub-unit", now, 3.0, "Publicus birdus", "Public Bird", 0.9)])
    db.insert_detections([Detection("priv-unit", now, 3.0, "Privatus birdus", "Private Bird", 0.9)])
    return create_app(cfg), db


def test_private_unit_source_names(tmp_path):
    _, db = _central(tmp_path)
    assert db.private_unit_source_names() == {"priv-unit"}


def test_public_feed_hides_private_unit_lan_shows_all(tmp_path):
    app, _ = _central(tmp_path)
    client = TestClient(app)

    public = client.get("/partials/detections", headers=PUBLIC).text
    assert "Public Bird" in public
    assert "Private Bird" not in public  # hidden over the tunnel

    lan = client.get("/partials/detections").text  # no CF header = LAN/admin
    assert "Public Bird" in lan
    assert "Private Bird" in lan  # operator sees everything


def test_dashboard_hides_private_unit_identity_publicly(tmp_path):
    app, _ = _central(tmp_path)
    public = TestClient(app).get("/", headers=PUBLIC).text
    assert "Public Bird" in public          # public unit's feed shows
    assert "priv-unit" not in public         # private unit's identity hidden
    assert "Privatus birdus" not in public   # and its detections aren't in the feed/map
    # NB: a species *common name* can still appear in the global species
    # autocomplete (not unit-identifying) — that aggregate is intentionally out
    # of scope here and will be governed by pi's per-account visibility at merge.


def test_private_site_page_is_404_only_over_tunnel(tmp_path):
    app, _ = _central(tmp_path)
    client = TestClient(app)
    # Private unit's page: 404 publicly, visible on the LAN.
    assert client.get("/site/priv-unit", headers=PUBLIC).status_code == 404
    assert client.get("/site/priv-unit").status_code == 200
    # Public unit's page is fine either way.
    assert client.get("/site/pub-unit", headers=PUBLIC).status_code == 200
