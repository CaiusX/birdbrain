from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from birdbrain import tbb_sync
from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
from birdbrain.tbb_sync import (
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


# --- audio quality travelling with the batch ---------------------------------

_SNAP = {
    "score": 74, "level_score": 0.62, "avail_score": 0.91, "structure_score": 0.44,
    "level_dbfs": -31.5, "silence_fraction": 0.09, "clip_fraction": 0.0012,
    "flatness": 0.33, "fraction_good": 0.81, "issue_label": "good",
    "band_hz_low": 120, "band_hz_high": 11000,
}


def test_audio_quality_snapshot_roundtrips(tmp_path):
    """The getter must hand back exactly what upsert stored, in the shape the
    payload needs — it's the seam between the unit's pipeline and the wire."""
    db = _db_with(tmp_path, 0)
    assert db.audio_quality_snapshot("tbb-test") is None

    db.upsert_audio_quality("tbb-test", _SNAP)
    got = db.audio_quality_snapshot("tbb-test")
    assert got == _SNAP
    # updated_at is deliberately absent: central stamps its own receive time.
    assert "updated_at" not in got


def test_payload_carries_audio_quality(tmp_path):
    db = _db_with(tmp_path, 1)
    rows = fetch_batch(db, 0, 10)
    assert detections_payload("tbb-test", rows)["audio_quality"] is None
    payload = detections_payload("tbb-test", rows, None, _SNAP)
    assert payload["audio_quality"]["score"] == 74


def test_sync_once_attaches_the_units_own_quality(tmp_path, monkeypatch):
    db = _db_with(tmp_path, 3)
    db.upsert_audio_quality("tbb-test", _SNAP)
    cfg = _cfg(tmp_path, tbb_sync_batch_size=2)
    state = SyncState.load(cfg.tbb_sync_state_file)
    seen = []
    monkeypatch.setattr(
        tbb_sync, "post_batch",
        lambda url, tok, payload, **k: seen.append(payload["audio_quality"]) or True,
    )

    assert sync_once(db, cfg, state) == 3
    # Every batch of the drain carries it; central keeps only the latest.
    assert [q["score"] for q in seen] == [74, 74]


def test_sync_once_without_a_quality_row_still_syncs(tmp_path, monkeypatch):
    """A unit whose accumulator hasn't warmed up yet must not fail to sync."""
    db = _db_with(tmp_path, 2)
    cfg = _cfg(tmp_path)
    state = SyncState.load(cfg.tbb_sync_state_file)
    seen = []
    monkeypatch.setattr(
        tbb_sync, "post_batch",
        lambda url, tok, payload, **k: seen.append(payload["audio_quality"]) or True,
    )

    assert sync_once(db, cfg, state) == 2
    assert seen == [None]
