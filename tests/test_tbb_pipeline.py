from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from africam.audio import MicSource
from africam.config import AppConfig
from africam.detector.birdnet import NON_BIRD_CLASSES, Detection
from africam.pipeline import build_source
from africam.storage import Database, DetectionRow
from africam.tbb import prune_clips, tbb_source_config


def test_tbb_source_config_is_a_single_mic_source():
    app = AppConfig(
        tbb_unit_id="tbb-a1b2",
        tbb_mic_device="plughw:2,0",
        tbb_lat=-25.7,
        tbb_lon=28.2,
        tbb_min_confidence=0.6,
    )
    cfg = tbb_source_config(app)
    assert cfg.kind == "mic"
    assert cfg.name == "tbb-a1b2"
    assert cfg.device == "plughw:2,0"
    assert cfg.lat == -25.7 and cfg.lon == 28.2
    assert cfg.min_confidence == 0.6
    # No site resolution on a unit — it's a single static-location source.
    assert cfg.multisite is False
    # The unit drops BirdNET's non-bird noise classes.
    assert cfg.exclude_non_bird is True


def test_non_bird_classes_cover_noise_not_birds():
    # The noise classes a unit should hide...
    assert {"Engine", "Dog", "Siren", "Human vocal"} <= NON_BIRD_CLASSES
    assert len(NON_BIRD_CLASSES) == 11
    # ...but never a real species.
    assert "Pycnonotus tricolor" not in NON_BIRD_CLASSES


def test_build_source_constructs_micsource_for_mic_kind():
    app = AppConfig()
    cfg = tbb_source_config(AppConfig(tbb_unit_id="unit", tbb_mic_device="plughw:1,0"))
    source = build_source(cfg, app)
    assert isinstance(source, MicSource)
    assert source.device == "plughw:1,0"
    assert source.name == "unit"
    # ffmpeg command reads ALSA, confirming the mic path is wired end-to-end.
    assert "alsa" in source._ffmpeg_command()


def _det(sci: str, started_at: datetime) -> Detection:
    return Detection(
        source_name="unit",
        started_at=started_at,
        duration_s=3.0,
        scientific_name=sci,
        common_name=sci.lower(),
        confidence=0.9,
    )


def test_prune_clips_deletes_old_keeps_recent(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'tbb.sqlite'}")
    old_clip = tmp_path / "old.ogg"
    new_clip = tmp_path / "new.ogg"
    old_clip.write_bytes(b"clip")
    new_clip.write_bytes(b"clip")
    # A cached spectrogram PNG next to the old clip should go too.
    old_png = old_clip.with_suffix(".png")
    old_png.write_bytes(b"png")

    now = datetime.now(UTC)
    db.insert_detections([_det("Old", now - timedelta(days=30))], clip_path=str(old_clip))
    db.insert_detections([_det("New", now)], clip_path=str(new_clip))

    deleted = prune_clips(db, retention_days=14)

    assert deleted == 1
    assert not old_clip.exists()
    assert not old_png.exists()
    assert new_clip.exists()

    with db.session() as s:
        paths = {r.scientific_name: r.clip_path for r in s.scalars(select(DetectionRow))}
    assert paths["Old"] is None  # row kept, clip_path NULLed
    assert paths["New"] == str(new_clip)


def test_prune_clips_noop_when_nothing_old(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'tbb.sqlite'}")
    clip = tmp_path / "recent.ogg"
    clip.write_bytes(b"clip")
    db.insert_detections([_det("Recent", datetime.now(UTC))], clip_path=str(clip))

    assert prune_clips(db, retention_days=14) == 0
    assert clip.exists()
