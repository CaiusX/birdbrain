"""An ingest node reports its own host + pipeline state, and central's admin
health pane shows it beside central's own."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from birdbrain import node_sync
from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.ingest import NODE_HEALTH_KEY, hash_token
from birdbrain.node_sync import MarkStore, NodeSyncConfig, SourceLink, node_health_payload
from birdbrain.storage import Database
from birdbrain.web.app import create_app
from birdbrain.wire import WireNodeHealth

CAM = "Nkorho Bush Lodge"


def _central(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'central.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
        media_cache_enabled=False,
    )
    db = Database(cfg.db_url)
    db.upsert_device(CAM, hash_token("tok"), lat=0.0, lon=0.0)
    return create_app(cfg), db


def _report(node="BNE", **over):
    doc = {
        "node": node, "schema": 1, "reported_at": datetime.now(UTC).isoformat(),
        "version": "abc1234",
        "host": {"load1": 1.2, "load5": 1.1, "load15": 1.0, "cpus": 4,
                 "mem_total": 8_000_000_000, "mem_available": 5_000_000_000,
                 "temp_c": 61.0, "throttled": {"under_voltage_now": False}, "uptime_s": 3600},
        "disk": {"total": 500_000_000_000, "used": 20_000_000_000, "free": 480_000_000_000},
        "workers_running": 38, "workers_total": 38, "worker_problems": [],
        "rows_behind": 3, "clips_behind": 40, "last_detection_age_s": 12.0, "det_24h": 18000,
    }
    doc.update(over)
    return doc


def _post(app, doc, token="tok"):
    return TestClient(app).post(
        "/ingest/node-health", json=doc, headers={"Authorization": f"Bearer {token}"}
    )


# --- node side ----------------------------------------------------------------


def _node_db(tmp_path, n_rows=5):
    db = Database(f"sqlite:///{tmp_path / 'node.sqlite'}")
    base = datetime.now(UTC) - timedelta(minutes=5)
    for i in range(n_rows):
        db.insert_detections([Detection(
            source_name=CAM, started_at=base + timedelta(seconds=3 * i), duration_s=3.0,
            scientific_name=f"Sp {i}", common_name="x", confidence=0.6,
        )], clip_path=str(tmp_path / f"c{i}.ogg"))
    return db


def test_payload_reports_backlog_and_workers(tmp_path):
    db = _node_db(tmp_path, n_rows=5)
    db.worker_started(CAM)
    db.worker_heartbeat(CAM)
    cfg = NodeSyncConfig(central_url="http://c", state_file=tmp_path / "s.json",
                         links=[SourceLink(source=CAM, unit=CAM, token="t")])
    marks = MarkStore(cfg.state_file)
    marks.set(CAM, 3)        # central acked rows 1-3
    marks.set_clip(CAM, 1)   # and holds the clip for row 1
    doc = node_health_payload(db, cfg, marks, node="BNE", clips_dir=tmp_path, version="v1")
    assert (doc["rows_behind"], doc["clips_behind"]) == (2, 2)
    assert (doc["workers_running"], doc["workers_total"]) == (1, 1)
    assert doc["worker_problems"] == []
    assert doc["det_24h"] == 5 and doc["last_detection_age_s"] is not None
    assert doc["disk"]["total"] > 0 and "load1" in doc["host"]
    # ...and it is exactly what central will accept.
    WireNodeHealth.model_validate(doc)


def test_payload_flags_a_stale_or_stopped_worker(tmp_path):
    db = _node_db(tmp_path, n_rows=1)
    db.worker_started(CAM)
    db.worker_stopped(CAM)
    cfg = NodeSyncConfig(
        central_url="http://c", state_file=tmp_path / "s.json",
        links=[SourceLink(source=CAM, unit=CAM, token="t"),
               SourceLink(source="Never Started", unit="Never Started", token="t2")],
    )
    doc = node_health_payload(
        db, cfg, MarkStore(cfg.state_file), node="BNE", clips_dir=tmp_path
    )
    assert doc["workers_running"] == 0 and doc["workers_total"] == 2
    assert {p["name"]: p["status"] for p in doc["worker_problems"]} == {
        CAM: "stopped", "Never Started": "never",
    }


def test_post_node_health_is_best_effort(monkeypatch):
    class Boom:
        def post(self, *a, **k):
            raise node_sync.requests.ConnectionError("down")

    assert node_sync.post_node_health("http://c", "t", {"node": "x"}, session=Boom()) is False


# --- central side -------------------------------------------------------------


def test_report_is_stored_and_rendered(tmp_path):
    app, db = _central(tmp_path)
    r = _post(app, _report())
    assert r.status_code == 200, r.text
    stored = json.loads(db.get_setting(NODE_HEALTH_KEY + "BNE"))
    assert stored["reported_by"] == CAM and "received_at" in stored

    html = TestClient(app).get("/partials/health").text
    assert "Node BNE" in html
    assert "38/38" in html                       # workers card
    assert "3 rows · 40 clips not yet on central" in html
    assert "abc1234" in html
    assert "last reported" not in html           # fresh, so no stale banner


def test_latest_report_replaces_the_previous_one(tmp_path):
    app, db = _central(tmp_path)
    _post(app, _report(workers_running=10))
    _post(app, _report(workers_running=38))
    assert len(db.settings_with_prefix(NODE_HEALTH_KEY)) == 1
    assert "38/38" in TestClient(app).get("/partials/health").text


def test_stale_report_is_flagged(tmp_path):
    app, db = _central(tmp_path)
    _post(app, _report())
    doc = json.loads(db.get_setting(NODE_HEALTH_KEY + "BNE"))
    doc["received_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    db.set_setting(NODE_HEALTH_KEY + "BNE", json.dumps(doc))
    html = TestClient(app).get("/partials/health").text
    assert re.search(r"BNE last reported \d+m ago", html)


def test_worker_problems_and_power_fault_banner(tmp_path):
    app, _ = _central(tmp_path)
    _post(app, _report(
        workers_running=37,
        worker_problems=[{"name": "Tembe", "status": "stopped", "error": "eof"}],
        host={"load1": 1.0, "cpus": 4, "throttled": {"under_voltage_now": True}},
    ))
    html = TestClient(app).get("/partials/health").text
    assert "1 site not running on BNE" in html and "Tembe" in html
    assert "BNE is under-voltage right now" in html


def test_auth_and_schema(tmp_path):
    app, _ = _central(tmp_path)
    assert _post(app, _report(), token="nope").status_code == 403
    assert TestClient(app).post("/ingest/node-health", json=_report()).status_code == 401
    assert _post(app, _report(schema=99)).status_code == 409
    bad = _report()
    bad["workers_running"] = -1
    assert _post(app, bad).status_code == 422


def test_a_node_with_no_report_shows_nothing_extra(tmp_path):
    app, _ = _central(tmp_path)
    assert "Node " not in TestClient(app).get("/partials/health").text
