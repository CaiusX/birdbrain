from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from birdbrain import node_sync
from birdbrain.detector.birdnet import Detection
from birdbrain.node_sync import (
    MarkStore,
    NodeSyncConfig,
    SourceLink,
    detections_payload,
    fetch_batch,
    load_node_config,
    sync_link_once,
    sync_once,
)
from birdbrain.storage import Database, DetectionRow
from birdbrain.wire import SCHEMA_VERSION, WireBatch

CAM_A = "Nkorho Bush Lodge"
CAM_B = "Deteema Springs"


def _db_with(tmp_path, per_source):
    """A node database holding several cams' detections interleaved by time —
    the shape the real pipeline produces, with ids from all sources mixed."""
    db = Database(f"sqlite:///{tmp_path / 'node.sqlite'}")
    base = datetime.now(UTC)
    i = 0
    for offset in range(max(per_source.values(), default=0)):
        for name, n in per_source.items():
            if offset >= n:
                continue
            db.insert_detections([
                Detection(
                    source_name=name,
                    started_at=base + timedelta(seconds=i),
                    duration_s=3.0,
                    scientific_name=f"Species {i}",
                    common_name=f"sp{i}",
                    confidence=0.7,
                )
            ])
            i += 1
    return db


def _cfg(tmp_path, **kw):
    kw.setdefault(
        "links",
        [
            SourceLink(source=CAM_A, unit=CAM_A, token="tok-a"),
            SourceLink(source=CAM_B, unit=CAM_B, token="tok-b"),
        ],
    )
    return NodeSyncConfig(
        central_url="http://central.test",
        state_file=tmp_path / "state.json",
        **kw,
    )


def _live(db, *names):
    """Mark each cam's worker alive. The keep-alive is gated on it, so tests
    about the keep-alive clock need this or they pass for the wrong reason."""
    for n in names:
        db.worker_started(n)
        db.worker_heartbeat(n)
    return db


# --- marks -----------------------------------------------------------------


def test_mark_store_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    assert MarkStore.load(p).get(CAM_A) == 0  # missing file → 0
    ms = MarkStore(p)
    ms.set(CAM_A, 42)
    ms.set(CAM_B, 7)
    ms.save()
    reloaded = MarkStore.load(p)
    assert reloaded.get(CAM_A) == 42
    assert reloaded.get(CAM_B) == 7
    assert reloaded.get("never seen") == 0


def test_unparseable_mark_replays_only_its_own_source(tmp_path):
    """One corrupt entry must not reset the whole roster's progress."""
    p = tmp_path / "state.json"
    p.write_text('{"marks": {"' + CAM_A + '": "banana", "' + CAM_B + '": 9}}')
    ms = MarkStore.load(p)
    assert ms.get(CAM_A) == 0
    assert ms.get(CAM_B) == 9


# --- partitioning: the reason this module exists ---------------------------


def test_fetch_batch_partitions_by_source(tmp_path):
    """The whole point: a link must only ever see its own cam's rows.

    tbb_sync's fetch_batch takes the entire DB, which on a multi-cam node would
    file every cam's detections under one unit at one lat/lon.
    """
    db = _db_with(tmp_path, {CAM_A: 3, CAM_B: 3})
    rows = fetch_batch(db, CAM_A, since_id=0, limit=100)
    assert {r.source_name for r in rows} == {CAM_A}
    assert len(rows) == 3


def test_fetch_batch_is_ordered_and_capped(tmp_path):
    db = _db_with(tmp_path, {CAM_A: 5, CAM_B: 5})
    rows = fetch_batch(db, CAM_A, since_id=0, limit=3)
    ids = [r.id for r in rows]
    assert ids == sorted(ids)
    assert len(ids) == 3


def test_a_stalled_cam_does_not_hold_back_the_others(tmp_path, monkeypatch):
    """A cam central rejects must not block the rest of the roster — the
    failure mode a single shared high-water mark would produce."""
    db = _db_with(tmp_path, {CAM_A: 4, CAM_B: 4})
    cfg = _cfg(tmp_path)
    marks = MarkStore(cfg.state_file)

    def fake_post(url, token, payload, **kw):
        return payload["unit"] != CAM_A  # A always fails, B always succeeds

    monkeypatch.setattr(node_sync, "post_batch", fake_post)
    results = sync_once(db, cfg, marks, {})
    assert results[CAM_A] == 0
    assert results[CAM_B] == 4
    assert marks.get(CAM_A) == 0  # not advanced — backlog retried next tick
    assert marks.get(CAM_B) > 0


def test_one_links_exception_does_not_abort_the_pass(tmp_path, monkeypatch):
    db = _db_with(tmp_path, {CAM_A: 2, CAM_B: 2})
    cfg = _cfg(tmp_path)
    marks = MarkStore(cfg.state_file)

    def fake_post(url, token, payload, **kw):
        if payload["unit"] == CAM_A:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(node_sync, "post_batch", fake_post)
    results = sync_once(db, cfg, marks, {})
    assert results[CAM_A] == 0
    assert results[CAM_B] == 2


# --- payload / wire contract ----------------------------------------------


def test_payload_validates_against_the_shared_wire_schema(tmp_path):
    """A node must be indistinguishable from a TBB unit on the wire — central
    parses both with the same model, and it is not being changed for this."""
    db = _db_with(tmp_path, {CAM_A: 2})
    rows = fetch_batch(db, CAM_A, 0, 10)
    link = SourceLink(source=CAM_A, unit=CAM_A, token="tok")
    payload = detections_payload(link, rows, "Africa/Johannesburg")
    batch = WireBatch.model_validate(payload)
    assert batch.unit == CAM_A
    assert batch.schema_version == SCHEMA_VERSION
    assert batch.timezone == "Africa/Johannesburg"
    assert len(batch.detections) == 2
    # A node measures no mic, so it sends no quality snapshot rather than a fake.
    assert batch.audio_quality is None


def test_client_ids_are_namespaced_per_unit(tmp_path):
    """Two links share one local id space, so the unit prefix is what keeps
    their idempotency keys from colliding on central."""
    db = _db_with(tmp_path, {CAM_A: 2, CAM_B: 2})
    a = detections_payload(
        SourceLink(source=CAM_A, unit=CAM_A, token="t"), fetch_batch(db, CAM_A, 0, 10), None
    )
    b = detections_payload(
        SourceLink(source=CAM_B, unit=CAM_B, token="t"), fetch_batch(db, CAM_B, 0, 10), None
    )
    ids_a = {d["client_id"] for d in a["detections"]}
    ids_b = {d["client_id"] for d in b["detections"]}
    assert not ids_a & ids_b
    assert all(i.startswith(f"{CAM_A}:") for i in ids_a)


def test_empty_payload_is_a_valid_keepalive(tmp_path):
    link = SourceLink(source=CAM_A, unit=CAM_A, token="tok")
    batch = WireBatch.model_validate(detections_payload(link, [], None))
    assert batch.detections == []


# --- draining --------------------------------------------------------------


def test_drain_advances_mark_per_batch(tmp_path, monkeypatch):
    """The mark is saved as each batch is acked, so a node killed mid-drain
    resumes rather than replaying the whole run."""
    db = _db_with(tmp_path, {CAM_A: 5})
    cfg = _cfg(tmp_path, batch_size=2)
    marks = MarkStore(cfg.state_file)
    seen = []

    def fake_post(url, token, payload, **kw):
        seen.append(len(payload["detections"]))
        return True

    monkeypatch.setattr(node_sync, "post_batch", fake_post)
    sent = sync_link_once(db, cfg, cfg.links[0], marks)
    assert sent == 5
    assert seen == [2, 2, 1]
    assert MarkStore.load(cfg.state_file).get(CAM_A) == marks.get(CAM_A)


def test_failed_post_leaves_mark_put(tmp_path, monkeypatch):
    db = _db_with(tmp_path, {CAM_A: 3})
    cfg = _cfg(tmp_path)
    marks = MarkStore(cfg.state_file)
    monkeypatch.setattr(node_sync, "post_batch", lambda *a, **k: False)
    assert sync_link_once(db, cfg, cfg.links[0], marks) == 0
    assert marks.get(CAM_A) == 0


def test_each_link_authenticates_with_its_own_token(tmp_path, monkeypatch):
    """A pooled session carries the header set on it, so sharing one across
    links would send every cam under whichever token was installed last."""
    db = _db_with(tmp_path, {CAM_A: 1, CAM_B: 1})
    cfg = _cfg(tmp_path)
    marks = MarkStore(cfg.state_file)
    used = {}

    def fake_post(url, token, payload, **kw):
        used[payload["unit"]] = token
        return True

    monkeypatch.setattr(node_sync, "post_batch", fake_post)
    sync_once(db, cfg, marks, {})
    assert used == {CAM_A: "tok-a", CAM_B: "tok-b"}


# --- keep-alive gating -----------------------------------------------------


def test_keepalive_suppressed_when_worker_is_wedged(tmp_path, monkeypatch):
    """A cam whose worker has stopped must not keep vouching for itself —
    that would hide the exact failure the dashboard exists to surface."""
    db = _db_with(tmp_path, {CAM_A: 0, CAM_B: 0})
    cfg = _cfg(tmp_path, links=[SourceLink(source=CAM_A, unit=CAM_A, token="t")])
    marks = MarkStore(cfg.state_file)
    posts = []
    monkeypatch.setattr(
        node_sync, "post_batch", lambda u, t, p, **k: posts.append(p) or True
    )
    # No heartbeat recorded at all → treated as wedged.
    sync_once(db, cfg, marks, {}, keepalive_due={}, now=0.0)
    assert posts == []


def test_keepalive_posts_for_a_quiet_but_live_cam(tmp_path, monkeypatch):
    db = _live(_db_with(tmp_path, {CAM_A: 0}), CAM_A)
    cfg = _cfg(tmp_path, links=[SourceLink(source=CAM_A, unit=CAM_A, token="t")])
    marks = MarkStore(cfg.state_file)
    posts = []
    monkeypatch.setattr(
        node_sync, "post_batch", lambda u, t, p, **k: posts.append(p) or True
    )
    sync_once(db, cfg, marks, {}, keepalive_due={}, now=0.0)
    assert len(posts) == 1
    assert posts[0]["detections"] == []


def test_backlog_flushes_even_when_the_worker_is_wedged(tmp_path, monkeypatch):
    """Rows captured before a wedge are still good data — only the keep-alive
    is gated on liveness, never real detections."""
    db = _db_with(tmp_path, {CAM_A: 3})  # no heartbeat → wedged
    cfg = _cfg(tmp_path, links=[SourceLink(source=CAM_A, unit=CAM_A, token="t")])
    marks = MarkStore(cfg.state_file)
    monkeypatch.setattr(node_sync, "post_batch", lambda *a, **k: True)
    assert sync_once(db, cfg, marks, {}, keepalive_due={}, now=0.0)[CAM_A] == 3


# --- config ----------------------------------------------------------------


def test_load_node_config_parses_links(tmp_path):
    p = tmp_path / "node.toml"
    p.write_text(
        'central_url = "http://central.test"\n'
        'state_file = "data/x.json"\n'
        "\n[[link]]\n"
        f'source = "{CAM_A}"\nunit = "nkorho"\ntoken = "abc"\n'
        "\n[[link]]\n"
        f'source = "{CAM_B}"\nunit = "deteema"\ntoken = "def"\n'
    )
    cfg = load_node_config(p)
    assert cfg.central_url == "http://central.test"
    assert [ln.unit for ln in cfg.links] == ["nkorho", "deteema"]
    assert cfg.links[0].source == CAM_A


def test_missing_node_config_says_how_to_make_one(tmp_path):
    try:
        load_node_config(tmp_path / "absent.toml")
    except FileNotFoundError as e:
        assert "node.example.toml" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_batch_size_cannot_exceed_centrals_body_cap():
    """Central rejects a body over 2000 detections wholesale, so a node that
    configured a larger batch would stall permanently and silently."""
    with pytest.raises(ValueError):
        NodeSyncConfig(central_url="http://c.test", batch_size=5000)


def test_run_node_sync_stops_promptly_when_asked(tmp_path, monkeypatch):
    db = _db_with(tmp_path, {CAM_A: 0})
    cfg = _cfg(tmp_path, links=[SourceLink(source=CAM_A, unit=CAM_A, token="t")])
    monkeypatch.setattr(node_sync, "post_batch", lambda *a, **k: True)
    stop = threading.Event()
    stop.set()
    node_sync.run_node_sync(db, cfg, stop)  # returns rather than hanging


# --- clips ------------------------------------------------------------------


def _db_with_clips(tmp_path, source, windows, per_window=1, clip_dir=None):
    """``windows`` detections-windows for one cam, ``per_window`` species in
    each, every row of a window sharing one clip file on disk — the shape the
    pipeline writes. Returns (db, [clip paths])."""
    db = Database(f"sqlite:///{tmp_path / 'node.sqlite'}")
    clip_dir = clip_dir or (tmp_path / "clips" / source)
    clip_dir.mkdir(parents=True, exist_ok=True)
    base = datetime.now(UTC) - timedelta(days=10)
    paths = []
    for w in range(windows):
        at = base + timedelta(seconds=3 * w)
        p = clip_dir / f"w{w}.ogg"
        p.write_bytes(b"OggS" + bytes([w]) * 64)
        paths.append(p)
        db.insert_detections(
            [
                Detection(
                    source_name=source, started_at=at, duration_s=3.0,
                    scientific_name=f"Species {w}-{k}", common_name="x", confidence=0.6,
                )
                for k in range(per_window)
            ],
            clip_path=str(p),
        )
    return db, paths


def _acked_rows(db, marks, source):
    """Pretend central acked every row of ``source`` (sets the row mark)."""
    with db.session() as s:
        last = s.scalar(
            select(func.max(DetectionRow.id)).where(DetectionRow.source_name == source)
        )
    marks.set(source, last)
    marks.save()
    return last


def test_mark_store_carries_clip_marks_and_tolerates_their_absence(tmp_path):
    p = tmp_path / "state.json"
    m = MarkStore(p)
    m.set(CAM_A, 40)
    m.set_clip(CAM_A, 12)
    m.save()
    back = MarkStore.load(p)
    assert (back.get(CAM_A), back.get_clip(CAM_A)) == (40, 12)
    # A state file written before clip push existed has no clip marks: every
    # clip replays from 0, which central dedupes by skipping rows that already
    # have a file.
    p.write_text(f'{{"marks": {{"{CAM_A}": 40}}}}')
    old = MarkStore.load(p)
    assert (old.get(CAM_A), old.get_clip(CAM_A)) == (40, 0)


def test_clip_batch_groups_shared_files_and_namespaces_ids(tmp_path, monkeypatch):
    db, paths = _db_with_clips(tmp_path, CAM_A, windows=2, per_window=2)
    cfg = _cfg(tmp_path, links=[SourceLink(source=CAM_A, unit="Unit A", token="t")])
    marks = MarkStore(cfg.state_file)
    _acked_rows(db, marks, CAM_A)

    posted = []

    def fake_post(central_url, token, manifest, parts, *, session=None, timeout=90.0):
        posted.append((manifest, parts))
        return {"unknown": []}

    monkeypatch.setattr(node_sync, "post_clips", fake_post)
    sent = node_sync.upload_clips_once(db, cfg, cfg.links[0], marks)
    assert sent == 2  # two windows → two files, though four rows
    manifest, parts = posted[0]
    assert manifest["unit"] == "Unit A" and manifest["schema"] == SCHEMA_VERSION
    assert [len(c["client_ids"]) for c in manifest["clips"]] == [2, 2]
    assert all(cid.startswith("Unit A:") for c in manifest["clips"] for cid in c["client_ids"])
    assert set(parts) == {c["part"] for c in manifest["clips"]}
    assert parts[manifest["clips"][0]["part"]][1] == paths[0].read_bytes()
    # The clip mark caught up with the row mark and was persisted.
    assert marks.get_clip(CAM_A) == marks.get(CAM_A)
    assert MarkStore.load(cfg.state_file).get_clip(CAM_A) == marks.get(CAM_A)


def test_clip_mark_never_passes_the_row_mark(tmp_path, monkeypatch):
    """Rows central has not acked yet have no home for their clip: the clip
    window stops at the row mark, whatever else is in the table."""
    db, _ = _db_with_clips(tmp_path, CAM_A, windows=5)
    cfg = _cfg(tmp_path)
    marks = MarkStore(cfg.state_file)
    marks.set(CAM_A, 2)  # only the first two rows acked
    posted = []
    monkeypatch.setattr(
        node_sync, "post_clips",
        lambda *a, **k: (posted.append(a[2]) or {"unknown": []}),
    )
    assert node_sync.upload_clips_once(db, cfg, cfg.links[0], marks) == 2
    assert marks.get_clip(CAM_A) == 2
    assert sum(len(c["client_ids"]) for m in posted for c in m["clips"]) == 2


def test_failed_clip_post_leaves_clip_mark_put(tmp_path, monkeypatch):
    db, _ = _db_with_clips(tmp_path, CAM_A, windows=3)
    cfg = _cfg(tmp_path)
    marks = MarkStore(cfg.state_file)
    _acked_rows(db, marks, CAM_A)
    monkeypatch.setattr(node_sync, "post_clips", lambda *a, **k: None)
    assert node_sync.upload_clips_once(db, cfg, cfg.links[0], marks) == 0
    assert marks.get_clip(CAM_A) == 0


def test_rows_without_a_file_advance_the_clip_mark_without_a_request(tmp_path, monkeypatch):
    db, paths = _db_with_clips(tmp_path, CAM_A, windows=3)
    for p in paths:
        p.unlink()  # pruned locally, or never written
    cfg = _cfg(tmp_path)
    marks = MarkStore(cfg.state_file)
    last = _acked_rows(db, marks, CAM_A)
    calls = []
    monkeypatch.setattr(node_sync, "post_clips", lambda *a, **k: calls.append(1) or {})
    assert node_sync.upload_clips_once(db, cfg, cfg.links[0], marks) == 0
    assert calls == []
    assert marks.get_clip(CAM_A) == last


def test_clip_drain_is_bounded_per_tick(tmp_path, monkeypatch):
    db, _ = _db_with_clips(tmp_path, CAM_A, windows=10)
    cfg = _cfg(tmp_path, clip_batch_size=2, clip_batches_per_tick=3)
    marks = MarkStore(cfg.state_file)
    last = _acked_rows(db, marks, CAM_A)
    monkeypatch.setattr(node_sync, "post_clips", lambda *a, **k: {"unknown": []})
    assert node_sync.upload_clips_once(db, cfg, cfg.links[0], marks) == 6
    assert marks.get_clip(CAM_A) == last - 4
    # the rest goes next tick
    assert node_sync.upload_clips_once(db, cfg, cfg.links[0], marks) == 4
    assert marks.get_clip(CAM_A) == last


def test_sync_pass_pushes_rows_then_clips(tmp_path, monkeypatch):
    db, _ = _db_with_clips(tmp_path, CAM_A, windows=2)
    cfg = _cfg(tmp_path, links=[SourceLink(source=CAM_A, unit=CAM_A, token="t")])
    marks = MarkStore(cfg.state_file)
    order = []
    monkeypatch.setattr(node_sync, "post_batch", lambda *a, **k: order.append("rows") or True)
    monkeypatch.setattr(node_sync, "post_clips", lambda *a, **k: order.append("clips") or {})
    sync_once(db, cfg, marks)
    assert order == ["rows", "clips"]
    assert marks.get_clip(CAM_A) == marks.get(CAM_A) > 0


def test_clip_push_can_be_switched_off(tmp_path, monkeypatch):
    db, _ = _db_with_clips(tmp_path, CAM_A, windows=2)
    cfg = _cfg(tmp_path, upload_clips=False)
    marks = MarkStore(cfg.state_file)
    monkeypatch.setattr(node_sync, "post_batch", lambda *a, **k: True)
    monkeypatch.setattr(node_sync, "post_clips", lambda *a, **k: pytest.fail("clips posted"))
    sync_once(db, cfg, marks)
    assert marks.get_clip(CAM_A) == 0


def test_local_prune_only_removes_clips_central_has_acked(tmp_path):
    db, paths = _db_with_clips(tmp_path, CAM_A, windows=4)  # all 10 days old
    cfg = _cfg(tmp_path, clip_retention_days=3)
    marks = MarkStore(cfg.state_file)
    marks.set_clip(CAM_A, 2)  # central holds the first two
    removed = node_sync.prune_uploaded_clips(db, cfg, marks)
    assert removed == 2
    assert [p.exists() for p in paths] == [False, False, True, True]
    with db.session() as s:
        rows = sorted(s.scalars(select(DetectionRow)), key=lambda r: r.id)
    assert [r.clip_path is None for r in rows] == [True, True, False, False]


def test_local_prune_keeps_recent_clips_even_when_acked(tmp_path):
    db, paths = _db_with_clips(tmp_path, CAM_A, windows=2)
    cfg = _cfg(tmp_path, clip_retention_days=30)  # window longer than the rows' age
    marks = MarkStore(cfg.state_file)
    _acked_rows(db, marks, CAM_A)
    marks.set_clip(CAM_A, marks.get(CAM_A))
    assert node_sync.prune_uploaded_clips(db, cfg, marks) == 0
    assert all(p.exists() for p in paths)
