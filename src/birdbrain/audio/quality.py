"""Per-source audio-quality metric: how usable a cam's live audio is for
BirdNET detection.

Two reliable dimensions are measured from the already-decoded 3 s chunks
(near-free CPU; BirdNET dominates), EMA-smoothed over ~5 min:
  * **level** — RMS dBFS: dead/quiet mics (Namib, Okaukuejo) score low.
  * **availability** — fraction of non-silent chunks; plus a clipping penalty.

The third dimension — whether the (adequate-level) audio is actually *usable*
vs broadband-masked — is NOT reliably separable by simple DSP: live
calibration showed a loud busy soundscape (Olifants) and loud surf (Stony
Point) are near-identical in flatness/SNR/dynamics. So "is this producing?" is
taken from **detection yield** instead (recent detections per hour), folded in
at snapshot time. Loud + near-zero detections ⇒ "noise-masked".

``flatness`` is still computed as a diagnostic only (not load-bearing).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_SILENCE_DBFS = -72.0    # below this a mic is functionally dead for BirdNET
_LEVEL_OK_DBFS = -60.0   # at/above this, level is adequate
_CLIP_FRAC_HI = 0.005    # >0.5% railed samples = overdriven
# Level adequacy ramp (dBFS) and detection-yield saturation (per hour).
_LEVEL_LO_DBFS = -72.0
_LEVEL_HI_DBFS = -50.0
_YIELD_SAT_PER_H = 3.0   # >= this many detections/hour ⇒ yield_score 1.0

# EMA half-life ~5 min at 3 s chunks (100 chunks); first DB flush after ~1 min.
EMA_ALPHA = 1.0 - 0.5 ** (1.0 / 100.0)
MIN_CHUNKS = 20

# Frequency-band analysis: STFT bin frequencies (1025 bins, 0..24 kHz @48k),
# and the dB margin below the in-band level that still counts as "carries
# energy". A sharp upper edge well below the ~16 kHz YouTube-AAC ceiling means
# the source (mic/encoder) is band-limited — loses high-frequency bird calls.
_N_FFT = 2048
_FREQS = np.fft.rfftfreq(_N_FFT, 1.0 / 48_000)
_BAND_MARGIN_DB = 25.0
# Periodic (not symmetric) Hann — the window an FFT wants, and what librosa's
# 'hann' default resolves to. np.hanning is symmetric, so drop its last point.
_WINDOW = np.hanning(_N_FFT + 1)[:-1].astype(np.float32)


def _power_spectrogram(y: np.ndarray, n_fft: int = _N_FFT) -> np.ndarray:
    """|STFT|^2 with hop == n_fft (no overlap), matching what this module used
    to get from ``librosa.stft(y, n_fft=2048, hop_length=2048)``.

    Hand-rolled so that a capture unit need not import librosa at all. That one
    import is the single largest thing on a unit's memory budget: it pulls
    scipy, numba, llvmlite and soxr, measured at **+189 MB** on a 415 MB Pi Zero
    2 W — for two calls, both of which numpy does directly. Verified equivalent
    to the librosa version to 5.2e-06 (float32 rounding), so the quality scores
    and band edges are numerically continuous across this change.

    Mirrors librosa's defaults: centred frames (zero-padded by n_fft // 2, its
    ``pad_mode='constant'``) and a periodic Hann window.
    """
    hop = n_fft
    yp = np.pad(np.asarray(y, dtype=np.float32), n_fft // 2, mode="constant")
    n_frames = 1 + (len(yp) - n_fft) // hop
    if n_frames < 1:
        return np.zeros((n_fft // 2 + 1, 0), dtype=np.float32)
    # as_strided gives a read-only view over the frames; copy so the windowing
    # multiply below cannot write through overlapping strides.
    frames = np.lib.stride_tricks.as_strided(
        yp, shape=(n_frames, n_fft), strides=(yp.strides[0] * hop, yp.strides[0])
    ).copy()
    spec = np.fft.rfft(frames * _WINDOW, axis=1).T
    return np.abs(spec) ** 2


def _band_edges(psd: np.ndarray) -> tuple[float | None, float | None]:
    """(low_hz, high_hz) where the averaged power spectrum carries energy,
    relative to the in-band (0.5–4 kHz) level. None when indeterminate."""
    psd_db = 10.0 * np.log10(psd + 1e-12)
    inband = (_FREQS >= 500) & (_FREQS <= 4000)
    ref = float(np.median(psd_db[inband]))
    keep = _FREQS[psd_db >= ref - _BAND_MARGIN_DB]
    if keep.size == 0:
        return None, None
    above = keep[keep >= 40.0]  # skip DC/sub-bass rumble for the low edge
    low = float(above.min()) if above.size else float(keep.min())
    return low, float(keep.max())


# Band penalty: feeds whose upper edge is well below the codec ceiling lose the
# upper bird band. Ramp from the floor at 4 kHz up to 1.0 at 10 kHz; unknown
# (too quiet to measure) or >=10 kHz is unpenalised.
_BAND_FULL_HZ = 10_000.0
_BAND_NARROW_HZ = 4_000.0
_BAND_PENALTY_FLOOR = 0.6


def _band_penalty(band_hi: float | None) -> float:
    if band_hi is None or band_hi >= _BAND_FULL_HZ:
        return 1.0
    frac = (band_hi - _BAND_NARROW_HZ) / (_BAND_FULL_HZ - _BAND_NARROW_HZ)
    return float(np.clip(
        _BAND_PENALTY_FLOOR + (1.0 - _BAND_PENALTY_FLOOR) * frac,
        _BAND_PENALTY_FLOOR, 1.0))


def _dbfs(x: float) -> float:
    return 20.0 * float(np.log10(x + 1e-12))


def chunk_features(y: np.ndarray) -> dict:
    """Cheap per-chunk acoustic features from mono float32 [-1, 1] samples."""
    y = np.asarray(y, dtype=np.float32)
    if y.size == 0:
        return {"rms_dbfs": -120.0, "peak_dbfs": -120.0, "crest_db": 0.0,
                "clip_frac": 0.0, "flatness": 1.0, "psd": None}
    rms_dbfs = _dbfs(float(np.sqrt(np.mean(y * y) + 1e-12)))
    peak_dbfs = _dbfs(float(np.max(np.abs(y))))
    clip_frac = float(np.mean(np.abs(y) >= 0.999))
    psd = None
    flatness = 1.0
    try:
        # One STFT → both the spectral flatness (diagnostic) and the mean power
        # spectrum (for the frequency-band edges).
        power = _power_spectrogram(y)
        psd = power.mean(axis=1)
        gm = np.exp(np.mean(np.log(power + 1e-12), axis=0))
        am = np.mean(power, axis=0) + 1e-12
        flatness = float(np.median(gm / am))
    except Exception:
        pass
    return {
        "rms_dbfs": rms_dbfs, "peak_dbfs": peak_dbfs,
        "crest_db": peak_dbfs - rms_dbfs, "clip_frac": clip_frac,
        "flatness": flatness, "psd": psd,
    }


def classify_chunk(f: dict) -> str:
    """Single label per chunk (level/clip based; flatness is unreliable on
    real audio so it isn't used here)."""
    if f["rms_dbfs"] < _SILENCE_DBFS:
        return "silent"
    if f["clip_frac"] > _CLIP_FRAC_HI:
        return "clipping"
    if f["rms_dbfs"] < _LEVEL_OK_DBFS:
        return "quiet"
    return "good"


@dataclass
class _Ema:
    alpha: float
    value: float | None = None

    def update(self, x: float) -> None:
        self.value = (
            x if self.value is None
            else (1.0 - self.alpha) * self.value + self.alpha * x
        )


class QualityAccumulator:
    """Rolling EMA of per-chunk acoustic features. ``snapshot(detections_per_h)``
    folds in detection yield to produce the 0-100 score + issue label."""

    def __init__(self, alpha: float = EMA_ALPHA) -> None:
        self._alpha = alpha
        self._rms = _Ema(alpha)
        self._clip = _Ema(alpha)
        self._flat = _Ema(alpha)
        self._silent = _Ema(alpha)
        self._good = _Ema(alpha)
        self._psd: np.ndarray | None = None   # EMA of the mean power spectrum
        self.chunk_count = 0

    def update(self, f: dict) -> None:
        label = classify_chunk(f)
        self._rms.update(f["rms_dbfs"])
        self._clip.update(f["clip_frac"])
        self._flat.update(f["flatness"])
        self._silent.update(1.0 if label == "silent" else 0.0)
        self._good.update(1.0 if label == "good" else 0.0)
        psd = f.get("psd")
        if psd is not None:
            self._psd = (
                psd if self._psd is None
                else (1.0 - self._alpha) * self._psd + self._alpha * psd
            )
        self.chunk_count += 1

    @property
    def ready(self) -> bool:
        return self.chunk_count >= MIN_CHUNKS

    def snapshot(self, detections_per_h: float = 0.0) -> dict | None:
        """Current metric, or None before any chunk was seen.
        ``detections_per_h`` is the recent detection rate (the masking signal)."""
        if self._rms.value is None:
            return None
        ema_rms = self._rms.value
        frac_silent = self._silent.value or 0.0
        ema_clip = self._clip.value or 0.0

        level_score = float(np.clip(
            (ema_rms - _LEVEL_LO_DBFS) / (_LEVEL_HI_DBFS - _LEVEL_LO_DBFS), 0.0, 1.0))
        # Frequency band — only meaningful when there's actual content to
        # measure (skip when the feed is silent / near-dead).
        band_lo = band_hi = None
        if self._psd is not None and frac_silent <= 0.6 and level_score >= 0.15:
            band_lo, band_hi = _band_edges(self._psd)

        avail_score = 1.0 - frac_silent
        yield_score = float(np.clip(detections_per_h / _YIELD_SAT_PER_H, 0.0, 1.0))
        clip_penalty = float(np.clip(1.0 - ema_clip / 0.05, 0.3, 1.0))
        yield_gate = float(np.clip(yield_score / 0.30, 0.0, 1.0))
        # Band penalty: a feed cut well below the codec ceiling loses the upper
        # bird band (sunbirds, white-eyes, high cisticolas). Ramps 4 kHz→0.6 up
        # to 10 kHz→1.0; full-range/codec-limited feeds (>=10 kHz) are unpenalised.
        band_penalty = _band_penalty(band_hi)
        composite = (
            (0.35 * level_score + 0.25 * avail_score + 0.40 * yield_score)
            * clip_penalty * (0.4 + 0.6 * yield_gate) * band_penalty
        )
        score = int(round(100.0 * composite))

        if clip_penalty < 0.8:
            issue = "clipping"
        elif frac_silent > 0.5:
            issue = "mostly silent"
        elif level_score < 0.25:
            issue = "too quiet"
        elif yield_score < 0.30:
            issue = "noise-masked"
        elif band_hi is not None and band_hi < 9000:
            issue = "band-limited"
        elif score >= 70:
            issue = "good"
        else:
            issue = "marginal"

        return {
            "score": score,
            "level_score": round(level_score, 3),
            "avail_score": round(avail_score, 3),
            # structure_score column repurposed to carry the yield-derived score
            "structure_score": round(yield_score, 3),
            "level_dbfs": round(ema_rms, 1),
            "silence_fraction": round(frac_silent, 3),
            "clip_fraction": round(ema_clip, 4),
            "flatness": round(self._flat.value or 0.0, 3),
            "fraction_good": round(self._good.value or 0.0, 3),
            "issue_label": issue,
            "band_hz_low": int(round(band_lo)) if band_lo else None,
            "band_hz_high": int(round(band_hi)) if band_hi else None,
        }
