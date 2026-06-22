from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from africam import tbb_sync
from africam.config import AppConfig
from africam.detector.birdnet import Detection
from africam.storage import Database
from africam.tbb_sync import (
    SyncState,
    detections_payload,
    fetch_batch,
    start_sync_agent,
    sync_once,
)


def _db_with(tmp_path, n):
    db = Database(f"sqlite:///{tmp_path / 'sync.sqlite'}")
    base = datetime.now(UTC)
    for i in range(n):
        db.insert_detections([
            Detection(
                source_name="tbb-test",
                started_at=base + timedelta(seconds=i),
                duration_s=3.0,
                scientific_name=f"Species {i}",
                common_name=f"sp{i}",
                confidence=0.7,
            )
        ])
    return db


def _cfg(tmp_path, **kw):
    return AppConfig(
        tbb_unit_id="tbb-test",
        tbb_central_url="http://central.test",
        tbb_device_token="tok",
        tbb_sync_state_file=tmp_path / "state.json",
        **kw,
    )


def test_sync_state_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    assert SyncState.load(p).last_synced_id == 0  # missing file → 0
    st = SyncState(p, 42)
    st.save()
    assert SyncState.load(p).last_synced_id == 42


def test_fetch_batch_is_ordered_and_capped(tmp_path):
    db = _db_with(tmp_path, 5)
    rows = fetch_batch(db, since_id=0, limit=3)
    assert [r.id for r in rows] == [1, 2, 3]
    rows = fetch_batch(db, since_id=3, limit=10)
    assert [r.id for r in rows] == [4, 5]


def test_detections_payload_shape(tmp_path):
    db = _db_with(tmp_path, 1)
    rows = fetch_batch(db, 0, 10)
    payload = detections_payload("tbb-test", rows)
    assert payload["unit"] == "tbb-test"
    assert payload["schema"] == 1
    d = payload["detections"][0]
    assert d["client_id"] == "tbb-test:1"
    assert d["scientific_name"] == "Species 0"
    assert d["has_clip"] is False
    assert "started_at" in d and d["duration_s"] == 3.0


def test_sync_once_drains_in_capped_batches(tmp_path, monkeypatch):
    db = _db_with(tmp_path, 5)
    cfg = _cfg(tmp_path, tbb_sync_batch_size=2)
    state = SyncState.load(cfg.tbb_sync_state_file)
    calls = []
    monkeypatch.setattr(
        tbb_sync, "post_batch",
        lambda url, tok, payload, **k: calls.append(len(payload["detections"])) or True,
    )

    sent = sync_once(db, cfg, state)

    assert sent == 5
    assert calls == [2, 2, 1]  # capped batches, drained oldest-first
    assert state.last_synced_id == 5
    # Persisted, so a restart resumes from the mark.
    assert SyncState.load(cfg.tbb_sync_state_file).last_synced_id == 5


def test_sync_once_offline_keeps_mark_and_loses_nothing(tmp_path, monkeypatch):
    db = _db_with(tmp_path, 3)
    cfg = _cfg(tmp_path)
    state = SyncState.load(cfg.tbb_sync_state_file)
    monkeypatch.setattr(tbb_sync, "post_batch", lambda *a, **k: False)  # central down

    assert sync_once(db, cfg, state) == 0
    assert state.last_synced_id == 0  # not advanced — backlog intact

    # Reconnect: everything drains, no loss.
    monkeypatch.setattr(tbb_sync, "post_batch", lambda *a, **k: True)
    assert sync_once(db, cfg, state) == 3
    assert state.last_synced_id == 3


def test_start_sync_agent_noop_when_disabled(tmp_path):
    db = _db_with(tmp_path, 0)
    cfg = AppConfig(tbb_sync_enabled=False, tbb_sync_state_file=tmp_path / "s.json")
    assert start_sync_agent(db, cfg, threading.Event()) is None
