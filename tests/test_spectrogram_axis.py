"""The spectrogram's frequency overlays must agree with the image underneath.

The picture is rendered by ffmpeg with a fixed log axis; the gridlines, the
species locator boxes and the hover readout are all drawn in the browser from
constants that repeat those bounds. If the two ever drift apart nothing breaks
loudly — the overlays just quietly point at the wrong frequencies, which is
worse than no overlay at all, because the call descriptions cite bands the
operator is meant to check against them.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src/birdbrain/web/app.py"
MODAL = Path(__file__).resolve().parents[1] / "src/birdbrain/web/templates/_spectrogram_modal.html"


def _ffmpeg_bounds() -> tuple[int, int]:
    m = re.search(r'SPEC_FILTER_BASE\s*=\s*"([^"]+)"', APP.read_text())
    assert m, "SPEC_FILTER_BASE not found in app.py"
    f = m.group(1)
    return (int(re.search(r"start=(\d+)", f).group(1)),
            int(re.search(r"stop=(\d+)", f).group(1)))


def _js_bounds() -> tuple[int, int]:
    m = re.search(r"const SPEC_LO_HZ = (\d+), SPEC_HI_HZ = (\d+);", MODAL.read_text())
    assert m, "SPEC_LO_HZ / SPEC_HI_HZ not found in the modal"
    return int(m.group(1)), int(m.group(2))


def test_the_overlay_axis_matches_the_rendered_image():
    assert _js_bounds() == _ffmpeg_bounds()


def test_the_image_is_still_rendered_on_a_log_frequency_scale():
    """Both the gridline placement and the hover readout assume a log axis; a
    switch to linear would put every line in the wrong place."""
    m = re.search(r'SPEC_FILTER_BASE\s*=\s*"([^"]+)"', APP.read_text())
    assert "fscale=log" in m.group(1)


def test_every_gridline_falls_inside_the_rendered_band():
    lo, hi = _ffmpeg_bounds()
    grid = re.search(r"const FREQ_GRID = \[([^\]]+)\]", MODAL.read_text())
    assert grid, "FREQ_GRID not found"
    freqs = [int(x) for x in grid.group(1).split(",")]
    assert freqs, "no gridlines"
    assert freqs == sorted(freqs), "gridlines should read low to high"
    for f in freqs:
        assert lo < f < hi, f"{f} Hz is outside the {lo}-{hi} Hz image"


def test_the_readout_and_the_locator_boxes_use_one_shared_mapping():
    """_yPctToFreq is the inverse of _freqToYPct. If they disagree, the number
    under the cursor and the box drawn for the same species won't line up."""
    src = MODAL.read_text()
    assert "function _freqToYPct(" in src
    assert "function _yPctToFreq(" in src

    lo, hi = _ffmpeg_bounds()

    def freq_to_y(f):                      # mirrors the JS
        t = (math.log(f) - math.log(lo)) / (math.log(hi) - math.log(lo))
        return (1 - min(max(t, 0.0), 1.0)) * 100

    def y_to_freq(pct):
        t = 1 - min(max(pct / 100, 0.0), 1.0)
        return math.exp(math.log(lo) + t * (math.log(hi) - math.log(lo)))

    for f in (100, 500, 1000, 3000, 8000, 11000):
        assert abs(y_to_freq(freq_to_y(f)) - f) < 1.0, f
    # ...and the edges land where the printed labels claim.
    assert abs(y_to_freq(100) - lo) < 1.0
    assert abs(y_to_freq(0) - hi) < 1.0


def test_the_image_fills_its_box_exactly():
    """Every overlay on the spectrogram — playhead, locator boxes, gridlines,
    cursor readout — maps a percentage of the BOX to a time or a frequency.
    That is only true if the image exactly fills the box.

    object-cover scales to cover and crops the overflow. At 900x220 into a
    711x224 box it drew 916 px wide and cut 205, so the picture showed
    0.67-5.33 s of a 6 s clip while every overlay still assumed 0-6 — up to
    0.67 s out at the edges and exactly right in the middle, which is what
    "nearly in sync" looks like and why it went unnoticed.
    """
    src = MODAL.read_text()
    img = re.search(r'<img id="spec-modal-img"[^>]*>', src).group(0)
    assert "object-fill" in img, "the spectrogram must fill its box, not cover it"
    for bad in ("object-cover", "object-contain", "object-none", "object-scale-down"):
        assert bad not in img, f"{bad} crops or letterboxes; overlays would misalign"


def test_the_locator_boxes_scale_by_the_clips_own_duration():
    """The marker carries the duration the peak was measured against. Using a
    hardcoded window instead would stretch every box on a clip of another
    length — the saved clips are 6 s while the BirdNET window is 3 s."""
    src = MODAL.read_text()
    assert "m.peak_time_s / dur" in src
    assert "m.duration_s" in src


def test_the_modal_ships_the_hover_elements():
    src = MODAL.read_text()
    for el in ("spec-modal-grid", "spec-modal-crosshair", "spec-modal-cursor"):
        assert f'id="{el}"' in src, el
    # The readout must not swallow clicks — click-to-seek runs on the same box.
    for el in ("spec-modal-crosshair", "spec-modal-cursor", "spec-modal-grid"):
        block = re.search(rf'id="{el}"[^>]*', src).group(0)
        assert "pointer-events-none" in block, f"{el} would block click-to-seek"


def test_the_readout_is_hidden_when_the_modal_closes():
    """Otherwise it reappears over the next clip showing the last one's numbers."""
    src = MODAL.read_text()
    close = re.search(r"dialog\.addEventListener\('close',\s*\(\) => \{([^}]*)\}", src)
    assert close and "_hideCursor()" in close.group(1)
