"""The /admin live-audition endpoint.

Two things are pinned here. First, that an RTSP source is offered at all — the
endpoint used to understand only youtube, device and tbb:// and fell through to
a bare 404 for anything else.

Second, and more useful: that a stream which fails to start says so. A
StreamingResponse has already committed 200 by the time the generator runs, so
an ffmpeg that dies on startup used to arrive as a silent, empty, apparently
healthy audio element — the worst kind of failure to debug from a browser. The
first chunk is now read before the response is returned.

Djuma is the case that prompted this: its camera serves exactly one client and
the detection worker permanently holds that slot, so audition there can only
ever fail, and it should fail legibly.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
from birdbrain.web import app as app_mod
from birdbrain.web.app import create_app

RTSP_URL = "rtsp://example.invalid:8554/cam-audio"


def _app(tmp_path, *, kind="rtsp", url=RTSP_URL):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "nope.toml",
        sites_file=tmp_path / "nope.toml",
        media_cache_enabled=False,
    )
    db = Database(cfg.db_url)
    db.add_runtime_source(
        name="Cam", kind=kind, url=url, lat=-24.8, lon=31.5,
        min_confidence=0.5, timezone="Africa/Johannesburg",
    )
    db.insert_detections([
        Detection("Cam", datetime.now(UTC), 3.0, "Testus birdus", "Test Bird", 0.8)
    ])
    return create_app(cfg)


class _FakeProc:
    """Stands in for ffmpeg: emits ``payload`` then EOF."""

    def __init__(self, payload: bytes, *, err: str = "", err_file=None):
        self.stdout = io.BytesIO(payload)
        self._done = payload == b""
        if err and err_file is not None:
            err_file.write(err.encode())
            err_file.flush()

    def poll(self):
        return 0 if self._done else None

    def kill(self):
        self._done = True

    def wait(self, timeout=None):
        self._done = True
        return 0


def _patch_ffmpeg(monkeypatch, payload: bytes, err: str = ""):
    """Capture the ffmpeg argv and hand back a canned process."""
    seen: dict = {}

    def fake_popen(cmd, stdout=None, stderr=None, **kw):
        seen["cmd"] = cmd
        seen["err_file"] = stderr
        return _FakeProc(payload, err=err, err_file=stderr)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return seen


def test_rtsp_source_is_offered_live_audio(tmp_path, monkeypatch):
    """It used to 404: rtsp fell past every branch of the kind dispatch."""
    seen = _patch_ffmpeg(monkeypatch, b"\xff\xfb" + b"\x00" * 4096)
    r = TestClient(_app(tmp_path)).get("/api/sites/Cam/live.mp3")
    assert r.status_code == 200
    assert r.content.startswith(b"\xff\xfb")
    assert "-rtsp_transport" in seen["cmd"]
    assert RTSP_URL in seen["cmd"]


def test_a_stream_that_cannot_start_is_a_502_not_a_silent_200(tmp_path, monkeypatch):
    """The failure this endpoint used to hide. An empty body under a 200 looks
    like a working player with no sound."""
    _patch_ffmpeg(monkeypatch, b"", err="Server returned 400 Bad Request\n")
    r = TestClient(_app(tmp_path)).get("/api/sites/Cam/live.mp3")
    assert r.status_code == 502
    assert "400 Bad Request" in r.json()["detail"]


def test_rtsp_failure_explains_the_single_client_case(tmp_path, monkeypatch):
    """ffmpeg says "400 Bad Request", which does not tell you that the camera
    only serves one listener and the detector already has it."""
    _patch_ffmpeg(monkeypatch, b"", err="Server returned 400 Bad Request\n")
    r = TestClient(_app(tmp_path)).get("/api/sites/Cam/live.mp3")
    assert "only one client" in r.json()["detail"]


def test_non_rtsp_failure_does_not_get_the_rtsp_hint(tmp_path, monkeypatch):
    """The hint would be actively misleading on a mic."""
    _patch_ffmpeg(monkeypatch, b"", err="Device or resource busy\n")
    app = _app(tmp_path, kind="device", url="pulse:some_source")
    r = TestClient(app).get("/api/sites/Cam/live.mp3")
    assert r.status_code == 502
    assert "only one client" not in r.json()["detail"]


def test_mic_kind_explains_why_it_cannot_be_auditioned(tmp_path, monkeypatch):
    """The admin table offers a button for mic sources, so a bare 404 read as a
    broken feature. MicSource holds ALSA exclusively; the fix is to route it
    through PipeWire, and the response now says so."""
    _patch_ffmpeg(monkeypatch, b"data")
    app = _app(tmp_path, kind="mic", url="plughw:1,0")
    r = TestClient(app).get("/api/sites/Cam/live.mp3")
    assert r.status_code == 409
    assert "pulse:" in r.json()["detail"]


def test_the_stderr_temp_file_is_cleaned_up(tmp_path, monkeypatch):
    """One temp file per audition request would otherwise pile up in /tmp."""
    seen = _patch_ffmpeg(monkeypatch, b"", err="boom\n")
    TestClient(_app(tmp_path)).get("/api/sites/Cam/live.mp3")
    assert not Path(seen["err_file"].name).exists()


def test_live_audio_is_lan_only(tmp_path, monkeypatch):
    """Each listener spawns an ffmpeg on the Pi; this is an admin tool."""
    _patch_ffmpeg(monkeypatch, b"\xff\xfb")
    r = TestClient(_app(tmp_path)).get(
        "/api/sites/Cam/live.mp3", headers={"CF-Connecting-IP": "203.0.113.7"}
    )
    assert r.status_code == 404


def test_app_module_imports_tempfile():
    """The endpoint writes ffmpeg's stderr to a temp file rather than a pipe —
    an unread pipe would deadlock a ten-minute stream that logs steadily."""
    assert hasattr(app_mod, "tempfile")
