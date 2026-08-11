"""The idle keep-alive must not vouch for a unit that has gone deaf.

Central stamps a heartbeat on every ingest, empty batches included. That is what
makes a quiet unit read "running" between birds — and it is also what would let
a unit whose capture loop has wedged keep reporting itself healthy while hearing
nothing. The dashboard would show green for the one failure it exists to catch.

So the keep-alive is gated on the capture loop actually being alive. Real
backlog is never gated: audio captured before a wedge is still good data.

The wedge itself is not hypothetical — central's supervisor lost JHB - Hyde Park
for 13 hours to the same shape on 2026-08-10 (ffmpeg alive, worker blocked in a
read that never returned, heartbeat frozen).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from birdbrain.config_core import UnitConfig
from birdbrain.storage import Database, WorkerHeartbeatRow
from birdbrain.tbb_capture import capture_is_live, heartbeat_fresh_s
from birdbrain.web import tbb_app


def _cfg(tmp_path) -> UnitConfig:
    return UnitConfig(
        db_url=f"sqlite:///{tmp_path / 'u.sqlite'}",
        clips_dir=tmp_path / "clips",
        tbb_unit_id="tbb-test",
    )


def _beat(db: Database, name: str, *, age_s: float, state: str) -> None:
    db.worker_heartbeat(name)
    with db.session() as s:
        row = s.get(WorkerHeartbeatRow, name)
        row.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=age_s)
        row.state = state
        s.commit()


def test_live_capture_is_recognised(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_url)
    _beat(db, "tbb-test", age_s=1, state="running")
    assert capture_is_live(db, cfg) is True


def test_wedged_capture_is_not_live(tmp_path):
    """The failure this exists for: state still says running because the loop
    never got far enough to say otherwise, but the heartbeat has frozen."""
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_url)
    _beat(db, "tbb-test", age_s=heartbeat_fresh_s(cfg) + 60, state="running")
    assert capture_is_live(db, cfg) is False


def test_cleanly_stopped_capture_is_not_live(tmp_path):
    """A stopped worker leaves a recent heartbeat behind — freshness alone
    would call that alive."""
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_url)
    _beat(db, "tbb-test", age_s=1, state="stopped")
    assert capture_is_live(db, cfg) is False


def test_unit_that_never_ran_is_not_live(tmp_path):
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_url)
    assert capture_is_live(db, cfg) is False


def test_quiet_but_healthy_unit_still_counts_as_live(tmp_path):
    """Guards the regression that would matter most in the other direction: a
    unit hearing no birds at 3am must keep its keep-alive, or every unit goes
    offline nightly."""
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_url)
    # Older than a beat, well inside the freshness window.
    _beat(db, "tbb-test", age_s=cfg.worker_heartbeat_seconds + 1, state="running")
    assert capture_is_live(db, cfg) is True


def test_freshness_window_stays_above_the_beat_interval(tmp_path):
    """tbb-update.sh gates its rollback on the `listening` flag derived from
    this. If the window ever dips under the beat interval, a healthy update
    rolls itself back."""
    cfg = _cfg(tmp_path)
    assert heartbeat_fresh_s(cfg) > cfg.worker_heartbeat_seconds


def test_web_and_sync_agree_on_freshness():
    """One definition, two readers. They used to be separate functions."""
    assert tbb_app.heartbeat_fresh_s is heartbeat_fresh_s
