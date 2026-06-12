"""Per-source audio-quality metric: how usable a cam's live audio is for
BirdNET detection. Pure *acoustic* — signal level + spectral structure +
silence — independent of how many birds are actually around.

Computed from the already-decoded 3 s chunks in the pipeline (near-free CPU;
BirdNET dominates), smoothed with an EMA over ~5 min, and written to the DB for
the admin/site UI (see AudioQualityMetricRow / AudioQualitySampleRow).

It distinguishes the failure modes seen in the field:
  * dead / near-silent mic (Namib, Okaukuejo) -> "too quiet" / "mostly silent"
  * loud but broadband-masked (Stony Point surf) -> "noise-masked"
  * overdriven / clipped feed -> "clipping"
  * healthy structured audio (Tembe) -> "good"
"""
from __future__ import annotations

from dataclasses import dataclass

import librosa
import numpy as np

# Per-chunk thresholds (dBFS / spectral flatness). Starting points calibrated
# against observed sites — Tembe ~-50 (good), Namib/Okaukuejo -75..-91 (dead),
# Stony Point -34 but surf-masked. Tune against live values after first deploy.
_SILENCE_DBFS = -72.0    # below this a mic is functionally dead for BirdNET
_LEVEL_OK_DBFS = -60.0   # at/above this, level is adequate
_CLIP_FRAC_HI = 0.005    # >0.5% railed samples = overdriven
_FLAT_NOISE = 0.35       # flatness above this (while loud) = broadband masking
_FLAT_TONAL = 0.08       # structure ramp: <=this (tonal) -> 1.0
_FLAT_BROAD = 0.45       #                  >=this (broadband) -> 0.0

# EMA half-life ~5 min at 3 s chunks (100 chunks); first DB flush after ~1 min.
EMA_ALPHA = 1.0 - 0.5 ** (1.0 / 100.0)
MIN_CHUNKS = 20


def _dbfs(x: float) -> float:
    return 20.0 * float(np.log10(x + 1e-12))


def chunk_features(y: np.ndarray) -> dict:
    """Cheap per-chunk acoustic features from mono float32 [-1, 1] samples."""
    y = np.asarray(y, dtype=np.float32)
    if y.size == 0:
        return {
            "rms_dbfs": -120.0, "peak_dbfs": -120.0, "crest_db": 0.0,
            "clip_frac": 0.0, "flatness": 1.0, "structure": 0.0,
        }
    rms_dbfs = _dbfs(float(np.sqrt(np.mean(y * y) + 1e-12)))
    peak_dbfs = _dbfs(float(np.max(np.abs(y))))
    clip_frac = float(np.mean(np.abs(y) >= 0.999))
    try:
        flatness = float(
            np.median(librosa.feature.spectral_flatness(y=y, n_fft=2048, hop_length=2048))
        )
    except Exception:
        flatness = 1.0
    structure = float(
        np.clip((_FLAT_BROAD - flatness) / (_FLAT_BROAD - _FLAT_TONAL), 0.0, 1.0)
    )
    return {
        "rms_dbfs": rms_dbfs, "peak_dbfs": peak_dbfs,
        "crest_db": peak_dbfs - rms_dbfs, "clip_frac": clip_frac,
        "flatness": flatness, "structure": structure,
    }


def classify_chunk(f: dict) -> str:
    """Single label per chunk, priority-ordered (first match wins)."""
    if f["rms_dbfs"] < _SILENCE_DBFS:
        return "silent"
    # Key clipping off actually-railed samples — crest alone false-flags pure
    # tones (a sine's crest is only ~3 dB) as clipped.
    if f["clip_frac"] > _CLIP_FRAC_HI:
        return "clipping"
    if f["flatness"] > _FLAT_NOISE and f["rms_dbfs"] > _LEVEL_OK_DBFS:
        return "noise"   # loud + broadband = masked (Stony Point)
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
    """Rolling EMA of per-chunk features → a 0-100 score + issue label.

    Cold-start seeds each EMA with its first value; ``ready`` gates the first
    DB flush until ~1 min of audio has been seen so the score reflects real
    sound rather than a single chunk.
    """

    def __init__(self, alpha: float = EMA_ALPHA) -> None:
        self._rms = _Ema(alpha)
        self._clip = _Ema(alpha)
        self._flat = _Ema(alpha)
        self._struct = _Ema(alpha)
        self._silent = _Ema(alpha)
        self._good = _Ema(alpha)
        self.chunk_count = 0

    def update(self, f: dict) -> None:
        label = classify_chunk(f)
        self._rms.update(f["rms_dbfs"])
        self._clip.update(f["clip_frac"])
        self._flat.update(f["flatness"])
        self._struct.update(f["structure"])
        self._silent.update(1.0 if label == "silent" else 0.0)
        self._good.update(1.0 if label == "good" else 0.0)
        self.chunk_count += 1

    @property
    def ready(self) -> bool:
        return self.chunk_count >= MIN_CHUNKS

    def snapshot(self) -> dict | None:
        """Current aggregate metric, or None before any chunk was seen."""
        if self._rms.value is None:
            return None
        ema_rms = self._rms.value
        frac_silent = self._silent.value or 0.0
        ema_clip = self._clip.value or 0.0
        struct = self._struct.value or 0.0

        level_score = float(np.clip((ema_rms + 72.0) / 22.0, 0.0, 1.0))
        avail_score = 1.0 - frac_silent
        clip_penalty = float(np.clip(1.0 - ema_clip / 0.05, 0.3, 1.0))
        struct_gate = float(np.clip(struct / 0.30, 0.0, 1.0))
        composite = (
            (0.35 * level_score + 0.25 * avail_score + 0.40 * struct)
            * clip_penalty * (0.4 + 0.6 * struct_gate)
        )
        score = int(round(100.0 * composite))

        if clip_penalty < 0.8:
            issue = "clipping"
        elif frac_silent > 0.5:
            issue = "mostly silent"
        elif level_score < 0.25:
            issue = "too quiet"
        elif struct < 0.30:
            issue = "noise-masked"
        elif score >= 70:
            issue = "good"
        else:
            issue = "marginal"

        return {
            "score": score,
            "level_score": round(level_score, 3),
            "avail_score": round(avail_score, 3),
            "structure_score": round(struct, 3),
            "level_dbfs": round(ema_rms, 1),
            "silence_fraction": round(frac_silent, 3),
            "clip_fraction": round(ema_clip, 4),
            "flatness": round(self._flat.value or 0.0, 3),
            "fraction_good": round(self._good.value or 0.0, 3),
            "issue_label": issue,
        }
