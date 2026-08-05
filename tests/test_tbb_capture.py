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
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database, DetectionRow, WorkerHeartbeatRow
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


def test_a_species_floor_can_sit_above_the_source_floor(tmp_path):
    """Used to hold back a loud common bird that buries everything quieter."""
    app = _app(tmp_path)
    db = Database(app.db_url)
    db.set_species_min_confidence("Corvus albus", 0.95)
    detector = _FakeDetector([_det("Corvus albus", 0.80), _det("Quiet bird", 0.80)])

    stop = threading.Event()
    run_capture(_cfg(), app, detector, db, _FakeSource(1), stop)

    with db.session() as s:
        kept = [r.scientific_name for r in s.query(DetectionRow).all()]
    assert kept == ["Quiet bird"], f"0.80 is below the 0.95 species floor, got {kept}"


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
