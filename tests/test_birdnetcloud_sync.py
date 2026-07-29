from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from africam import birdnetcloud_sync as bnc
from africam.config import AppConfig
from africam.detector.birdnet import Detection
from africam.storage import Database


def _db_with(tmp_path, confidences):
    db = Database(f"sqlite:///{tmp_path / 'bnc.sqlite'}")
    base = datetime(2026, 7, 29, 8, 0, 0, tzinfo=UTC)
    for i, conf in enumerate(confidences):
        db.insert_detections([
            Detection(
                source_name="tbb-test",
                started_at=base + timedelta(seconds=i),
                duration_s=3.0,
                scientific_name=f"Sci {i}",
                common_name=f"Com {i}",
                confidence=conf,
            )
        ])
    return db


def _cfg(tmp_path, **kw):
    base = dict(
        tbb_unit_id="tbb-test",
        birdnetcloud_enabled=True,
        birdnetcloud_token="tok",
        birdnetcloud_min_confidence=0.7,
        birdnetcloud_upload_clips=False,
        birdnetcloud_state_file=tmp_path / "bnc_state.json",
        birdnetcloud_max_per_tick=50,
    )
    base.update(kw)
    return AppConfig(**base)


class _Recorder:
    """Stands in for post_detection; records payloads, can fail on demand."""

    def __init__(self, fail_from=None):
        self.sent = []
        self.fail_from = fail_from

    def __call__(self, endpoint, token, payload, **kw):
        if self.fail_from is not None and len(self.sent) >= self.fail_from:
            return None
        self.sent.append(payload)
        return f"uuid-{len(self.sent)}"


def test_seeds_from_newest_so_history_is_never_replayed(tmp_path):
    """First run must not blast existing rows at the cloud — their API has no
    dedupe, so a replay is permanent duplication."""
    db = _db_with(tmp_path, [0.9] * 5)
    cfg = _cfg(tmp_path)
    state = bnc.seed_state(db, cfg.birdnetcloud_state_file)
    assert state.last_pushed_id == bnc.current_max_id(db) == 5

    rec = _Recorder()
    sent, skipped = bnc.sync_once(db, cfg, state, "tok")
    assert (sent, skipped) == (0, 0)


def test_forwards_only_new_rows_above_threshold(tmp_path, monkeypatch):
    db = _db_with(tmp_path, [0.9, 0.5, 0.95, 0.69, 0.7])
    cfg = _cfg(tmp_path)
    state = bnc.CloudState(cfg.birdnetcloud_state_file, 0)
    rec = _Recorder()
    monkeypatch.setattr(bnc, "post_detection", rec)

    sent, skipped = bnc.sync_once(db, cfg, state, "tok")
    assert sent == 3 and skipped == 2          # 0.5 and 0.69 filtered out
    assert [p["common_name"] for p in rec.sent] == ["Com 0", "Com 2", "Com 4"]
    assert state.last_pushed_id == 5


def test_no_duplicates_across_ticks(tmp_path, monkeypatch):
    db = _db_with(tmp_path, [0.9] * 3)
    cfg = _cfg(tmp_path)
    state = bnc.CloudState(cfg.birdnetcloud_state_file, 0)
    rec = _Recorder()
    monkeypatch.setattr(bnc, "post_detection", rec)

    bnc.sync_once(db, cfg, state, "tok")
    bnc.sync_once(db, cfg, state, "tok")       # nothing new
    assert len(rec.sent) == 3


def test_transient_failure_does_not_advance_or_duplicate(tmp_path, monkeypatch):
    """The failing row must be retried exactly once more, not skipped and not
    double-sent."""
    db = _db_with(tmp_path, [0.9] * 4)
    cfg = _cfg(tmp_path)
    state = bnc.CloudState(cfg.birdnetcloud_state_file, 0)
    rec = _Recorder(fail_from=2)               # first two succeed, then fail
    monkeypatch.setattr(bnc, "post_detection", rec)

    sent, _ = bnc.sync_once(db, cfg, state, "tok")
    assert sent == 2 and state.last_pushed_id == 2

    rec.fail_from = None                       # network comes back
    sent, _ = bnc.sync_once(db, cfg, state, "tok")
    assert sent == 2 and state.last_pushed_id == 4
    names = [p["common_name"] for p in rec.sent]
    assert names == ["Com 0", "Com 1", "Com 2", "Com 3"]   # each exactly once


def test_auth_rejection_propagates_and_holds_the_mark(tmp_path, monkeypatch):
    db = _db_with(tmp_path, [0.9] * 2)
    cfg = _cfg(tmp_path)
    state = bnc.CloudState(cfg.birdnetcloud_state_file, 0)

    def boom(*a, **kw):
        raise bnc.AuthRejected("http 401")

    monkeypatch.setattr(bnc, "post_detection", boom)
    with pytest.raises(bnc.AuthRejected):
        bnc.sync_once(db, cfg, state, "tok")
    assert state.last_pushed_id == 0           # nothing consumed


def test_payload_carries_explicit_utc_offset(tmp_path):
    """We store naive UTC; a naive timestamp would be read as local time by a
    timezone-aware server and land hours off."""
    db = _db_with(tmp_path, [0.9])
    cfg = _cfg(tmp_path, tbb_lat=-26.124, tbb_lon=28.034)
    row = bnc.fetch_batch(db, 0, 1)[0]
    payload = bnc.detection_payload(row, cfg)

    assert payload["detected_at"].endswith("+00:00")
    parsed = datetime.fromisoformat(payload["detected_at"])
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == datetime(2026, 7, 29, 8, 0, 0, tzinfo=UTC)
    assert payload["latitude"] == -26.124 and payload["longitude"] == 28.034
    assert payload["kind"] == "bird"


def test_agent_is_a_noop_without_a_token(tmp_path):
    """Every TBB unit tracks origin/tbb; one without a token must be untouched."""
    db = _db_with(tmp_path, [0.9])
    cfg = _cfg(tmp_path, birdnetcloud_token=None, birdnetcloud_token_file=None)
    assert bnc.start_cloud_agent(db, cfg, threading.Event()) is None

    cfg_off = _cfg(tmp_path, birdnetcloud_enabled=False)
    assert bnc.start_cloud_agent(db, cfg_off, threading.Event()) is None


def test_token_can_come_from_a_file(tmp_path):
    f = tmp_path / "token"
    f.write_text("  secret-token\n")
    cfg = _cfg(tmp_path, birdnetcloud_token=None, birdnetcloud_token_file=f)
    assert bnc.resolve_token(cfg) == "secret-token"
