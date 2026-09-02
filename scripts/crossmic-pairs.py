#!/usr/bin/env python3
"""Does a bird's voice survive two microphones? Cross-mic pair analysis.

tbb-test and JHB - Hyde Park sit 550m apart on matched hardware and frequently
hear the same call. Each such co-detection is one individual recorded
simultaneously down two different acoustic paths — which is the only reason any
of this works, because it is a same-individual pair whose *recording
conditions differ*. Same-microphone pairs cannot do that job: an embedding
trained on them learns wind and distance and looks excellent doing it.

    scripts/crossmic-pairs.py --species 'Hadada Ibis'
    scripts/crossmic-pairs.py --species 'Cape Robin-Chat' --cache crc.npz

Results as of 2026-09-02, 24 log-spectral bands and no model at all:

    Hadada Ibis       603 pairs   AUC 0.885  (null: within 1 hour)
    Cape Robin-Chat  3156 pairs   AUC 0.718  (null: within 10 minutes)

FOUR THINGS THIS GETS WRONG IF YOU CHANGE THEM CARELESSLY.

Normalise per clip. The two mics are at different distances, so loudness and
SNR differ systematically. Un-normalised spectra separate the *microphones*
beautifully and mean nothing.

Keep the null same-species and cross-mic. Comparing matched pairs against other
species measures species; against same-mic clips it measures conditions. Both
flatter the result enormously.

Constrain the null in time. Matched pairs are simultaneous, so they share wind,
traffic and the same dawn chorus. The --near control draws the null from events
minutes away instead, holding ambient roughly constant. For Cape Robin-Chat
this moved AUC 0.769 -> 0.718; the difference was the afternoon, not the bird.

Co-detection is not ground truth. It says one acoustic event reached both mics,
not that one bird made it — Hadadas are gregarious and often call in groups.
Nothing here involves a ringed bird.

AND THE CONFOUND STILL STANDING: both mics are fixed, so a bird on a fixed
perch gives a fixed acoustic path every time, and some of the separation may be
"same place" rather than "same bird". Cape Robin-Chat is the partial answer —
it is mobile and still scores 0.718 — but settling it needs a third microphone
at a different bearing, or ringed birds.
"""
from __future__ import annotations

import argparse
import bisect
import pathlib
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import numpy as np

SR = 22050          # these species sit low; 48k buys nothing and costs time
NMEL = 24
LO_HZ, HI_HZ = 200, 8000


def decode(path: str) -> np.ndarray | None:
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR),
             "-f", "f32le", "-"],
            capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    x = np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64)
    return x if len(x) > SR // 2 else None


def features(x: np.ndarray | None) -> np.ndarray | None:
    """Level-normalised spectral shape over the loudest half-second.

    Focusing on the loudest window keeps the distance on the call rather than
    on whatever silence surrounds it, which otherwise dominates.
    """
    n, hop = 1024, 256
    if x is None or len(x) < n * 2:
        return None
    frames = np.array([x[i:i + n] for i in range(0, len(x) - n, hop)])
    power = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)) ** 2
    energy = power.sum(axis=1)
    if energy.max() <= 0:
        return None
    k = max(1, int(0.5 * SR / hop))
    lo = max(0, int(np.argmax(np.convolve(energy, np.ones(k), "same")) - k // 2))
    seg = power[lo:lo + k]
    if len(seg) == 0:
        return None
    f = np.fft.rfftfreq(n, 1 / SR)
    edges = np.geomspace(LO_HZ, HI_HZ, NMEL + 1)
    band = np.array([seg[:, (f >= edges[i]) & (f < edges[i + 1])].sum(axis=1).mean()
                     for i in range(NMEL)])
    v = np.log10(band + 1e-12)
    v = v - v.mean()                      # kill level: the mics differ in distance
    nrm = float(np.linalg.norm(v))
    return v / nrm if nrm > 0 else None


def find_pairs(db, species: str, a_src: str, b_src: str, window: float) -> list[tuple]:
    def rows(src):
        return [(datetime.fromisoformat(t), p) for t, p in db.execute(
            "select started_at, clip_path from detections where source_name=? "
            "and common_name=? and clip_path is not null order by 1", (src, species))]
    left, right = rows(a_src), rows(b_src)
    bt = [r[0] for r in right]
    out = []
    for t, pa in left:
        i = bisect.bisect_left(bt, t)
        for j in (i - 1, i):
            if 0 <= j < len(right) and abs((right[j][0] - t).total_seconds()) <= window:
                if pathlib.Path(pa).exists() and pathlib.Path(right[j][1]).exists():
                    out.append((t, pa, right[j][1]))
                break
    return out


def build(args) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cache = pathlib.Path(args.cache) if args.cache else None
    if cache and cache.exists():
        z = np.load(cache)
        print(f"  loaded {len(z['t'])} cached pairs from {cache}")
        return z["t"], z["a"], z["b"]
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    pairs = find_pairs(db, args.species, args.unit, args.other, args.window)
    print(f"  {args.species}: {len(pairs)} candidate pairs; decoding "
          f"(this is the slow part — cache it with --cache)")

    def work(p):
        _t, pa, pb = p
        fa, fb = features(decode(pa)), features(decode(pb))
        return (_t.timestamp(), fa, fb) if fa is not None and fb is not None else None

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        res = [r for r in ex.map(work, pairs) if r is not None]
    t = np.array([r[0] for r in res])
    a = np.array([r[1] for r in res])
    b = np.array([r[2] for r in res])
    if cache:
        np.savez_compressed(cache, t=t, a=a, b=b)
        print(f"  cached -> {cache}")
    return t, a, b


def report(t, feat_a, feat_b, near_s: float, samples: int) -> None:
    n = len(t)
    matched = np.linalg.norm(feat_a - feat_b, axis=1)
    print(f"\n  pairs {n}   matched mean distance {matched.mean():.4f}")
    rng = np.random.default_rng(0)
    for label, hi in (("anywhere", None), ("within 1 hour", 3600.0),
                      (f"within {int(near_s/60)} min", near_s)):
        vals = []
        for _ in range(samples):
            i, j = rng.integers(0, n, 2)
            dt = abs(t[i] - t[j])
            if i == j or dt < 120:        # <2min apart is the same bout, not a null
                continue
            if hi is not None and dt > hi:
                continue
            vals.append(np.linalg.norm(feat_a[i] - feat_b[j]))
        if len(vals) < 200:
            print(f"    null {label:16} only {len(vals)} samples — skipped")
            continue
        v = np.array(vals)
        auc = float((v[:, None] > matched[None, :]).mean())
        d = (v.mean() - matched.mean()) / np.sqrt((v.var() + matched.var()) / 2)
        print(f"    null {label:16} n={len(v):6}  mean {v.mean():.4f}  "
              f"AUC {auc:.3f}  d' {d:+.2f}")
    print("\n  AUC 0.50 = no individual signal. Read the tightest null, not the first:")
    print("  the loose one includes shared ambient and flatters the result.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--species", required=True)
    ap.add_argument("--unit", default="tbb-test")
    ap.add_argument("--other", default="JHB - Hyde Park")
    ap.add_argument("--window", type=float, default=6.0,
                    help="co-detection window, seconds (550m is ~1.6s of flight)")
    ap.add_argument("--near", type=float, default=600.0,
                    help="tight null: draw unrelated events within this many seconds")
    ap.add_argument("--samples", type=int, default=60000)
    ap.add_argument("--workers", type=int, default=3, help="decode threads (of 4 cores)")
    ap.add_argument("--cache", help="npz to read/write extracted features")
    ap.add_argument("--db", default="data/birdbrain.sqlite")
    args = ap.parse_args()
    t, feat_a, feat_b = build(args)
    if len(t) < 30:
        print("  too few usable pairs to conclude anything")
        return 1
    report(t, feat_a, feat_b, args.near, args.samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
