"""The running-but-deaf alert.

JHB - Hyde Park's USB microphone stopped delivering samples on 2026-08-25 and
nobody noticed for three days. Every signal that existed said it was fine: the
worker heartbeat was fresh, ffmpeg was streaming, PipeWire reported the node
"running". The only evidence was an absence — no detections — and nothing
watched for that.

The threshold is per-source and self-calibrating, because a fixed one cannot
work: Twin Pan's worst normal silence is ~34 minutes and Hyde Park's is ~6.6h
of overnight quiet, so any single number either fires at every site each night
or takes days to notice a dead one. Each source is measured against the median
across days of that day's longest silence.

These tests pin the two ways it can be useless: staying quiet when a source is
genuinely deaf, and crying wolf during a normal nightly lull.
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


def _app(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope.toml",
        sites_file=tmp_path / "nope.toml",
        media_cache_enabled=False,
    )
    return cfg, Database(cfg.db_url)


def _source(db, name):
    db.add_runtime_source(
        name=name, kind="device", url="x", lat=-26.1, lon=28.0,
        min_confidence=0.5, timezone="UTC",
    )


def _history(db, name, *, days=14, gap_h=1, silent_h=0.0):
    """Hourly detections for ``days``, with a ``gap_h``-hour hole in the middle
    of each day, the most recent one exactly ``silent_h`` hours ago.

    The hole sits mid-day rather than at the edge so it cannot merge with the
    trailing silence — that mistake made the fixture's real silence 6h longer
    than the test claimed, which is the sort of thing that makes a passing test
    meaningless. Each day's worst gap is therefore ``gap_h + 1``.
    """
    newest = datetime.now(UTC) - timedelta(hours=silent_h)
    hole = range(12, 12 + int(gap_h))
    dets = [
        Detection(name, newest - timedelta(hours=i), 3.0, SCI, COMMON, 0.8)
        for i in range(days * 24)
        if (i % 24) not in hole
    ]
    db.insert_detections(dets)


def _running(db, name):
    db.worker_started(name)
    db.worker_heartbeat(name)


def _deaf(cfg, db):
    """The names the /admin health panel would flag as running-but-silent."""
    body = TestClient(create_app(cfg)).get("/partials/health").text
    if "running but silent" not in body:
        return []
    block = body[body.index("running but silent"):]
    block = block[:block.index("</div>")]
    return re.findall(r'/site/([^"]+)"', block)


def test_a_deaf_source_is_flagged(tmp_path):
    """Worker healthy, no detections for far longer than this source ever
    normally goes quiet — the Hyde Park failure."""
    cfg, db = _app(tmp_path)
    _source(db, "deafsite")
    _history(db, "deafsite", gap_h=2, silent_h=30)   # normal hole 2h, silent 30h
    _running(db, "deafsite")
    assert "deafsite" in _deaf(cfg, db)


def test_a_normal_nightly_lull_is_not_flagged(tmp_path):
    """The false alarm that would make people stop reading the panel. This
    source goes quiet for 6h every night; being 8h quiet is unremarkable for it
    even though it would be alarming for a chatty site."""
    cfg, db = _app(tmp_path)
    _source(db, "quietsite")
    _history(db, "quietsite", gap_h=6, silent_h=8)   # normal hole 6h -> threshold 12h
    _running(db, "quietsite")
    assert _deaf(cfg, db) == []


def test_a_chatty_source_still_gets_the_floor(tmp_path):
    """A source that never pauses would otherwise get a threshold of minutes,
    and fire on any hiccup. The floor keeps it at hours."""
    cfg, db = _app(tmp_path)
    _source(db, "busysite")
    _history(db, "busysite", gap_h=1, silent_h=2)    # threshold floors at 3h
    _running(db, "busysite")
    assert _deaf(cfg, db) == []


def test_a_stopped_source_is_not_reported_as_deaf(tmp_path):
    """A source that admits it is down belongs in the "not running" banner. The
    deaf list is for sources that claim to be fine."""
    cfg, db = _app(tmp_path)
    _source(db, "downsite")
    _history(db, "downsite", gap_h=2, silent_h=30)
    db.worker_started("downsite")
    db.worker_stopped("downsite")
    assert "downsite" not in _deaf(cfg, db)


def test_a_source_with_no_history_is_not_judged(tmp_path):
    """Too new to have habits. Guessing a threshold for it produces exactly the
    false alarm this design is trying to avoid."""
    cfg, db = _app(tmp_path)
    _source(db, "newsite")
    now = datetime.now(UTC)
    db.insert_detections([
        Detection("newsite", now - timedelta(hours=40), 3.0, SCI, COMMON, 0.8)
    ])
    _running(db, "newsite")
    assert _deaf(cfg, db) == []
