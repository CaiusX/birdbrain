from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from birdbrain import tbb_sync
from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
from birdbrain.sync_status import jittered
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


# --- state files must not depend on the working directory -------------------

def test_state_files_are_anchored_to_the_database(tmp_path):
    """They defaulted to relative "data/…" paths, so a process started from
    anywhere but the checkout root looked at a file that was not there. On the
    cloud side that reads as first-run and seeds past the backlog; on this side
    it replays from zero. Both silent. Anchor them to the database they
    describe, which is absolute on a provisioned unit."""
    root = tmp_path / "unit" / "data"
    root.mkdir(parents=True)
    cfg = AppConfig(db_url=f"sqlite:///{root / 'birdbrain.sqlite'}")

    assert cfg.tbb_sync_state_file == root / "tbb_sync_state.json"
    assert cfg.birdnetcloud_state_file == root / "birdnetcloud_sync_state.json"
    assert cfg.tbb_sync_state_file.is_absolute()


def test_an_explicit_state_path_is_left_alone(tmp_path):
    explicit = tmp_path / "somewhere-else.json"
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'x.sqlite'}",
        tbb_sync_state_file=explicit,
    )
    assert cfg.tbb_sync_state_file == explicit


def test_a_non_file_database_leaves_the_defaults_relative():
    """No directory to anchor to — must not crash or invent one."""
    cfg = AppConfig(db_url="sqlite:///:memory:")
    assert not cfg.tbb_sync_state_file.is_absolute()


# --- connection reuse, jitter, keep-alive clock -----------------------------

def test_session_is_created_once_and_reused(tmp_path, monkeypatch):
    """A fresh TLS handshake every 45s is ~4MB/day — more than the payload."""
    db = _db_with(tmp_path, 0)
    cfg = _cfg(tmp_path, tbb_sync_interval_seconds=1)
    made = []
    monkeypatch.setattr(tbb_sync, "make_session",
                        lambda tok: made.append(tok) or _DummySession())
    monkeypatch.setattr(tbb_sync, "post_batch", lambda *a, **k: True)

    stop = threading.Event()
    ticks = {"n": 0}
    stop.wait = lambda t: (ticks.__setitem__("n", ticks["n"] + 1), ticks["n"] >= 3)[1]
    tbb_sync._sync_loop(db, cfg, stop)

    assert len(made) == 1, f"one session for the loop's life, got {len(made)}"


def test_intervals_are_jittered_so_a_fleet_does_not_phase_lock(tmp_path):
    """Units are flashed from one image and powered up together."""

    seen = {round(jittered(300), 3) for _ in range(50)}
    assert len(seen) > 40, "jitter should actually vary"
    assert all(255 <= v <= 345 for v in seen), f"out of the +/-15% band: {sorted(seen)[:3]}"


def test_keepalive_has_its_own_clock(tmp_path, monkeypatch):
    """Empty batches carry no data and cost ~2MB/day on a metered link. They
    should refresh central's liveness occasionally, not every tick."""
    db = _db_with(tmp_path, 0)
    cfg = _cfg(tmp_path, tbb_sync_interval_seconds=1, tbb_sync_keepalive_seconds=1800)
    monkeypatch.setattr(tbb_sync, "make_session", lambda tok: _DummySession())
    posts = []
    monkeypatch.setattr(tbb_sync, "post_batch", lambda *a, **k: posts.append(1) or True)

    clock = {"t": 100.0}
    monkeypatch.setattr(tbb_sync.time, "monotonic", lambda: clock["t"])
    stop = threading.Event()
    ticks = {"n": 0}

    def counting_wait(timeout):
        ticks["n"] += 1
        clock["t"] += 60.0          # a minute per tick, inside the 1800s window
        return ticks["n"] >= 5
    stop.wait = counting_wait
    tbb_sync._sync_loop(db, cfg, stop)

    assert len(posts) == 1, f"5 idle ticks inside one keep-alive window -> 1 post, got {len(posts)}"


def test_keepalive_can_be_turned_off_entirely(tmp_path, monkeypatch):
    """0 = a unit that only ever speaks when it has something to report."""
    db = _db_with(tmp_path, 0)
    cfg = _cfg(tmp_path, tbb_sync_interval_seconds=1, tbb_sync_keepalive_seconds=0)
    monkeypatch.setattr(tbb_sync, "make_session", lambda tok: _DummySession())
    posts = []
    monkeypatch.setattr(tbb_sync, "post_batch", lambda *a, **k: posts.append(1) or True)

    stop = threading.Event()
    ticks = {"n": 0}
    stop.wait = lambda t: (ticks.__setitem__("n", ticks["n"] + 1), ticks["n"] >= 4)[1]
    tbb_sync._sync_loop(db, cfg, stop)

    assert posts == [], "an idle unit with keep-alive off should send nothing"


class _DummySession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def post(self, *a, **kw):
        raise AssertionError("post_batch is patched in these tests")
