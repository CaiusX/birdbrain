"""The front page's fast path and its cached slow path.

`/` recomputed three full scans of `detections` on every load — all-time
distinct species per site, each site's first detection, and the 30-day
species overlap that draws the map's connection web. Together that was ~4.2s
of a ~11s page on 1.58M rows, and it grew with the table.

Those three now come from a short TTL cache. The live 24h figures deliberately
do not, so the tests that matter are: the cached numbers are right, and the
live ones still move.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
from birdbrain.web.app import create_app


def _app(tmp_path, dets=()):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope.toml",
        sites_file=tmp_path / "nope.toml",
        media_cache_enabled=False,
    )
    db = Database(cfg.db_url)
    if dets:
        db.insert_detections(list(dets))
    return create_app(cfg), db


def _det(src, sci, when, conf=0.8):
    return Detection(src, when, 3.0, sci, sci.split()[-1].title(), conf)


def _sited(db, name, lat, lon):
    db.add_runtime_source(
        name=name, kind="device", url="x", lat=lat, lon=lon,
        min_confidence=0.5, timezone="UTC",
    )


def test_front_page_renders_with_no_data(tmp_path):
    app, _ = _app(tmp_path)
    assert TestClient(app).get("/").status_code == 200


def test_all_time_species_per_site_is_correct(tmp_path):
    """The cached figure behind the map's site popup. Three distinct species at
    A, one at B — and a repeat must not inflate the count."""
    now = datetime.now(UTC)
    app, db = _app(tmp_path, [
        _det("A", "Aaa aaa", now - timedelta(hours=1)),
        _det("A", "Bbb bbb", now - timedelta(hours=2)),
        _det("A", "Ccc ccc", now - timedelta(hours=3)),
        _det("A", "Aaa aaa", now - timedelta(hours=4)),   # repeat
        _det("B", "Aaa aaa", now - timedelta(hours=1)),
    ])
    _sited(db, "A", -26.1, 28.0)
    _sited(db, "B", -25.0, 27.0)
    body = TestClient(app).get("/").text
    counts = sorted(int(x) for x in re.findall(r'"species_all": (\d+)', body))
    assert counts == [1, 3], f"expected A=3 distinct, B=1, got {counts}"


def test_live_24h_numbers_are_not_cached(tmp_path):
    """Only the three slow roll-ups are cached. If the 24h counts ever join
    them, the front page would quietly stop being live — which is the whole
    point of the page."""
    now = datetime.now(UTC)
    app, db = _app(tmp_path, [_det("A", "Aaa aaa", now - timedelta(minutes=5))])
    _sited(db, "A", -26.1, 28.0)
    client = TestClient(app)

    first = client.get("/").text
    db.insert_detections([_det("A", "Zzz zzz", datetime.now(UTC))])
    second = client.get("/").text
    assert first != second, "a new detection must show up immediately"


def test_repeated_loads_are_consistent(tmp_path):
    """Serving from the cache must not change what the cached figures say."""
    now = datetime.now(UTC)
    app, db = _app(tmp_path, [
        _det("A", "Aaa aaa", now - timedelta(hours=1)),
        _det("A", "Bbb bbb", now - timedelta(hours=2)),
    ])
    _sited(db, "A", -26.1, 28.0)
    client = TestClient(app)
    a = re.findall(r'"species_all": (\d+)', client.get("/").text)
    b = re.findall(r'"species_all": (\d+)', client.get("/").text)
    assert a == b == ["2"]


def test_shared_species_web_pairs_sites(tmp_path):
    """The 30-day overlap, also cached. Two sites sharing two species get one
    edge carrying that count."""
    now = datetime.now(UTC)
    app, db = _app(tmp_path, [
        _det("A", "Aaa aaa", now - timedelta(days=1)),
        _det("A", "Bbb bbb", now - timedelta(days=1)),
        _det("B", "Aaa aaa", now - timedelta(days=2)),
        _det("B", "Bbb bbb", now - timedelta(days=2)),
        # Outside the 30-day window — must not count toward the overlap.
        _det("A", "Ccc ccc", now - timedelta(days=60)),
        _det("B", "Ccc ccc", now - timedelta(days=60)),
    ])
    _sited(db, "A", -26.1, 28.0)
    _sited(db, "B", -25.0, 27.0)
    body = TestClient(app).get("/").text
    shared = [int(x) for x in re.findall(r'"shared": (\d+)', body)]
    assert shared == [2], f"expected one edge worth 2 shared species, got {shared}"
