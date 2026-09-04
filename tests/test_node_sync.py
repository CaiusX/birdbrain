from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

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
from birdbrain.storage import Database
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
