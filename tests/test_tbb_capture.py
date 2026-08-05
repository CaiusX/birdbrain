"""The unit's own capture loop.

``tbb_capture`` is a deliberate fork of ``pipeline.run_source``, so these tests
exist to pin the behaviour that was worth keeping — floors, suppressions,
pre-roll, backoff, worker state — and the fact that it no longer reaches for
central's modules.
"""
from __future__ import annotations

import ast
import pathlib
import threading
from datetime import UTC, datetime

import numpy as np
import pytest
import soundfile as sf

from birdbrain.audio.source import AudioChunk
from birdbrain.config import AppConfig, SourceConfig
from birdbrain.config_core import UnitConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database, DetectionRow, WorkerHeartbeatRow, models, models_core
from birdbrain.tbb_capture import run_capture


def _cfg(**kw):
    base = dict(
        name="tbb-test", kind="mic", url="alsa:plughw:1,0", device="plughw:1,0",
        lat=-26.1, lon=28.0, min_confidence=0.5, multisite=False, exclude_non_bird=True,
    )
    base.update(kw)
    return SourceConfig(**base)


def _app(tmp_path, **kw):
    base = dict(
        db_url=f"sqlite:///{tmp_path / 'unit.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
        save_clips=False,
    )
    base.update(kw)
    return AppConfig(**base)


class _FakeSource:
    """Yields a fixed number of chunks, then signals stop.

    Setting the event on exhaustion matters: run_capture treats a finished
    stream as a disconnect and reconnects after a backoff, which is correct for
    a real mic and would loop this test forever.
    """

    def __init__(self, n=1, sample_rate=48_000, stop_event=None):
        self.n = n
        self.sample_rate = sample_rate
        self.stop_event = stop_event

    def stream(self, stop_event=None):
        event = stop_event or self.stop_event
        for i in range(self.n):
            yield AudioChunk(
                samples=np.zeros(self.sample_rate * 3, dtype=np.float32),
                sample_rate=self.sample_rate,
                started_at=datetime(2026, 8, 5, 6, 0, i, tzinfo=UTC),
                source_name="tbb-test",
            )
        if event is not None:
            event.set()


class _FakeDetector:
    """Returns the same detections for every chunk."""

    def __init__(self, detections):
        self.detections = detections
        self.calls = 0

    def analyze(self, chunk, **kw):
        self.calls += 1
        self.kwargs = kw
        return list(self.detections)


def _det(sci="Pycnonotus tricolor", conf=0.9):
    return Detection(
        source_name="tbb-test", started_at=datetime(2026, 8, 5, 6, 0, tzinfo=UTC),
        duration_s=3.0, scientific_name=sci, common_name=sci, confidence=conf,
    )


def _run(tmp_path, detector, chunks=1, app=None, cfg=None):
    app = app or _app(tmp_path)
    db = Database(app.db_url)
    stop = threading.Event()
    run_capture(cfg or _cfg(), app, detector, db, _FakeSource(chunks), stop)
    return db


# --- the loop does its job --------------------------------------------------

def test_detections_are_written_with_the_units_static_location(tmp_path):
    """A unit has no site resolver — its location comes from enrollment."""
    detector = _FakeDetector([_det()])
    db = _run(tmp_path, detector)

    with db.session() as s:
        rows = list(s.query(DetectionRow).all())
    assert len(rows) == 1
    assert rows[0].latitude == -26.1 and rows[0].longitude == 28.0
    assert rows[0].site is None, "a single-mic unit has no site"
    # The detector was given the static coordinates, not a resolver's answer.
    assert detector.kwargs["lat"] == -26.1


def test_worker_state_transitions_are_recorded(tmp_path):
    """The unit's own page and /healthz read these; tbb-update.sh gates on them."""
    db = _run(tmp_path, _FakeDetector([]))
    with db.session() as s:
        row = s.get(WorkerHeartbeatRow, "tbb-test")
    assert row is not None
    assert row.state == "stopped", "loop exited cleanly, so the worker is stopped"


def test_the_units_floor_comes_from_its_config(tmp_path):
    """Central polls three live-tunable floors from app_settings and
    species_notes every minute. A unit has no /admin to set them: after six
    weeks the bench unit's species_notes held 70 rows and not one with a
    min_confidence, and app_settings held only debris. So the unit passes
    tbb_min_confidence straight to the detector and reads neither table.
    """
    app = _app(tmp_path)
    db = Database(app.db_url)
    # A floor set the central way must NOT influence the unit.
    db.set_species_min_confidence("Corvus albus", 0.95)
    detector = _FakeDetector([_det("Corvus albus", 0.80)])

    run_capture(_cfg(min_confidence=0.6), app, detector, db, _FakeSource(1), threading.Event())

    assert detector.kwargs["min_confidence"] == 0.6, "cfg.min_confidence is the floor"
    with db.session() as s:
        kept = [r.scientific_name for r in s.query(DetectionRow).all()]
    assert kept == ["Corvus albus"], "the central-only species floor is not consulted"


def test_suppressed_species_are_dropped(tmp_path):
    app = _app(tmp_path)
    db = Database(app.db_url)
    db.add_species_suppression("tbb-test", "Phantom bird")
    detector = _FakeDetector([_det("Phantom bird"), _det("Real bird")])

    run_capture(_cfg(), app, detector, db, _FakeSource(1), threading.Event())

    with db.session() as s:
        kept = [r.scientific_name for r in s.query(DetectionRow).all()]
    assert kept == ["Real bird"]


def test_a_chunk_with_no_detections_writes_nothing(tmp_path):
    db = _run(tmp_path, _FakeDetector([]), chunks=3)
    with db.session() as s:
        assert s.query(DetectionRow).count() == 0


def test_a_failing_detector_does_not_kill_the_loop(tmp_path):
    """One bad chunk must not take a field unit off the air."""
    class _Boom:
        calls = 0

        def analyze(self, chunk, **kw):
            _Boom.calls += 1
            raise RuntimeError("model exploded")

    db = _run(tmp_path, _Boom(), chunks=3)
    assert _Boom.calls == 3, "every chunk still attempted"
    with db.session() as s:
        assert s.query(DetectionRow).count() == 0


def test_stop_event_exits_promptly(tmp_path):
    """systemd sends SIGTERM; the loop must not sit in its backoff sleep."""
    app = _app(tmp_path)
    db = Database(app.db_url)
    stop = threading.Event()
    stop.set()
    run_capture(_cfg(), app, _FakeDetector([]), db, _FakeSource(5), stop)
    # Returning at all (rather than looping on the ended stream) is the assertion.


# --- clips ------------------------------------------------------------------

def test_clip_carries_three_seconds_of_pre_roll(tmp_path):
    """A call straddling a chunk boundary would otherwise be cut at the start."""
    app = _app(tmp_path, save_clips=True)
    db = Database(app.db_url)
    detector = _FakeDetector([_det()])

    run_capture(_cfg(), app, detector, db, _FakeSource(2), threading.Event())

    with db.session() as s:
        rows = s.query(DetectionRow).all()
    clips = {r.clip_path for r in rows if r.clip_path}
    assert clips, "a detection with save_clips on must produce a clip"
    durations = sorted(round(sf.info(p).duration, 1) for p in clips)
    # The first chunk has nothing before it, so it is saved bare (3s). Every
    # later one carries the previous chunk as pre-roll (3 + 3 = 6s).
    assert durations == [3.0, 6.0], f"expected a bare clip and a pre-rolled one, got {durations}"


# --- the point of the fork --------------------------------------------------

CENTRAL_ONLY = {
    "birdbrain.notes",            # Anthropic AI commentary
    "birdbrain.weather_worker",
    "birdbrain.site_ocr",         # pytesseract, multi-site cameras
    "birdbrain.site_resolver",
    "birdbrain.sites",
    "birdbrain.sandbox",          # an /admin feature the unit cannot set
    "birdbrain.highlight_watcher",  # PIL, YouTube replay detection
    "birdbrain.pipeline",         # the thing that drags in all of the above
}


def _birdbrain_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name.startswith("birdbrain")}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("birdbrain"):
            out.add(node.module)
    return out


@pytest.mark.parametrize("module", ["tbb.py", "tbb_capture.py", "web/tbb_app.py"])
def test_unit_modules_do_not_import_central_only_code(module):
    """The unit used to reach central's run_source, which imports seven modules
    a microphone in a field cannot use. Keeping this list empty is what makes
    the standalone repo a file copy rather than a refactor.
    """
    path = pathlib.Path("src/birdbrain") / module
    leaked = _birdbrain_imports(path) & CENTRAL_ONLY
    assert not leaked, f"{module} imports central-only: {sorted(leaked)}"


# --- the schema boundary ----------------------------------------------------

# The 8 tables a unit owns. Everything else belongs to central and should not
# follow the unit into its own repository.
UNIT_TABLES = {
    "detections",
    "worker_heartbeats",
    "worker_downtime",
    "species_suppressions",
    "audio_quality_metrics",
    "audio_quality_samples",
    "app_settings",
}


def test_models_core_holds_exactly_the_units_tables():
    """models_core.py is what the standalone repo takes. If a central table
    drifts into it, the unit repo inherits users, weather, page views and the
    rest — and has to explain why they are in a microphone's schema."""

    tables = {
        obj.__tablename__ for name, obj in vars(models_core).items()
        if name.endswith("Row") and hasattr(obj, "__tablename__")
    }
    assert tables == UNIT_TABLES, f"unexpected: {tables ^ UNIT_TABLES}"


def test_central_still_sees_every_table():
    """The split must be invisible to central: one Base, all 23 tables, and
    `from birdbrain.storage.models import X` unchanged for every X."""

    assert models.Base is models_core.Base, "a second Base would split the metadata"
    assert len(models.Base.metadata.tables) == 23
    # A representative from each side, imported the way callers already do.
    assert models.DetectionRow.__tablename__ == "detections"
    assert models.UserRow.__tablename__ == "users"


def test_the_unit_reads_no_central_only_table():
    """Guards the trim: tbb_capture used to poll species_notes and app_settings
    every minute for floors a unit cannot set."""

    tree = ast.parse(pathlib.Path("src/birdbrain/tbb_capture.py").read_text())
    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "SpeciesNoteRow" not in referenced
    for method in ("species_min_confidence_map", "global_min_confidence",
                   "source_min_confidence"):
        assert method not in ast.dump(tree), f"{method} is central's live-tuning path"


# --- the config boundary ----------------------------------------------------

def test_unit_config_holds_only_what_a_unit_reads():
    """config_core.py is what the standalone repo takes. Central's own settings
    — AI commentary, weather backfill, the media cache, TOML source files, web
    sessions and invite codes — must not follow it there."""

    fields = set(UnitConfig.model_fields)
    for central_only in (
        "notes_enabled", "notes_model", "weather_tick_seconds",
        "media_cache_enabled", "sources_file", "sites_file",
        "secret_key", "invite_code", "xeno_canto_key",
    ):
        assert central_only not in fields, f"{central_only} is central's"
    # Everything the unit's own subsystems configure.
    for needed in (
        "db_url", "clips_dir", "log_level", "worker_heartbeat_seconds",
        "tbb_unit_id", "tbb_mic_device", "tbb_min_confidence",
        "birdnetcloud_enabled", "birdnetcloud_clip_policy",
    ):
        assert needed in fields, f"{needed} is the unit's"


def test_central_config_is_a_superset_and_unchanged():
    """The split must be invisible to central: one object, every field on it,
    and `from birdbrain.config import AppConfig` untouched."""

    assert issubclass(AppConfig, UnitConfig)
    assert set(UnitConfig.model_fields) < set(AppConfig.model_fields)
    assert AppConfig.model_config["env_prefix"] == "BIRDBRAIN_"


def test_the_settings_machinery_lives_with_the_unit(tmp_path):
    """Env loading and both validators apply on either side, so they belong in
    the base — a unit that could not resolve its own state files or read its
    .env would be a repo that does not run."""

    root = tmp_path / "data"
    root.mkdir()
    cfg = UnitConfig(db_url=f"sqlite:///{root / 'unit.sqlite'}")
    assert cfg.tbb_sync_state_file == root / "tbb_sync_state.json"
    assert cfg.birdnetcloud_state_file == root / "birdnetcloud_sync_state.json"
