"""A muted cam is disabled, tracked, and put back on the roster by itself if
its audio ever returns."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from birdbrain.config import AppConfig
from birdbrain.storage import Database

_spec = importlib.util.spec_from_file_location(
    "check_muted_cams", Path(__file__).resolve().parents[1] / "scripts" / "check-muted-cams.py"
)
cmc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cmc)

CAM = "GRACE Gorilla Sanctuary"
OTHER = "Lola ya Bonobo"


def _setup(tmp_path, *names):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'node.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
    )
    db = Database(cfg.db_url)
    for n in names:
        db.add_runtime_source(
            name=n, kind="youtube", url=f"https://www.youtube.com/watch?v={n[:5]}",
            lat=0.0, lon=0.0, min_confidence=0.3, timezone="UTC", external=True,
        )
    return cfg, db


def test_add_disables_and_records(tmp_path):
    _, db = _setup(tmp_path, CAM, OTHER)
    cmc.add(db, [CAM], dry_run=False)
    assert CAM in db.list_disabled_source_names()
    assert cmc.muted_list(db) == [CAM]
    # ...and the other cam is untouched.
    assert OTHER not in db.list_disabled_source_names()


def test_a_cam_whose_audio_returns_is_re_enabled_and_forgotten(tmp_path, monkeypatch):
    cfg, db = _setup(tmp_path, CAM)
    cmc.add(db, [CAM], dry_run=False)
    monkeypatch.setattr(cmc.YouTubeSource, "current_url", lambda self: "http://stream")
    monkeypatch.setattr(cmc, "mean_dbfs", lambda url, seconds=20: -47.0)

    cmc.check(db, cfg, dry_run=False)

    assert CAM not in db.list_disabled_source_names()
    assert cmc.muted_list(db) == []


def test_a_still_silent_cam_stays_disabled(tmp_path, monkeypatch):
    cfg, db = _setup(tmp_path, CAM)
    cmc.add(db, [CAM], dry_run=False)
    monkeypatch.setattr(cmc.YouTubeSource, "current_url", lambda self: "http://stream")
    monkeypatch.setattr(cmc, "mean_dbfs", lambda url, seconds=20: -91.0)

    cmc.check(db, cfg, dry_run=False)

    assert CAM in db.list_disabled_source_names()
    assert cmc.muted_list(db) == [CAM]


def test_digital_silence_reported_as_minus_inf_stays_disabled(tmp_path, monkeypatch):
    cfg, db = _setup(tmp_path, CAM)
    cmc.add(db, [CAM], dry_run=False)
    monkeypatch.setattr(cmc.YouTubeSource, "current_url", lambda self: "http://stream")
    monkeypatch.setattr(cmc, "mean_dbfs", lambda url, seconds=20: float("-inf"))

    cmc.check(db, cfg, dry_run=False)
    assert CAM in db.list_disabled_source_names()


def test_a_transient_resolve_failure_never_re_enables(tmp_path, monkeypatch):
    """'No video formats found!' happens to healthy cams. It says nothing
    about the audio, so the cam must stay exactly as it was."""
    cfg, db = _setup(tmp_path, CAM)
    cmc.add(db, [CAM], dry_run=False)

    def boom(self):
        raise RuntimeError("yt-dlp failed: No video formats found!")

    monkeypatch.setattr(cmc.YouTubeSource, "current_url", boom)
    monkeypatch.setattr(cmc, "mean_dbfs", lambda *a, **k: pytest.fail("must not sample"))

    cmc.check(db, cfg, dry_run=False)
    assert CAM in db.list_disabled_source_names()
    assert cmc.muted_list(db) == [CAM]


def test_an_unmeasurable_stream_stays_disabled(tmp_path, monkeypatch):
    cfg, db = _setup(tmp_path, CAM)
    cmc.add(db, [CAM], dry_run=False)
    monkeypatch.setattr(cmc.YouTubeSource, "current_url", lambda self: "http://stream")
    monkeypatch.setattr(cmc, "mean_dbfs", lambda url, seconds=20: None)

    cmc.check(db, cfg, dry_run=False)
    assert CAM in db.list_disabled_source_names()


def test_only_listed_cams_are_ever_touched(tmp_path, monkeypatch):
    """Cams are also disabled for unrelated reasons — a YouTube IP block pauses
    the whole fleet. Re-enabling one of those on an audio check would fight the
    resume script that paused it."""
    cfg, db = _setup(tmp_path, CAM, OTHER)
    cmc.add(db, [CAM], dry_run=False)
    db.set_source_disabled(OTHER, True)  # paused by youtube-resume, not muted
    monkeypatch.setattr(cmc.YouTubeSource, "current_url", lambda self: "http://stream")
    monkeypatch.setattr(cmc, "mean_dbfs", lambda url, seconds=20: -40.0)

    cmc.check(db, cfg, dry_run=False)

    assert CAM not in db.list_disabled_source_names()   # muted one came back
    assert OTHER in db.list_disabled_source_names()     # paused one untouched


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    cfg, db = _setup(tmp_path, CAM)
    cmc.add(db, [CAM], dry_run=False)
    monkeypatch.setattr(cmc.YouTubeSource, "current_url", lambda self: "http://stream")
    monkeypatch.setattr(cmc, "mean_dbfs", lambda url, seconds=20: -40.0)

    cmc.check(db, cfg, dry_run=True)

    assert CAM in db.list_disabled_source_names()
    assert cmc.muted_list(db) == [CAM]


def test_a_cam_removed_from_the_roster_drops_off_the_list(tmp_path, monkeypatch):
    cfg, db = _setup(tmp_path)          # no runtime sources at all
    db.set_setting(cmc.MUTED_KEY, json.dumps([CAM]))
    monkeypatch.setattr(cmc, "mean_dbfs", lambda *a, **k: pytest.fail("must not sample"))

    cmc.check(db, cfg, dry_run=False)
    assert cmc.muted_list(db) == []


def test_unparseable_muted_setting_is_survivable(tmp_path):
    _, db = _setup(tmp_path)
    db.set_setting(cmc.MUTED_KEY, "{not json")
    assert cmc.muted_list(db) == []


def test_mean_dbfs_parses_ffmpeg_output(monkeypatch):
    class R:
        stderr = "[Parsed_volumedetect_0 @ 0x1] mean_volume: -47.3 dB\nmax_volume: -12.0 dB\n"
    monkeypatch.setattr(cmc.subprocess, "run", lambda *a, **k: R())
    assert cmc.mean_dbfs("http://x") == -47.3

    class Silent:
        stderr = "[Parsed_volumedetect_0 @ 0x1] mean_volume: -inf dB\n"
    monkeypatch.setattr(cmc.subprocess, "run", lambda *a, **k: Silent())
    assert cmc.mean_dbfs("http://x") == float("-inf")

    class Nothing:
        stderr = "some other ffmpeg noise"
    monkeypatch.setattr(cmc.subprocess, "run", lambda *a, **k: Nothing())
    assert cmc.mean_dbfs("http://x") is None


def test_the_floor_sits_between_silence_and_the_quietest_real_cam():
    """Digital silence reads about -91 dB; the quietest cam in the roster that
    still produces detections sits near -66. The floor must separate them."""
    assert -91.0 < cmc.AUDIO_FLOOR_DBFS < -66.0
