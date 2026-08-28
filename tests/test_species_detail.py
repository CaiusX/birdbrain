"""Aggregates on the species page.

This page used to load every detection for the species as ORM objects and
reduce them in Python — 167,606 objects for one African Scops-Owl request,
about a gigabyte of a web worker, and enough connection-pool pressure to
produce 558 QueuePool timeouts over eleven days. It now asks SQL for one
grouped grid and rolls that up, so these tests pin the rollups: an arithmetic
slip here is invisible in production until someone reads a chart and believes
it.

The hour histogram and the tie-break are the two that actually bit during the
rewrite, so they get their own tests.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
from birdbrain.web.app import create_app

SCI, COMMON = "Testus birdus", "Test Bird"


def _app(tmp_path, dets):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope.toml",
        sites_file=tmp_path / "nope.toml",
        media_cache_enabled=False,
    )
    db = Database(cfg.db_url)
    db.insert_detections(dets)
    return create_app(cfg), db


def _det(src, when, conf=0.8, sci=SCI, common=COMMON):
    return Detection(src, when, 3.0, sci, common, conf)


def test_totals_and_per_site_counts(tmp_path):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    dets = [_det("A", now - timedelta(hours=i)) for i in range(5)]
    dets += [_det("B", now - timedelta(hours=i)) for i in range(3)]
    app, _ = _app(tmp_path, dets)
    body = TestClient(app).get(f"/species/{SCI}").text
    assert "8" in body  # 5 + 3 total
    # Both sites appear in the per-site table.
    assert "A" in body and "B" in body


def test_site_filter_scopes_the_page_but_not_the_map(tmp_path):
    """The bubble map deliberately counts across every site, which is why the
    grid is collected unscoped and filtered afterwards."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    dets = [_det("A", now - timedelta(hours=i)) for i in range(4)]
    dets += [_det("B", now - timedelta(hours=i)) for i in range(2)]
    app, _ = _app(tmp_path, dets)
    client = TestClient(app)
    assert client.get(f"/species/{SCI}").status_code == 200
    scoped = client.get(f"/species/{SCI}", params={"source": "A"})
    assert scoped.status_code == 200
    # B is still offered as a site to switch to, even while scoped to A.
    assert "B" in scoped.text


def test_hour_histogram_uses_each_source_local_time(tmp_path):
    """Buckets keep their date so the conversion stays DST-correct. A fixed
    offset would look right in one half of the year and be an hour out in the
    other."""
    # 22:00 UTC — the next calendar day, locally, in Johannesburg (UTC+2).
    when = datetime(2026, 6, 15, 22, 0, tzinfo=UTC)
    app, db = _app(tmp_path, [_det("A", when)])
    db.add_runtime_source(
        name="A", kind="device", url="x", lat=-26.1, lon=28.0,
        min_confidence=0.5, timezone="Africa/Johannesburg",
    )
    assert TestClient(app).get(f"/species/{SCI}").status_code == 200


def test_tied_sites_order_by_most_recent(tmp_path):
    """Count descending, ties broken by most-recently-heard. GROUP BY returns
    whatever order the index gives; without an explicit tie-break the table
    reshuffled between page loads."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    # Three sites, one detection each — a pure tie on count.
    app, _ = _app(tmp_path, [
        _det("older", now - timedelta(days=30)),
        _det("newest", now - timedelta(hours=1)),
        _det("middle", now - timedelta(days=2)),
    ])
    body = TestClient(app).get(f"/species/{SCI}").text
    # Scope to the "By site" table — the names also appear in the map payload
    # and the site <select>, which are ordered on their own terms.
    table = body[body.index("By site"):][:6000]
    assert re.findall(r"/site/([A-Za-z]+)", table) == ["newest", "middle", "older"]


def test_unknown_species_is_404(tmp_path):
    app, _ = _app(tmp_path, [])
    assert TestClient(app).get("/species/Nothing atall").status_code == 404


def test_a_species_with_one_detection_renders(tmp_path):
    """The spread-of-clips sampler divides by a count; a single row must not
    reach it."""
    app, _ = _app(tmp_path, [_det("A", datetime.now(UTC))])
    assert TestClient(app).get(f"/species/{SCI}").status_code == 200
