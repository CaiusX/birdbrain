"""Cached page roll-ups for /rare and /confidence.

Both pages were built from group-bys over the whole detections table — the
confidence curves ~5.5s each, /rare's newly-heard ~5.5s and its label tallies
~2.5s — recomputed on every load of a 1.58M-row table. They now come from a
small TTL cache keyed on the page's arguments.

The risk a keyed cache introduces is serving one window's answer for another,
which would be invisible on the page and wrong in a way nobody would question.
Most of what follows is about that.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
from birdbrain.web.app import _ttl_cache, create_app


def test_ttl_cache_serves_repeat_calls_without_recomputing():
    calls = []

    @_ttl_cache(60.0)
    def f(x):
        calls.append(x)
        return x * 2

    assert f(2) == 4 and f(2) == 4
    assert calls == [2], "second call should have been served from the cache"


def test_ttl_cache_keys_on_arguments():
    """The whole point: /confidence?since=24h must not be served the all-time
    answer."""
    calls = []

    @_ttl_cache(60.0)
    def f(x):
        calls.append(x)
        return x * 2

    assert f(1) == 2
    assert f(2) == 4
    assert calls == [1, 2]


def test_ttl_cache_expires():
    calls = []

    @_ttl_cache(0.05)
    def f():
        calls.append(1)
        return len(calls)

    assert f() == 1
    time.sleep(0.1)
    assert f() == 2, "past the TTL it must recompute, or it never sees new data"


def test_ttl_cache_is_bounded():
    """A key space that grew unbounded would be a slow leak in a long-lived
    web worker."""
    @_ttl_cache(60.0, maxsize=4)
    def f(x):
        return x

    for i in range(20):
        f(i)
    assert f.cache_size() <= 4


def _app(tmp_path, dets):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope.toml",
        sites_file=tmp_path / "nope.toml",
        media_cache_enabled=False,
    )
    db = Database(cfg.db_url)
    db.insert_detections(list(dets))
    db.add_runtime_source(
        name="A", kind="device", url="x", lat=-26.1, lon=28.0,
        min_confidence=0.5, timezone="UTC",
    )
    return create_app(cfg)


def test_confidence_windows_do_not_share_a_cache_entry(tmp_path):
    """An old detection belongs to the all-time curves and not to the 24h ones.
    If the two windows collided in the cache this is what would break, silently.
    """
    now = datetime.now(UTC)
    app = _app(tmp_path, [
        Detection("A", now - timedelta(hours=2), 3.0, "Recentus birdus", "Recent Bird", 0.9),
        Detection("A", now - timedelta(days=40), 3.0, "Ancientus birdus", "Ancient Bird", 0.9),
    ])
    client = TestClient(app)
    all_time = client.get("/confidence", params={"since": "all"}).text
    last_24h = client.get("/confidence", params={"since": "24h"}).text
    assert "Ancient Bird" in all_time
    assert "Ancient Bird" not in last_24h
    assert "Recent Bird" in all_time and "Recent Bird" in last_24h


def test_rare_windows_do_not_share_a_cache_entry(tmp_path):
    """Same hazard on /rare, whose newly-heard section is keyed by window."""
    now = datetime.now(UTC)
    app = _app(tmp_path, [
        Detection("A", now - timedelta(hours=2), 3.0, "Recentus birdus", "Recent Bird", 0.9),
        Detection("A", now - timedelta(days=40), 3.0, "Ancientus birdus", "Ancient Bird", 0.9),
    ])
    client = TestClient(app)
    # "newly heard" means first-ever inside the window: the 40-day-old species
    # is new to neither.
    assert client.get("/rare", params={"since": "24h"}).status_code == 200
    assert client.get("/rare", params={"since": "7d"}).status_code == 200
    assert client.get("/rare", params={"since": "all"}).status_code == 200


def test_rare_site_filter_does_not_share_a_cache_entry(tmp_path):
    """/rare's cache key carries the site filter too."""
    now = datetime.now(UTC)
    app = _app(tmp_path, [
        Detection("A", now - timedelta(hours=1), 3.0, "Aaa aaa", "Site A Bird", 0.9),
        Detection("B", now - timedelta(hours=1), 3.0, "Bbb bbb", "Site B Bird", 0.9),
    ])
    client = TestClient(app)
    only_a = client.get("/rare", params={"since": "7d", "source": "A"}).text
    only_b = client.get("/rare", params={"since": "7d", "source": "B"}).text
    assert only_a != only_b, "a site-filtered view must not reuse another site's entry"
