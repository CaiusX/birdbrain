"""Audio-quality metric: numerical continuity, and the import budget.

The second half matters as much as the first. ``chunk_features`` runs on every
3 s chunk on every source, including a 415 MB Pi Zero 2 W field unit. It used to
reach for ``librosa.stft``, which drags in scipy, numba, llvmlite and soxr —
measured at **+122 MB and +367 modules** on top of the (mandatory) detector. The
hand-rolled power spectrogram exists to avoid exactly that, so a test has to
hold the line or the import creeps back in unnoticed.
"""
from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest

from birdbrain.audio.quality import (
    _FREQS,
    _N_FFT,
    _power_spectrogram,
    chunk_features,
    classify_chunk,
)


def _signal(seconds: float = 3.0, sr: int = 48_000) -> np.ndarray:
    """Deterministic broadband noise + a tone, so the spectrum has structure."""
    rng = np.random.default_rng(0)
    t = np.arange(int(sr * seconds)) / sr
    y = 0.05 * rng.standard_normal(t.size) + 0.2 * np.sin(2 * np.pi * 2500 * t)
    return y.astype(np.float32)


# --- numerical continuity with the librosa version we replaced ---------------

def test_power_spectrogram_matches_librosa():
    """The swap must not move the quality scores or the band edges. librosa is
    still a central dependency, so central can prove the equivalence; a unit
    that has dropped it simply skips this test."""
    librosa = pytest.importorskip("librosa")
    y = _signal()
    ref = np.abs(librosa.stft(y, n_fft=_N_FFT, hop_length=_N_FFT)) ** 2
    mine = _power_spectrogram(y)

    assert mine.shape == ref.shape
    # float32 rounding only — measured 5.2e-06 on the complex STFT.
    assert np.allclose(mine, ref, rtol=1e-4, atol=1e-4 * ref.max())


def test_bin_frequencies_match_librosa():
    librosa = pytest.importorskip("librosa")
    assert np.allclose(_FREQS, librosa.fft_frequencies(sr=48_000, n_fft=_N_FFT))


def test_power_spectrogram_shape_and_dtype():
    y = _signal()
    p = _power_spectrogram(y)
    assert p.shape[0] == _N_FFT // 2 + 1 == len(_FREQS)
    assert p.shape[1] == 1 + (len(y) + _N_FFT - _N_FFT) // _N_FFT  # centred framing
    assert np.all(p >= 0.0)


def test_power_spectrogram_survives_a_short_chunk():
    """A truncated final chunk must not raise — chunk_features swallows errors,
    so a crash here would silently zero the metric instead of being loud."""
    assert _power_spectrogram(np.zeros(10, dtype=np.float32)).shape[0] == _N_FFT // 2 + 1


def test_power_spectrogram_finds_the_tone():
    y = _signal()
    psd = _power_spectrogram(y).mean(axis=1)
    assert abs(_FREQS[int(np.argmax(psd))] - 2500) < 50


# --- the feature dict the pipeline consumes ---------------------------------

def test_chunk_features_returns_the_expected_keys():
    f = chunk_features(_signal())
    assert set(f) == {
        "rms_dbfs", "peak_dbfs", "crest_db", "clip_frac", "flatness", "psd",
    }
    assert -80.0 < f["rms_dbfs"] < 0.0
    assert 0.0 <= f["clip_frac"] <= 1.0
    assert 0.0 <= f["flatness"] <= 1.0
    assert f["psd"] is not None and f["psd"].shape == (_N_FFT // 2 + 1,)
    assert classify_chunk(f)


def test_chunk_features_handles_empty_and_silent_audio():
    empty = chunk_features(np.zeros(0, dtype=np.float32))
    assert empty["rms_dbfs"] == -120.0 and empty["psd"] is None
    silent = chunk_features(np.zeros(48_000, dtype=np.float32))
    assert silent["rms_dbfs"] < -100.0


# --- the import budget ------------------------------------------------------

# Run in a subprocess: sys.modules is process-global, so by the time this file
# runs another test may already have imported librosa for its own reasons. Only
# a clean interpreter can answer "does the quality path pull this in".
_IMPORT_PROBE = """
import sys
import numpy as np
from birdbrain.audio.quality import chunk_features
chunk_features(np.zeros(48000, dtype=np.float32))
heavy = [m for m in ("librosa", "scipy", "numba", "llvmlite", "soxr", "resampy")
         if m in sys.modules]
print(",".join(heavy))
"""


def test_quality_path_imports_nothing_heavy():
    """Regression guard for ~118 MB on a 415 MB unit.

    If this fails, something in the quality path started importing librosa (or
    friends) again. Use numpy directly, or move the import behind the call that
    actually needs it — do not relax this test.
    """
    out = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True, text=True, timeout=120, check=True,
    )
    leaked = out.stdout.strip()
    assert not leaked, f"quality path pulled heavy imports: {leaked}"
