from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from birdbrain.audio import MicSource
from birdbrain.config import AppConfig, SourceConfig
from birdbrain.detector.birdnet import NON_BIRD_CLASSES, Detection
from birdbrain.pipeline import build_source, should_hash_clips
from birdbrain.storage import Database, DetectionRow
from birdbrain.storage.models import AppSettingRow
from birdbrain.tbb import prune_clips, sweep_orphan_spectrograms, tbb_source_config
from birdbrain.web.tbb_app import heartbeat_fresh_s


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
    # Every cached spectrogram variant next to the old clip should go too.
    # ".fire.png" is the one that actually leaked: the pruner used to look for
    # "<stem>.png" and "<stem>.large.png" while the web app wrote "<stem>.fire.png",
    # so the cache grew forever while its clips were deleted.
    old_pngs = [
        old_clip.with_suffix(".png"),
        old_clip.parent / f"{old_clip.stem}.large.png",
        old_clip.parent / f"{old_clip.stem}.fire.png",
    ]
    for png in old_pngs:
        png.write_bytes(b"png")

    now = datetime.now(UTC)
    db.insert_detections([_det("Old", now - timedelta(days=30))], clip_path=str(old_clip))
    db.insert_detections([_det("New", now)], clip_path=str(new_clip))

    deleted = prune_clips(db, retention_days=14)

    assert deleted == 1
    assert not old_clip.exists()
    for png in old_pngs:
        assert not png.exists(), f"{png.name} outlived its clip"
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


# --- clip fingerprinting is central-only ------------------------------------
# The hash catches YouTube ad/highlight replays, which a live mic cannot
# produce. Skipping it saves an OGG decode + resample + mel-spectrogram per
# detection. (The memory win came separately, from dropping librosa out of
# audio/quality.py — see test_audio_quality.py.)

def test_a_mic_unit_does_not_fingerprint_its_clips():
    app = AppConfig()  # default audio_hash_enabled=True — the mic check alone must win
    cfg = tbb_source_config(AppConfig(tbb_unit_id="unit", tbb_mic_device="plughw:1,0"))
    assert cfg.kind == "mic"
    assert should_hash_clips(app, cfg) is False


def test_central_still_fingerprints_by_default():
    """Regression guard: the replay filter is load-bearing on YouTube sources."""
    app = AppConfig()
    assert app.audio_hash_enabled is True
    yt = SourceConfig(name="Tembe", kind="youtube", url="https://example.test/x")
    assert should_hash_clips(app, yt) is True


def test_the_flag_switches_off_a_non_mic_source_too():
    """A unit provisioned with BIRDBRAIN_AUDIO_HASH_ENABLED=false is honoured
    whatever its source kind, so the .env is a real off-switch and not just
    documentation of what the kind check already does."""
    off = AppConfig(audio_hash_enabled=False)
    yt = SourceConfig(name="Tembe", kind="youtube", url="https://example.test/x")
    assert should_hash_clips(off, yt) is False


def test_sweep_orphan_spectrograms_clears_the_leaked_cache(tmp_path):
    """PNGs whose clip was already pruned are unreachable by prune_clips — that
    row's clip_path is NULL, so nothing points at them. They need their own
    sweep or they stay on the card forever."""
    clips = tmp_path / "clips" / "tbb-test" / "2026-08-01"
    clips.mkdir(parents=True)
    orphan = clips / "20260801T100000_000000Z.fire.png"
    orphan.write_bytes(b"x" * 4096)
    (clips / "nested").mkdir()
    (clips / "nested" / "another.png").write_bytes(b"y" * 2048)
    keep = clips / "20260801T100000_000000Z.ogg"
    keep.write_bytes(b"audio")

    files, freed = sweep_orphan_spectrograms(tmp_path / "clips")

    assert files == 2
    assert freed == 4096 + 2048
    assert not orphan.exists()
    assert keep.exists(), "the sweep must never touch audio"


def test_sweep_orphan_spectrograms_is_idempotent_and_safe_when_empty(tmp_path):
    """Runs on every pipeline start, so a second pass must be a cheap no-op —
    and a unit with no clips_dir yet must not crash the prune thread."""
    clips = tmp_path / "clips"
    clips.mkdir()
    assert sweep_orphan_spectrograms(clips) == (0, 0)
    assert sweep_orphan_spectrograms(tmp_path / "does-not-exist") == (0, 0)


# --- SD-card write budget ---------------------------------------------------

def test_central_keeps_the_fast_heartbeat():
    """Central's liveness view calls a source stale after 60s, so raising this
    default would show every YouTube source as dead. A unit overrides it in its
    own .env instead."""
    assert AppConfig().worker_heartbeat_seconds == 15.0


def test_listening_window_follows_the_heartbeat_cadence():
    """tbb-update.sh gates its rollback on `"listening": true`. If the freshness
    window ever falls below the beat interval, a perfectly good update rolls
    itself back — so this must track the cadence, not be a fixed 90s."""

    fast = AppConfig(worker_heartbeat_seconds=15.0)
    slow = AppConfig(worker_heartbeat_seconds=60.0)
    very_slow = AppConfig(worker_heartbeat_seconds=300.0)

    assert heartbeat_fresh_s(fast) == 90.0          # floor still applies
    assert heartbeat_fresh_s(slow) == 180.0         # 3 beats of slack
    assert heartbeat_fresh_s(very_slow) == 900.0
    for cfg in (fast, slow, very_slow):
        assert heartbeat_fresh_s(cfg) > cfg.worker_heartbeat_seconds, (
            "a unit must never look dead between two consecutive beats"
        )


def test_analyze_runs_once_not_on_every_boot(tmp_path):
    """Database() is constructed twice per unit, plus twice per self-update, and
    ANALYZE is a full index scan and a write that grows with the table."""
    url = f"sqlite:///{tmp_path / 'a.sqlite'}"
    Database(url)
    with Database(url).session() as s:
        marker = s.execute(
            select(AppSettingRow).where(AppSettingRow.key == "analyze:detections")
        ).scalar_one_or_none()
    assert marker is not None, "first construction should record that it ran"

    db = Database(url)
    with db.engine.begin() as conn:
        assert db._analyze_is_due(conn) is False, "a fresh marker means not due"


def test_analyze_becomes_due_again_when_the_marker_is_stale(tmp_path):
    url = f"sqlite:///{tmp_path / 'b.sqlite'}"
    db = Database(url)
    old = (datetime.now(UTC) - timedelta(days=db.ANALYZE_INTERVAL_DAYS + 1)).isoformat()
    db.set_setting("analyze:detections", old)
    with db.engine.begin() as conn:
        assert db._analyze_is_due(conn) is True


def test_temp_store_is_memory(tmp_path):
    """Sort/GROUP BY scratch must not spill onto the card."""
    db = Database(f"sqlite:///{tmp_path / 'c.sqlite'}")
    with db.engine.begin() as conn:
        # 2 == MEMORY
        assert conn.exec_driver_sql("PRAGMA temp_store").scalar() == 2
