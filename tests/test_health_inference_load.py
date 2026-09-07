"""The detector-saturation gauge counts local workers only.

/admin's "Inference" card estimates how close this Pi is to its ceiling: every
worker shares one lock-serialized BirdNET detector, so capacity is
``chunk_seconds / inference_ms``. The figure is only meaningful if the workers
counted are ones this box actually runs.

Push-fed sources are not. A TBB unit — and, since node-sync, every link on a
stream-ingest node — runs its own detector on its own hardware and POSTs
finished detections over HTTP; ``pipeline.py::_desired_sources`` skips them for
exactly that reason. But they hold ``running`` heartbeats on central (the node's
keep-alive refreshes them so they don't read as dead), so a gauge built from
"sources whose heartbeat says running" silently billed this Pi for another
box's inference. With six node links reporting, the card read ~37% while real
local load was ~13%.

They stay on the roster and in the Workers count — their liveness is real and
worth watching. They just don't enter the saturation maths.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from birdbrain.config import AppConfig
from birdbrain.storage import Database
from birdbrain.web.app import create_app


def _app(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
        media_cache_enabled=False,
    )
    return create_app(cfg), Database(cfg.db_url)


def _local(db, name):
    db.add_runtime_source(
        name=name, kind="youtube", url=f"https://example.test/{name}",
        lat=0.0, lon=0.0, min_confidence=0.5, timezone="UTC", external=False,
    )
    db.worker_heartbeat(name)


def _pushed(db, unit_id):
    """A push-fed source with a live heartbeat — what an ingest-node link or a
    TBB unit looks like on central."""
    db.register_tbb_source(unit_id, lat=0.0, lon=0.0, timezone="UTC")
    db.worker_heartbeat(unit_id)


def _inference_card(html: str) -> tuple[int, int]:
    """(percent, workers shown) from the rendered Inference card."""
    pct = int(re.search(r">(\d+)%<", html).group(1))
    shown = int(re.search(r"(\d+) / ~\d+ cams max", html).group(1))
    return pct, shown


def test_push_fed_sources_do_not_inflate_inference(tmp_path):
    app, db = _app(tmp_path)
    for n in ("Kalahari", "Djuma"):
        _local(db, n)
    for n in ("Angama Mara", "Onguma Waterhole", "Nkorho Bush Lodge"):
        _pushed(db, n)

    html = TestClient(app).get("/partials/health").text
    pct, shown = _inference_card(html)

    # 2 local workers x 100ms / 3000ms chunk ≈ 7%. Counting all five running
    # sources would give 17% and "5 / ~30".
    assert shown == 2
    assert pct == 7


def test_workers_card_still_counts_push_fed_sources(tmp_path):
    """The saturation fix must not hide the node's sites from the roster —
    their liveness is the whole point of the keep-alive."""
    app, db = _app(tmp_path)
    _local(db, "Kalahari")
    for n in ("Angama Mara", "Onguma Waterhole"):
        _pushed(db, n)

    html = TestClient(app).get("/partials/health").text
    assert "3/3" in html
    assert "2 push-fed" in html


def test_all_local_roster_is_unchanged(tmp_path):
    """With no push-fed sources the gauge reads exactly as it always did."""
    app, db = _app(tmp_path)
    for n in ("Kalahari", "Djuma", "Tembe"):
        _local(db, n)

    pct, shown = _inference_card(TestClient(app).get("/partials/health").text)
    assert shown == 3
    assert pct == 10
