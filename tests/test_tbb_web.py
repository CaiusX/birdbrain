from __future__ import annotations

import types
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from africam.config import AppConfig
from africam.detector.birdnet import Detection
from africam.storage import Database
from africam.web import tbb_app
from africam.web.tbb_app import create_tbb_app, list_alsa_devices, update_env_file


def _make_app(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'web.sqlite'}",
        clips_dir=tmp_path / "clips",
        tbb_unit_id="tbb-test",
        tbb_timezone="UTC",
    )
    db = Database(cfg.db_url)
    db.insert_detections([
        Detection(
            source_name="tbb-test",
            started_at=datetime.now(UTC),
            duration_s=3.0,
            scientific_name="Pycnonotus tricolor",
            common_name="Dark-capped Bulbul",
            confidence=0.83,
        )
    ])
    return create_tbb_app(cfg)


def test_now_page_renders_unit_and_detection(tmp_path):
    client = TestClient(_make_app(tmp_path))
    r = client.get("/")
    assert r.status_code == 200
    assert "tbb-test" in r.text
    assert "Dark-capped Bulbul" in r.text
    assert "species today" in r.text


def test_feed_fragment_and_today_and_health(tmp_path):
    client = TestClient(_make_app(tmp_path))

    feed = client.get("/feed")
    assert feed.status_code == 200
    assert "Dark-capped Bulbul" in feed.text

    today = client.get("/today")
    assert today.status_code == 200
    assert "Dark-capped Bulbul" in today.text

    health = client.get("/healthz")
    assert health.status_code == 200
    body = health.json()
    assert body["ok"] is True
    assert body["unit"] == "tbb-test"


def test_setup_page_lists_devices(tmp_path, monkeypatch):
    sample = (
        "**** List of CAPTURE Hardware Devices ****\n"
        "card 0: Headset [Logitech USB Headset], device 0: USB Audio [USB Audio]\n"
        "  Subdevices: 1/1\n"
    )
    monkeypatch.setattr(
        tbb_app.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(stdout=sample),
    )
    client = TestClient(_make_app(tmp_path))
    r = client.get("/setup")
    assert r.status_code == 200
    assert "plughw:0,0" in r.text


def test_clip_404_when_missing(tmp_path):
    client = TestClient(_make_app(tmp_path))
    assert client.get("/clips/9999").status_code == 404


def test_list_alsa_devices_parses_cards(monkeypatch):
    sample = (
        "card 0: Headset [Logitech USB Headset], device 0: USB Audio [USB Audio]\n"
        "card 1: U0x46d [USB Device], device 2: USB Audio\n"
        "  Subdevices: 1/1\n"
    )
    monkeypatch.setattr(
        tbb_app.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(stdout=sample),
    )
    devices = list_alsa_devices()
    assert {d["device"] for d in devices} == {"plughw:0,0", "plughw:1,2"}


def test_list_alsa_devices_empty_when_no_arecord(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("arecord not installed")

    monkeypatch.setattr(tbb_app.subprocess, "run", _boom)
    assert list_alsa_devices() == []


def test_update_env_file_replaces_and_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# unit config\nAFRICAM_TBB_UNIT_ID=old\nAFRICAM_LOG_LEVEL=INFO\n",
        encoding="utf-8",
    )
    update_env_file(env, {
        "AFRICAM_TBB_UNIT_ID": "tbb-a1b2",
        "AFRICAM_TBB_MIC_DEVICE": "plughw:0,0",
    })
    text = env.read_text(encoding="utf-8")
    assert "AFRICAM_TBB_UNIT_ID=tbb-a1b2" in text
    assert "AFRICAM_TBB_UNIT_ID=old" not in text
    assert "AFRICAM_TBB_MIC_DEVICE=plughw:0,0" in text  # appended
    assert "AFRICAM_LOG_LEVEL=INFO" in text  # untouched
    assert "# unit config" in text  # comment preserved
