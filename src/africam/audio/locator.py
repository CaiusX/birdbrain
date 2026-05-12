"""Approximate time-localization of a species's call within a saved clip.

BirdNET classifies the whole 3 s chunk as one unit, so it doesn't tell us
*where* in the clip a given species called. We approximate this by looking
at the STFT energy inside the species's typical frequency band and picking
the time bin with the highest energy.

This is a heuristic, not a detector — it tells you the *most likely* time
window for a call assuming it's actually present (which BirdNET has already
asserted). It works well when species occupy distinct frequency bands and
breaks down when calls overlap in frequency (e.g. two cisticolas, or two
sandpipers in the 3–6 kHz band).
"""

from __future__ import annotations

from pathlib import Path

import librosa
import numpy as np


# Approximate dominant frequency band (Hz) for each species we regularly see.
# Bands are deliberately wider than the call's fundamental to capture
# harmonics, and skew low when in doubt (most bird calls have strong
# low-frequency components even when their "pure tone" is higher).
#
# Missing species fall back to DEFAULT_BAND. When you spot a misplacement on
# the spectrogram, narrow or shift the band here and the modal updates on
# the next clip open.
SPECIES_FREQ_BANDS: dict[str, tuple[float, float]] = {
    # Geese / ibises / hornbills / heavy birds
    "Alopochen aegyptiaca":      (300, 1500),   # Egyptian Goose
    "Bostrychia hagedash":       (600, 3500),   # Hadada Ibis (harsh, broad)
    "Tockus rufirostris":        (400, 2500),   # Southern Red-billed Hornbill
    "Phoeniculus purpureus":     (500, 2000),   # Green Woodhoopoe
    "Nycticorax nycticorax":     (300, 1200),   # Black-crowned Night-Heron
    "Pternistis adspersus":      (400, 1800),   # Red-billed Francolin
    "Pternistis natalensis":     (400, 1800),   # Natal Francolin

    # Doves / pigeons
    "Streptopelia capicola":     (300, 800),    # Ring-necked Dove
    "Streptopelia semitorquata": (300, 800),    # Red-eyed Dove
    "Turtur chalcospilos":       (500, 1500),   # Emerald-spotted Wood-Dove
    "Columba guinea":            (300, 800),    # Speckled Pigeon

    # Owls / nightjars
    "Otus senegalensis":         (700, 1500),   # African Scops-Owl
    "Caprimulgus pectoralis":    (1500, 3500),  # Fiery-necked Nightjar

    # Bushshrikes / orioles / boubous (mid-range whistles)
    "Pycnonotus barbatus":       (1800, 5000),  # Common Bulbul
    "Andropadus importunus":     (1500, 4500),  # Sombre Greenbul
    "Oriolus larvatus":          (1500, 4000),  # African Black-headed Oriole
    "Laniarius ferrugineus":     (1000, 3000),  # Southern Boubou
    "Telophorus sulfureopectus": (1500, 4000),  # Sulphur-breasted Bushshrike
    "Telophorus viridis":        (1500, 4000),  # Four-colored Bushshrike
    "Telophorus olivaceus":      (1500, 4000),  # Olive Bushshrike
    "Malaconotus blanchoti":     (800, 2500),   # Gray-headed Bushshrike
    "Nilaus afer":               (2000, 5000),  # Brubru
    "Tauraco porphyreolophus":   (800, 2500),   # Purple-crested Turaco
    "Apaloderma narina":         (300, 1200),   # Narina Trogon (hoots low)
    "Halcyon senegalensis":      (2000, 5000),  # Woodland Kingfisher

    # Wagtails
    "Motacilla aguimp":          (2500, 6000),  # African Pied Wagtail
    "Motacilla flava":           (3000, 7000),  # Western Yellow Wagtail
    "Motacilla capensis":        (2500, 6000),  # Cape Wagtail

    # Shorebirds / waders
    "Vanellus armatus":          (2000, 5000),  # Blacksmith Lapwing
    "Charadrius tricollaris":    (2500, 6000),  # Three-banded Plover
    "Calidris minuta":           (3000, 7000),  # Little Stint
    "Actitis hypoleucos":        (3000, 7000),  # Common Sandpiper
    "Tringa glareola":           (3000, 7000),  # Wood Sandpiper

    # Cisticolas / robin-chats / small passerines
    "Cisticola chiniana":        (4000, 8000),  # Rattling Cisticola
    "Cisticola erythrops":       (3500, 7500),  # Red-faced Cisticola
    "Cercotrichas leucophrys":   (2500, 6000),  # Red-backed Scrub-Robin
    "Cossypha natalensis":       (1500, 5000),  # Red-capped Robin-Chat
    "Cossypha dichroa":          (1500, 5000),  # Chorister Robin-Chat
    "Fraseria plumbea":          (2500, 6000),  # Gray Tit-Flycatcher

    # Estrildid finches (high seeps)
    "Uraeginthus angolensis":    (4000, 8000),  # Southern Cordonbleu
    "Uraeginthus bengalus":      (4000, 8000),  # Red-cheeked Cordonbleu

    # Swallows / martins / swifts
    "Riparia paludicola":        (2500, 6000),  # Plain Martin
    "Cecropis abyssinica":       (2500, 6000),  # Lesser Striped Swallow
    "Apus caffer":               (3000, 7000),  # White-rumped Swift

    # Raptors / woodpeckers
    "Circus aeruginosus":        (1000, 3500),  # Eurasian Marsh-Harrier
    "Chloropicus namaquus":      (500, 2500),   # Bearded Woodpecker
}

# Used when the species isn't in the lookup. Wide band so the heuristic
# still finds *something* but the marker should be treated as a rough hint.
DEFAULT_BAND: tuple[float, float] = (500.0, 8000.0)


def locate_species_in_clip(
    wav_path: Path,
    species: list[tuple[str, str]],
    *,
    sample_rate: int = 24_000,
    n_fft: int = 1024,
    hop_length: int = 256,
) -> list[dict]:
    """Find the peak-energy time within each species's frequency band.

    Returns one dict per input species (skipping entries whose band falls
    outside the audible spectrogram range). Each dict contains ``peak_time_s``
    (where to draw the marker), ``freq_lo_hz``/``freq_hi_hz`` (the band the
    peak was searched in), ``snr_vs_median`` (how prominent the peak is —
    treat low SNR markers with skepticism), and an ``is_fallback`` flag
    signaling the species used DEFAULT_BAND.
    """
    y, sr = librosa.load(str(wav_path), sr=sample_rate, mono=True)
    duration_s = float(len(y)) / float(sr)
    if duration_s <= 0:
        return []

    spec = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=hop_length)) ** 2
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    times = librosa.frames_to_time(np.arange(spec.shape[1]), sr=sr, hop_length=hop_length)

    out: list[dict] = []
    for sci, common in species:
        band = SPECIES_FREQ_BANDS.get(sci)
        is_fallback = band is None
        lo, hi = band or DEFAULT_BAND
        mask = (freqs >= lo) & (freqs <= hi)
        if not mask.any():
            continue
        band_energy = spec[mask, :].sum(axis=0)
        peak_val = float(band_energy.max())
        if peak_val <= 0:
            continue
        peak_idx = int(band_energy.argmax())
        median = float(np.median(band_energy)) or 1e-9
        out.append(
            {
                "scientific_name": sci,
                "common_name": common,
                "peak_time_s": round(float(times[peak_idx]), 3),
                "duration_s": round(duration_s, 3),
                "freq_lo_hz": float(lo),
                "freq_hi_hz": float(hi),
                "snr_vs_median": round(peak_val / median, 2),
                "is_fallback": is_fallback,
            }
        )
    return out
