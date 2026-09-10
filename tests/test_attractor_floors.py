"""Choosing a confidence floor for a false-positive attractor.

An attractor is a label BirdNET reaches for when unsure — it turns up across
many genera rather than being confused with one bird. A floor helps only where
the species fires wrongly at lower confidences than it fires rightly.
"""

from __future__ import annotations

from birdbrain.cli import _pick_floor


def test_a_clear_gap_yields_the_smallest_qualifying_floor(tmp_path):
    own = [0.9] * 100                       # its own detections are all high
    wrong = [(0.2, 10), (0.3, 10)]          # it fires wrongly down low
    got = _pick_floor(own, wrong, baseline=0.15, keep_own=0.95, suppress=0.7)
    assert got is not None
    floor, kept, gone = got
    assert floor == 0.35, "smallest step clearing both bars"
    assert kept == 1.0 and gone == 1.0


def test_no_floor_when_it_fires_wrongly_where_it_fires_rightly(tmp_path):
    """The common case, and the reason most attractors are left alone."""
    own = [0.4] * 100
    wrong = [(0.45, 20)]
    assert _pick_floor(own, wrong, baseline=0.15, keep_own=0.95, suppress=0.7) is None


def test_a_floor_is_never_proposed_at_or_below_the_baseline(tmp_path):
    """THE regression. Measured against re-analysis' 0.05 rather than what
    capture keeps, Egyptian Goose — already floored at 0.90 — came back
    wanting 0.55, which would have recorded more of what the floor exists to
    suppress."""
    own = [0.95] * 100
    wrong = [(0.4, 50)]
    got = _pick_floor(own, wrong, baseline=0.90, keep_own=0.95, suppress=0.7)
    assert got is None or got[0] > 0.90


def test_the_own_detection_bar_is_respected(tmp_path):
    """A floor that would discard too much of the species' own data is not
    offered, however well it suppresses."""
    own = [0.2] * 50 + [0.9] * 50           # half sit low
    wrong = [(0.3, 20)]
    got = _pick_floor(own, wrong, baseline=0.15, keep_own=0.95, suppress=0.7)
    assert got is None, "0.35 would suppress well but drop half the detections"


def test_nothing_to_suppress_means_no_floor(tmp_path):
    assert _pick_floor([0.9] * 100, [], baseline=0.15, keep_own=0.95, suppress=0.7) is None
    assert _pick_floor([], [(0.2, 5)], baseline=0.15, keep_own=0.95, suppress=0.7) is None
