from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
import requests

from birdbrain import birdnetcloud_sync as bnc
from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database


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


class _DummySession:
    """Stands in for make_session; records that the loop closed it on exit."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


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


def test_heartbeat_uses_their_field_names(tmp_path):
    """Regression: we first sent {"version": ...}. Their agent sends
    ``firmware_version``, and the API 204s on anything — so the wrong key was
    not an error, it was a station that looked like it never registered."""
    info = bnc.host_info()
    assert info["firmware_version"] == bnc.FIRMWARE_VERSION
    assert "version" not in info
    # The station card / Network tab fields their dashboard reads.
    for key in ("hostname", "os_name", "hardware_detected", "free_disk_gb"):
        assert key in info, f"heartbeat missing {key}"
    assert info["queue_depth"] == 0
    assert all(v is not None for v in info.values())


def test_heartbeat_reports_real_backlog(tmp_path):
    db = _db_with(tmp_path, [0.9] * 7)
    state = bnc.CloudState(tmp_path / "s.json", 2)
    assert bnc.host_info(db, state)["queue_depth"] == 5


def test_session_is_reused_and_carries_auth(tmp_path):
    """Without keep-alive every POST pays a fresh TLS handshake — 1.4s of a 1.6s
    request from a Pi Zero, ~9MB/day of handshake traffic on a field link."""
    s = bnc.make_session("tok-123")
    assert s.headers["Authorization"] == "Bearer tok-123"
    # No urllib3 Retry: their API has no idempotency key, so a transparently
    # retried POST that did reach the server would duplicate the detection.
    for adapter in s.adapters.values():
        assert adapter.max_retries.total in (0, None), "retries would risk duplicates"


def test_sync_once_passes_the_session_through(tmp_path, monkeypatch):
    db = _db_with(tmp_path, [0.9, 0.9])
    cfg = _cfg(tmp_path)
    state = bnc.CloudState(cfg.birdnetcloud_state_file, 0)
    seen = []

    def fake_post(endpoint, token, payload, *, session=None, **kw):
        seen.append(session)
        return "uuid"

    monkeypatch.setattr(bnc, "post_detection", fake_post)
    sentinel = object()
    bnc.sync_once(db, cfg, state, "tok", sentinel)
    assert seen == [sentinel, sentinel]


def test_heartbeat_has_its_own_clock(tmp_path, monkeypatch):
    """A field unit polls for detections often but must not spend a request a
    minute saying nothing changed.

    This used to fail whenever the host's uptime was under an hour: the loop
    seeded ``last_heartbeat = 0.0`` and compared it against ``time.monotonic()``,
    which is time since boot, so the first beat only fired on a box that had
    been up longer than the interval. Now the first tick always beats, so the
    single beat asserted below no longer depends on the machine running it.
    """
    cfg = _cfg(tmp_path, birdnetcloud_interval_seconds=1,
               birdnetcloud_heartbeat_seconds=3600)
    db = _db_with(tmp_path, [0.9])
    bnc.seed_state(db, cfg.birdnetcloud_state_file)

    beats = []
    monkeypatch.setattr(bnc, "post_heartbeat", lambda *a, **kw: beats.append(1) or True)
    dummy = _DummySession()
    monkeypatch.setattr(bnc, "make_session", lambda tok: dummy)

    stop = threading.Event()
    ticks = {"n": 0}
    real_wait = stop.wait

    def counting_wait(timeout):
        ticks["n"] += 1
        return ticks["n"] >= 4          # let 4 ticks run, then stop
    stop.wait = counting_wait
    bnc._loop(db, cfg, "tok", stop)
    stop.wait = real_wait

    # 4 polls, one hour between heartbeats -> exactly one beat.
    assert ticks["n"] == 4
    assert len(beats) == 1, f"expected 1 heartbeat across 4 polls, got {len(beats)}"
    assert dummy.closed, "session must be closed when the loop exits"


def test_first_heartbeat_fires_immediately_after_a_reboot(tmp_path, monkeypatch):
    """time.monotonic() is time since boot, so seeding last_heartbeat to 0.0
    silently meant 'beat only if the box has been up longer than the interval'.
    A unit the net-watchdog just rebooted would read offline on their dashboard
    for a full heartbeat window. Pin the fresh-boot clock explicitly."""
    cfg = _cfg(tmp_path, birdnetcloud_interval_seconds=1,
               birdnetcloud_heartbeat_seconds=1800)
    db = _db_with(tmp_path, [0.9])
    bnc.seed_state(db, cfg.birdnetcloud_state_file)

    # 12 s of uptime — far below the 1800 s heartbeat interval.
    monkeypatch.setattr(bnc.time, "monotonic", lambda: 12.0)
    beats = []
    monkeypatch.setattr(bnc, "post_heartbeat", lambda *a, **kw: beats.append(1) or True)
    monkeypatch.setattr(bnc, "make_session", lambda tok: _DummySession())

    stop = threading.Event()
    ticks = {"n": 0}
    stop.wait = lambda timeout: (ticks.__setitem__("n", ticks["n"] + 1), ticks["n"] >= 2)[1]
    bnc._loop(db, cfg, "tok", stop)

    assert len(beats) == 1, "a freshly-booted unit must beat on its first tick"


def test_heartbeat_interval_is_respected_after_the_first_beat(tmp_path, monkeypatch):
    """The flip side: having beaten once, don't beat again until the interval
    has genuinely elapsed. Clock is pinned so this no longer depends on the
    host's uptime the way the original test silently did."""
    cfg = _cfg(tmp_path, birdnetcloud_interval_seconds=1,
               birdnetcloud_heartbeat_seconds=1800)
    db = _db_with(tmp_path, [0.9])
    bnc.seed_state(db, cfg.birdnetcloud_state_file)

    clock = {"t": 100.0}
    monkeypatch.setattr(bnc.time, "monotonic", lambda: clock["t"])
    beats = []
    monkeypatch.setattr(bnc, "post_heartbeat", lambda *a, **kw: beats.append(1) or True)
    monkeypatch.setattr(bnc, "make_session", lambda tok: _DummySession())

    stop = threading.Event()
    ticks = {"n": 0}

    def counting_wait(timeout):
        ticks["n"] += 1
        clock["t"] += 60.0          # a minute per tick, well inside the interval
        return ticks["n"] >= 4
    stop.wait = counting_wait
    bnc._loop(db, cfg, "tok", stop)

    assert ticks["n"] == 4
    assert len(beats) == 1, f"4 ticks x 60s inside a 1800s interval -> 1 beat, got {len(beats)}"


# --- state durability: the backlog must survive a bad state file ------------

def test_missing_state_seeds_but_corrupt_state_refuses(tmp_path):
    """The whole point of Stage 2. Missing means first run — seed past history.
    Unreadable means we HAD a mark and lost it, and seeding then silently drops
    every unsent row. Their API has no dedupe, so a dropped backlog is gone."""
    db = _db_with(tmp_path, [0.9] * 40)
    cfg = _cfg(tmp_path)
    path = cfg.birdnetcloud_state_file

    # Missing -> seed to the newest row.
    seeded = bnc.resolve_state(db, path)
    assert seeded is not None and seeded.last_pushed_id == 40

    # Corrupt, with no usable backup -> refuse to start.
    path.write_text("{not json", encoding="utf-8")
    path.with_name(path.name + ".bak").unlink(missing_ok=True)
    assert bnc.resolve_state(db, path) is None


def test_corrupt_state_recovers_from_the_backup(tmp_path):
    """A few duplicates beats a lost queue."""
    db = _db_with(tmp_path, [0.9] * 10)
    cfg = _cfg(tmp_path)
    path = cfg.birdnetcloud_state_file

    good = bnc.CloudState(path, 3)
    good.save()          # writes the primary
    bnc.CloudState(path, 7).save()   # rotates 3 -> .bak, primary now 7
    path.write_text("", encoding="utf-8")   # primary corrupted

    recovered = bnc.resolve_state(db, path)
    assert recovered is not None
    assert recovered.last_pushed_id == 3, "should fall back to the retained copy"


def test_state_write_is_atomic_and_keeps_a_backup(tmp_path):
    path = tmp_path / "state.json"
    bnc.CloudState(path, 5).save()
    bnc.CloudState(path, 9).save()

    assert json.loads(path.read_text())["last_pushed_id"] == 9
    assert json.loads(path.with_suffix(".json.bak").read_text())["last_pushed_id"] == 5
    # No temp file left lying around for a reader to trip over.
    assert not (tmp_path / "state.json.tmp").exists()


def test_garbage_contents_are_corrupt_not_a_fresh_start(tmp_path):
    """Well-formed JSON with a nonsense mark must not read as first-run."""
    db = _db_with(tmp_path, [0.9] * 5)
    path = tmp_path / "s.json"
    path.write_text('{"last_pushed_id": "banana"}', encoding="utf-8")
    assert bnc.resolve_state(db, path) is None


# --- drain efficiency -------------------------------------------------------

def test_sub_threshold_rows_do_not_rewrite_state_or_eat_the_budget(tmp_path, monkeypatch):
    """A unit detecting at 0.5 against a 0.7 cloud floor used to spend a full
    state-file rewrite per dropped row, and those rows consumed the per-tick
    send budget."""
    db = _db_with(tmp_path, [0.1] * 50 + [0.9])
    cfg = _cfg(tmp_path, birdnetcloud_max_per_tick=10)
    state = bnc.CloudState(cfg.birdnetcloud_state_file, 0)
    monkeypatch.setattr(bnc, "post_detection", _Recorder())

    saves = []
    real_save = bnc.CloudState.save
    monkeypatch.setattr(bnc.CloudState, "save",
                        lambda self: saves.append(self.last_pushed_id) or real_save(self))

    sent, skipped = bnc.sync_once(db, cfg, state, "tok")

    assert sent == 1, "the one row above the floor is sent despite 50 below it"
    assert skipped == 50
    assert state.last_pushed_id == 51, "mark clears the whole sub-threshold run"
    assert len(saves) <= 3, f"expected a couple of writes, got {len(saves)}"


def test_the_clean_tick_jump_cannot_skip_a_concurrent_insert(tmp_path, monkeypatch):
    """The end-of-tick jump uses the max id read BEFORE the fetch. A row that
    arrives mid-tick must be picked up next time, not stepped over."""
    db = _db_with(tmp_path, [0.9])
    cfg = _cfg(tmp_path)
    state = bnc.CloudState(cfg.birdnetcloud_state_file, 0)

    def send_then_insert(endpoint, token, payload, **kw):
        db.insert_detections([
            Detection(source_name="tbb-test", started_at=datetime(2026, 7, 30, tzinfo=UTC),
                      duration_s=3.0, scientific_name="Late", common_name="Late",
                      confidence=0.95)
        ])
        return "uuid-1"

    monkeypatch.setattr(bnc, "post_detection", send_then_insert)
    bnc.sync_once(db, cfg, state, "tok")
    assert state.last_pushed_id == 1, "must not jump past the row inserted mid-tick"

    rec = _Recorder()
    monkeypatch.setattr(bnc, "post_detection", rec)
    sent, _ = bnc.sync_once(db, cfg, state, "tok")
    assert sent == 1 and rec.sent[0]["common_name"] == "Late"


# --- clips are not lost when their upload fails -----------------------------

def test_failed_clip_upload_is_queued_and_retried(tmp_path, monkeypatch):
    """The detection lands and the mark moves past it, so a dropped clip is gone
    for good unless we remember it. The audio is on the card for days."""
    clip = tmp_path / "det.ogg"
    clip.write_bytes(b"audio")
    db = _db_with(tmp_path, [])
    db.insert_detections([
        Detection(source_name="tbb-test", started_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
                  duration_s=3.0, scientific_name="S", common_name="C", confidence=0.9)
    ], clip_path=str(clip))
    cfg = _cfg(tmp_path, birdnetcloud_upload_clips=True)
    state = bnc.CloudState(cfg.birdnetcloud_state_file, 0)
    monkeypatch.setattr(bnc, "post_detection", _Recorder())

    monkeypatch.setattr(bnc, "upload_clip", lambda *a, **kw: False)   # link down
    bnc.sync_once(db, cfg, state, "tok")
    assert state.pending_clips == [(1, "uuid-1")]
    assert state.last_pushed_id == 1, "the detection itself still went"

    tried = []
    monkeypatch.setattr(bnc, "upload_clip",
                        lambda ep, tok, cid, path, **kw: tried.append((cid, path)) or True)
    bnc.sync_once(db, cfg, state, "tok")
    assert tried == [("uuid-1", str(clip))]
    assert state.pending_clips == []


def test_pending_clip_is_dropped_once_its_audio_is_pruned(tmp_path, monkeypatch):
    """Retention wins: the row is in the cloud, only the audio is gone, and
    there is nothing left to retry."""
    db = _db_with(tmp_path, [])
    db.insert_detections([
        Detection(source_name="tbb-test", started_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
                  duration_s=3.0, scientific_name="S", common_name="C", confidence=0.9)
    ], clip_path=str(tmp_path / "already-pruned.ogg"))
    cfg = _cfg(tmp_path)
    state = bnc.CloudState(cfg.birdnetcloud_state_file, 1)
    state.queue_clip(1, "uuid-1")

    monkeypatch.setattr(bnc, "upload_clip",
                        lambda *a, **kw: pytest.fail("must not upload a missing file"))
    bnc.sync_once(db, cfg, state, "tok")
    assert state.pending_clips == []


def test_pending_clips_are_bounded(tmp_path):
    """A week offline must not grow the state file without limit."""
    state = bnc.CloudState(tmp_path / "s.json", 0)
    for i in range(bnc.CloudState.MAX_PENDING_CLIPS + 25):
        state.queue_clip(i, f"uuid-{i}")
    assert len(state.pending_clips) == bnc.CloudState.MAX_PENDING_CLIPS
    assert state.pending_clips[0][0] == 25, "oldest dropped first"


def test_pending_clips_survive_a_restart(tmp_path):
    path = tmp_path / "s.json"
    state = bnc.CloudState(path, 4)
    state.queue_clip(7, "uuid-7")
    state.save()

    reloaded, outcome = bnc.CloudState.load(path)
    assert outcome is bnc.StateRead.OK
    assert reloaded.last_pushed_id == 4
    assert reloaded.pending_clips == [(7, "uuid-7")]


# --- clip policy: the only bandwidth lever their API leaves us --------------

def _db_with_clips(tmp_path, species_seq):
    """One detection per entry, each with a real clip file on disk."""
    db = Database(f"sqlite:///{tmp_path / 'bnc.sqlite'}")
    base = datetime(2026, 7, 29, 8, 0, 0, tzinfo=UTC)
    for i, sci in enumerate(species_seq):
        clip = tmp_path / f"clip{i}.ogg"
        clip.write_bytes(b"audio")
        db.insert_detections([
            Detection(source_name="tbb-test", started_at=base + timedelta(minutes=i),
                      duration_s=3.0, scientific_name=sci, common_name=sci,
                      confidence=0.9)
        ], clip_path=str(clip))
    return db


def _uploads(tmp_path, monkeypatch, policy, species_seq, state=None, cfg=None):
    db = _db_with_clips(tmp_path, species_seq)
    cfg = cfg or _cfg(tmp_path, birdnetcloud_upload_clips=True,
                      birdnetcloud_clip_policy=policy)
    state = state or bnc.CloudState(cfg.birdnetcloud_state_file, 0)
    monkeypatch.setattr(bnc, "post_detection", _Recorder())
    got = []
    monkeypatch.setattr(bnc, "upload_clip",
                        lambda ep, tok, cid, path, **kw: got.append(path) or True)
    bnc.sync_once(db, cfg, state, "tok")
    return got, state


def test_clip_policy_all_uploads_every_clip(tmp_path, monkeypatch):
    got, _ = _uploads(tmp_path, monkeypatch, "all", ["Dove", "Dove", "Dove"])
    assert len(got) == 3


def test_clip_policy_none_uploads_nothing_but_still_sends_detections(tmp_path, monkeypatch):
    got, state = _uploads(tmp_path, monkeypatch, "none", ["Dove", "Bulbul"])
    assert got == []
    assert state.last_pushed_id == 2, "detections still go; only the audio is dropped"


def test_clip_policy_first_per_species_per_day(tmp_path, monkeypatch):
    """The 30th Laughing Dove of the morning is where the bytes go."""
    got, state = _uploads(
        tmp_path, monkeypatch, "first_per_species_per_day",
        ["Dove", "Dove", "Bulbul", "Dove", "Bulbul"],
    )
    assert len(got) == 2, "one per species, not one per detection"
    assert state.clip_species == {"Dove", "Bulbul"}


def test_clip_policy_resets_on_a_new_day(tmp_path):
    state = bnc.CloudState(tmp_path / "s.json", 0)
    assert state.claim_first_clip_of_day("2026-07-29", "Dove") is True
    assert state.claim_first_clip_of_day("2026-07-29", "Dove") is False
    assert state.claim_first_clip_of_day("2026-07-30", "Dove") is True
    assert state.clip_species == {"Dove"}, "yesterday's set is discarded, not grown"


def test_clip_policy_survives_a_restart(tmp_path):
    """Otherwise every restart re-sends one clip per species."""
    path = tmp_path / "s.json"
    state = bnc.CloudState(path, 1)
    state.claim_first_clip_of_day("2026-07-29", "Dove")
    state.save()

    reloaded, _ = bnc.CloudState.load(path)
    assert reloaded.claim_first_clip_of_day("2026-07-29", "Dove") is False


# --- failure classification -------------------------------------------------

def test_connection_error_and_timeout_are_logged_differently(tmp_path, monkeypatch):
    """A refused connection provably never arrived; a timeout may well have
    been processed. Both retry, but only one can duplicate — and a duplicate on
    their dashboard should have an explanation in the journal."""
    events = []
    monkeypatch.setattr(bnc.log, "warning", lambda ev, **kw: events.append(ev))

    class _Boom:
        def __init__(self, exc):
            self.exc = exc

        def post(self, *a, **kw):
            raise self.exc

    assert bnc.post_detection("http://x", "t", {}, session=_Boom(
        requests.ConnectionError("refused"))) is None
    assert bnc.post_detection("http://x", "t", {}, session=_Boom(
        requests.Timeout("slow"))) is None

    assert events == ["birdnetcloud.unreachable", "birdnetcloud.ambiguous_send"]
