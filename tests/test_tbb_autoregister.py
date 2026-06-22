from __future__ import annotations

from datetime import UTC, datetime

from africam.ingest import IngestBody, hash_token, ingest_batch
from africam.pipeline import _desired_sources
from africam.storage import Database, RuntimeSourceRow, WorkerHeartbeatRow


def _ingest_one(db, unit="tbb-a1b2"):
    db.upsert_device(unit, hash_token("tok"), lat=-25.7, lon=28.2)
    device = db.get_device(unit)
    body = IngestBody.model_validate({
        "unit": unit,
        "schema": 1,
        "detections": [{
            "started_at": "2026-06-22T05:14:03+00:00",
            "duration_s": 3.0,
            "scientific_name": "Pycnonotus tricolor",
            "common_name": "Dark-capped Bulbul",
            "confidence": 0.83,
        }],
    })
    return ingest_batch(db, device, body)


def test_register_tbb_source_is_create_only(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    assert db.register_tbb_source("tbb-x", lat=-25.7, lon=28.2) is True
    assert db.register_tbb_source("tbb-x", lat=99.9, lon=99.9) is False  # left as-is
    with db.session() as s:
        row = s.get(RuntimeSourceRow, "tbb-x")
    assert row.external is True
    assert row.kind == "mic"
    assert row.lat == -25.7  # not clobbered by the second call


def test_ingest_autoregisters_site_and_stamps_liveness(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    _ingest_one(db)
    with db.session() as s:
        src = s.get(RuntimeSourceRow, "tbb-a1b2")
        hb = s.get(WorkerHeartbeatRow, "tbb-a1b2")
    assert src is not None and src.external is True and src.lat == -25.7
    assert hb is not None and hb.state == "running"


def test_supervisor_skips_external_tbb_sources(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    # A normal runtime source the supervisor SHOULD run...
    db.add_runtime_source(
        name="yt-cam", kind="youtube", url="https://youtu.be/x",
        lat=None, lon=None, min_confidence=0.5, multisite=False,
        cookies_from_browser=None, cookies_file=None, timezone="UTC",
    )
    # ...and a push-fed TBB unit it must NOT run.
    db.register_tbb_source("tbb-a1b2", lat=-25.7, lon=28.2)

    desired = _desired_sources([], db)

    assert "yt-cam" in desired
    assert "tbb-a1b2" not in desired  # external → no local worker spawned


def test_liveness_goes_stale_when_unit_silent(tmp_path):
    # The heartbeat is what the dashboard reads; reuse the same row a stalled
    # YouTube source would produce. (Rendering 'stale' after 60s lives in the
    # web app's _hb_status; here we just confirm the row is written.)
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    _ingest_one(db)
    with db.session() as s:
        hb = s.get(WorkerHeartbeatRow, "tbb-a1b2")
    last = hb.last_heartbeat_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    assert (datetime.now(UTC) - last).total_seconds() < 60
