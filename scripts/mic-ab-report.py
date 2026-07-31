#!/usr/bin/env python3
"""Co-located mic A/B: tbb-test (Pi Zero 2 W) vs JHB - Hyde Park (Pi 5).

Both mics sit in the same place in Hyde Park, Johannesburg. The comparison is
only meaningful inside a window where nothing about the capture path changed, so
the anchor defaults to the most recent hardware change rather than to all time.

ANCHORS (see docs/tbb-hardware-log.md on the tbb branch):
  2026-06-25 12:01 UTC  co-location began     — different mics
  2026-07-30 09:27:40   tbb-test mic swap     — MATCHED mics (default)

Never pool across an anchor. Before the swap tbb-test ran a Codec Zero HAT mic
that sat hotter than the Pi 5's Boya; its apparent detection lead was almost
entirely one loud species (Hadada Ibis, 478 vs 170) rather than better hearing.
The swap put both units on the same 0c76 USB-PnP family at matched measured
level (-36.8 vs -37.8 dBFS), so from that anchor this reads much closer to a
pure Pi Zero vs Pi 5 compute comparison.

Hadada Ibis is reported separately on purpose: it is the single cleanest tell for
a level mismatch, so a ratio near 1.0 is the evidence that the mics really are
matched, and a ratio still near 2-3x says something other than the mic is at
work (placement, proximity, gain chain).

Read the dawn-chorus slice, not just the totals. A single chorus misled this
comparison badly once already (2026-06-26 showed tbb winning 2.6x; four days of
data flattened it to parity), so treat anything under ~3 days as provisional.

    scripts/mic-ab-report.py                      # since the mic-swap anchor
    scripts/mic-ab-report.py --since 2026-06-25T12:01
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import UTC, datetime

A = "tbb-test"
B = "JHB - Hyde Park"
MIC_SWAP = "2026-07-30 09:27:40"
MARKER = "Hadada Ibis"


def one(c, sql, *p):
    return c.execute(sql, p).fetchone()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=MIC_SWAP, help="UTC anchor (default: mic swap)")
    ap.add_argument("--db", default="data/birdbrain.sqlite")
    args = ap.parse_args()
    since = args.since.replace("T", " ")

    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    hrs = (datetime.now(UTC).replace(tzinfo=None) - datetime.fromisoformat(since)).total_seconds() / 3600
    tag = "MATCHED MICS" if since.startswith("2026-07-30") else "different mics"
    print(f"anchor {since} UTC ({tag}) — {hrs:.1f} h / {hrs/24:.1f} days of data")
    if hrs < 72:
        print("  ⚠  under 3 days: provisional. One chorus already produced a 2.6x")
        print("     artifact here that four days of data erased.")

    print(f"\n{'source':22} {'dets':>7} {'>=0.7':>7} {'spp':>5} {'mean conf':>10}")
    tot = {}
    for s in (A, B):
        r = one(c, """SELECT count(*), COALESCE(sum(confidence>=0.7),0),
                             count(DISTINCT scientific_name), COALESCE(avg(confidence),0)
                        FROM detections WHERE source_name=? AND started_at>=?""", s, since)
        tot[s] = r
        print(f"{s:22} {r[0]:7} {r[1]:7} {r[2]:5} {r[3]:10.3f}")
    if tot[B][0]:
        print(f"{'ratio (tbb/JHB)':22} {tot[A][0]/tot[B][0]:7.2f} "
              f"{(tot[A][1]/tot[B][1] if tot[B][1] else 0):7.2f}")

    print(f"\n--- {MARKER}: level-mismatch marker (was 478 vs 170 = 2.8x pre-swap) ---")
    m = {s: one(c, """SELECT count(*) FROM detections
                        WHERE source_name=? AND started_at>=? AND common_name=?""",
                s, since, MARKER)[0] for s in (A, B)}
    print(f"  {A}: {m[A]}   {B}: {m[B]}" +
          (f"   ratio {m[A]/m[B]:.2f}" if m[B] else ""))
    if m[B] and m[A] / m[B] < 1.35:
        print("  -> near parity: consistent with the mics genuinely being matched.")
    elif m[B]:
        print("  -> still skewed: if this holds over days, the cause is NOT mic")
        print("     sensitivity — look at placement, proximity, or the gain chain.")

    print("\n--- dawn chorus only (05:00-08:00 UTC), per day ---")
    print(f"{'date':12} {'tbb':>6} {'JHB':>6}")
    for (d,) in c.execute("""SELECT DISTINCT substr(started_at,1,10) FROM detections
                              WHERE started_at>=? AND source_name IN (?,?)
                                AND substr(started_at,12,2) BETWEEN '05' AND '07'
                              ORDER BY 1""", (since, A, B)):
        row = [one(c, """SELECT count(*) FROM detections WHERE source_name=?
                           AND substr(started_at,1,10)=?
                           AND substr(started_at,12,2) BETWEEN '05' AND '07'""", s, d)[0]
               for s in (A, B)]
        print(f"{d:12} {row[0]:6} {row[1]:6}")

    print("\n--- top shared species ---")
    print(f"{'species':30} {'tbb':>6} {'JHB':>6}")
    for (sp,) in c.execute("""SELECT common_name FROM detections
                               WHERE started_at>=? AND source_name IN (?,?)
                               GROUP BY common_name ORDER BY count(*) DESC LIMIT 10""",
                           (since, A, B)):
        row = [one(c, """SELECT count(*) FROM detections WHERE source_name=?
                           AND started_at>=? AND common_name=?""", s, since, sp)[0]
               for s in (A, B)]
        print(f"  {sp:28} {row[0]:6} {row[1]:6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
