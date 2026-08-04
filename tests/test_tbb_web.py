from __future__ import annotations

import subprocess
import sys
import types
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
from birdbrain.web import tbb_app
from birdbrain.web.tbb_app import create_tbb_app, list_alsa_devices, update_env_file


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


def test_setup_page_has_mic_test_button(tmp_path):
    r = TestClient(_make_app(tmp_path)).get("/setup")
    assert "/setup/mic-sample" in r.text
    assert "Test microphone" in r.text


def test_setup_page_has_enroll_form(tmp_path):
    r = TestClient(_make_app(tmp_path)).get("/setup")
    assert "/setup/enroll" in r.text
    assert "Claim code" in r.text


def test_setup_enroll_saves_issued_credentials(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(tbb_app, "update_env_file", lambda p, u: captured.update(u))
    monkeypatch.setattr(
        tbb_app.requests, "post",
        lambda *a, **k: types.SimpleNamespace(
            status_code=200, json=lambda: {"unit_id": "patio", "token": "TKN123"}
        ),
    )
    client = TestClient(_make_app(tmp_path))
    r = client.post(
        "/setup/enroll",
        data={"central_url": "http://c:8000/", "code": "BIRD-1",
              "display_name": "Patio", "lat": "-25.7", "lon": "28.2"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "enrolled=patio" in r.headers["location"]
    assert captured["BIRDBRAIN_TBB_DEVICE_TOKEN"] == "TKN123"
    assert captured["BIRDBRAIN_TBB_UNIT_ID"] == "patio"
    assert captured["BIRDBRAIN_TBB_SYNC_ENABLED"] == "true"
    assert captured["BIRDBRAIN_TBB_CENTRAL_URL"] == "http://c:8000"  # trailing slash trimmed
    assert captured["BIRDBRAIN_TBB_LAT"] == "-25.7"


def test_setup_enroll_surfaces_central_error(tmp_path, monkeypatch):
    calls = {"env": 0}

    def _count_env(p, u):
        calls["env"] += 1

    monkeypatch.setattr(tbb_app, "update_env_file", _count_env)
    monkeypatch.setattr(
        tbb_app.requests, "post",
        lambda *a, **k: types.SimpleNamespace(
            status_code=400, json=lambda: {"detail": "invalid or already-used claim code"}, text=""
        ),
    )
    client = TestClient(_make_app(tmp_path))
    r = client.post(
        "/setup/enroll",
        data={"central_url": "http://c", "code": "WRONG"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "enroll_error" in r.headers["location"]
    assert calls["env"] == 0  # nothing written on failure


def test_mic_sample_returns_ogg_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tbb_app.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=b"OggS-fake-audio", stderr=b""),
    )
    r = TestClient(_make_app(tmp_path)).get("/setup/mic-sample?seconds=2")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/ogg"
    assert r.content == b"OggS-fake-audio"


def test_mic_sample_applies_cleanup_filters(tmp_path, monkeypatch):
    captured = {}

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout=b"OggS", stderr=b"")

    monkeypatch.setattr(tbb_app.subprocess, "run", _fake_run)
    client = TestClient(_make_app(tmp_path))

    # No filters → no -af.
    client.get("/setup/mic-sample?gain_db=0&denoise=0&highpass=0")
    assert "-af" not in captured["cmd"]

    # All three → a single -af chain in the documented order.
    client.get("/setup/mic-sample?gain_db=6&denoise=1&highpass=1")
    af = captured["cmd"][captured["cmd"].index("-af") + 1]
    assert af == "highpass=f=120,afftdn=nr=12,volume=6.0dB"


def test_mic_sample_409_when_device_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tbb_app.subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"Device or resource busy"
        ),
    )
    r = TestClient(_make_app(tmp_path)).get("/setup/mic-sample")
    assert r.status_code == 409
    assert "in use" in r.json()["detail"]


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
        "# unit config\nBIRDBRAIN_TBB_UNIT_ID=old\nBIRDBRAIN_LOG_LEVEL=INFO\n",
        encoding="utf-8",
    )
    update_env_file(env, {
        "BIRDBRAIN_TBB_UNIT_ID": "tbb-a1b2",
        "BIRDBRAIN_TBB_MIC_DEVICE": "plughw:0,0",
    })
    text = env.read_text(encoding="utf-8")
    assert "BIRDBRAIN_TBB_UNIT_ID=tbb-a1b2" in text
    assert "BIRDBRAIN_TBB_UNIT_ID=old" not in text
    assert "BIRDBRAIN_TBB_MIC_DEVICE=plughw:0,0" in text  # appended
    assert "BIRDBRAIN_LOG_LEVEL=INFO" in text  # untouched
    assert "# unit config" in text  # comment preserved


# --- no spectrograms on a unit; the feed serves the audio itself -------------

def _app_with_clip(tmp_path):
    """App whose one detection has a real clip file, so has_clip is True."""
    clips = tmp_path / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    clip = clips / "det.ogg"
    clip.write_bytes(b"not really ogg, but a file")
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'web.sqlite'}",
        clips_dir=clips,
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
    ], clip_path=str(clip))
    return create_tbb_app(cfg)


def test_spectrogram_route_is_gone(tmp_path):
    """Rendering these cost a per-request ffmpeg fork and an SD write on the
    smallest box in the fleet, to make something central regenerates for free."""
    client = TestClient(_app_with_clip(tmp_path))
    assert client.get("/spectrograms/1.png").status_code == 404


def test_feed_offers_playable_audio_instead_of_an_image(tmp_path):
    client = TestClient(_app_with_clip(tmp_path))
    feed = client.get("/feed")
    assert feed.status_code == 200
    assert "/clips/1" in feed.text
    # preload="none" is load-bearing: the fragment re-renders every 5s, so an
    # eager preload would re-fetch every clip in the feed on every poll.
    assert 'preload="none"' in feed.text
    assert "/spectrograms/" not in feed.text
    assert 'class="spec"' not in feed.text


def test_feed_omits_the_player_when_the_clip_was_pruned(tmp_path):
    """clip_path goes NULL at retention, so the row survives without audio —
    the feed must not offer a dead play button."""
    client = TestClient(_make_app(tmp_path))     # detection has no clip
    feed = client.get("/feed")
    assert feed.status_code == 200
    assert "Dark-capped Bulbul" in feed.text
    assert "<audio" not in feed.text


def test_clip_route_still_serves_audio(tmp_path):
    """Central pulls unit clips from here, so this must keep working."""
    client = TestClient(_app_with_clip(tmp_path))
    r = client.get("/clips/1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/")


# --- the unit must not construct central's dashboard ------------------------

_UNIT_IMPORT_PROBE = """
import os, pathlib, sys, tempfile
d = tempfile.mkdtemp()
os.chdir(d)
os.environ["BIRDBRAIN_DB_URL"] = "sqlite:///./probe.sqlite"
os.environ["BIRDBRAIN_CLIPS_DIR"] = "./clips"
import birdbrain.web.tbb_app  # noqa: F401
print("central=%s lock=%s" % (
    "birdbrain.web.app" in sys.modules,
    pathlib.Path(".media-sweeper.lock").exists(),
))
"""


def test_importing_the_unit_app_does_not_build_central():
    """birdbrain.web.app ends with a module-level ``app = create_app()``, so an
    eager re-export in birdbrain/web/__init__.py makes merely importing the unit
    app construct the entire central dashboard: a second Database() with its
    migration and ANALYZE, the operator account, the species linkifier, the
    TOML loads, and a bid for the media-sweeper lock. On a 415MB field unit,
    on every tbb-web start. Keep the package __init__ lazy.
    """
    out = subprocess.run(
        [sys.executable, "-c", _UNIT_IMPORT_PROBE],
        capture_output=True, text=True, timeout=180, check=True,
    )
    assert "central=False" in out.stdout, f"unit imported central's app: {out.stdout!r}"
    assert "lock=False" in out.stdout, f"unit grabbed the sweeper lock: {out.stdout!r}"
