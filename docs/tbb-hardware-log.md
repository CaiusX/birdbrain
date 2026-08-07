# TBB hardware / capture-config change log

Dated record of physical and capture-path changes to TBB units, so that later
performance comparisons know which windows are comparable.

**Why this file exists:** the units feed long-running A/B comparisons against
central (`tbb-test` vs `JHB - Hyde Park`, co-located in Hyde Park, Johannesburg).
Swapping a microphone or changing gain silently invalidates any window that
spans the change — and the detection database records none of it, because the
capture path lives in host config (`~/.asoundrc`, `.env`, ALSA mixer state) that
is deliberately not in git. Anything that changes what the mic hears goes here,
with a UTC timestamp to anchor analysis on.

Analysis rule: **never pool detections across an anchor below.**

---

## 2026-07-30 09:27:40 UTC — tbb-test: Codec Zero HAT mic → USB PnP mic

Anchor for the **matched-mic** Pi Zero vs Pi 5 comparison.

| | Before | After |
|---|---|---|
| Device | Codec Zero HAT, da7213 (`card 0` "Zero") | USB PnP Audio Device, JMTek `0c76:153f` (`card 2` "Device") |
| Native format | stereo-only, 48 kHz | mono, 48 kHz (no downmix) |
| ALSA alias | `micshared` → `dsnoopmic` → `hw:0,0` | `micshared` → `dsnoopmic_usb` → `hw:CARD=Device` |
| Measured level | (ran hot — see below) | **-36.8 dBFS** peak-sample RMS over 3 s |

**Why:** the USB mic is the same `0c76` USB-PnP family as Hyde Park's Boya, so
the microphone stops being a variable and the comparison approximates a pure
Pi Zero vs Pi 5 compute test. It also retires a known confound: over the
2026-06-25 → 2026-07-30 window tbb-test appeared to out-detect the Pi 5 on
volume, but that was almost entirely **one loud species over-counted** (Hadada
Ibis 478 vs 170, 2.8×) because the HAT mic ran hotter. Strip that species and
the Pi 5 led.

> ⚠️ **The Hadada reasoning in the paragraph above is wrong — corrected
> 2026-08-07 (see the entry below).** The gap was central's 0.9 species floor
> being applied to Hyde Park and not to the unit, not mic gain. Applying the
> floor to both sides inverts the ratio (2.8× → 0.56×). The mic swap itself
> stands and remains a valid anchor; only the justification was mismeasured.

**Gain deliberately unchanged.** The USB mic measured -36.8 dBFS against Hyde
Park's -37.8 dBFS (quality score 100, "good") — within 1 dB. The ALSA capture
control was left at its default `Mic 408/496` (+25.5 dB). Raising it toward the
480+/496 maximum was considered and rejected: it would have recreated the very
level mismatch this change removes.

**Cards are referenced by name, not index.** USB card numbering shifts across
reboots, so `hw:2,0` is not stable; `hw:CARD=Device` is. The previous HAT path
is kept as `dsnoopmic_hat` → `hw:CARD=Zero` for fallback, and the pre-change
file is backed up on the unit at `~/.asoundrc.bak-20260730`.

**`.env` was not touched.** `BIRDBRAIN_TBB_MIC_DEVICE=micshared` is the
indirection point; which physical mic that resolves to is decided in
`~/.asoundrc` alone, so a swap is a one-line change and needs no service config
edit.

Verify the active device with:

```sh
fuser -v /dev/snd/pcmC*        # ffmpeg should hold pcmC2D0c (USB), not pcmC0D0c (HAT)
curl -fsS http://127.0.0.1:8080/healthz
```

Note that `/healthz` reports `worker_state: stopped` for the first ~1–2 minutes
after a restart while BirdNET loads on a Pi Zero. That is not a failure; wait
for `listening: true` before concluding anything.

---

## 2026-08-07 07:30 UTC — central applies its species floors at ingest

Anchor for **unit-vs-central detection counts**. Not a capture-path change —
nothing about what the mic hears changed. What changed is which detections
central *stores* from a unit, so counts before and after this timestamp are not
poolable for any species carrying a floor or a suppression.

**The defect.** Central's pipeline applies two admin-set rules post-analyze:
global per-species floors (`species_notes.min_confidence`) and per-source
suppressions (`species_suppressions`, including the all-sites `*` rule). A unit
runs its own pipeline against its own config and never sees either, and
`/ingest/detections` did not apply them on arrival. So a species floored at 0.9
was filtered at every locally-analysed source and stored unfiltered from every
unit. Unit rows entered the same table held to a laxer standard than the rest.

Two floors were live throughout: Hadada Ibis and Egyptian Goose, both at 0.9.

**Measured.** Hadada Ibis, tbb-test vs Hyde Park:

| Window | raw (as previously logged) | 0.9 floor applied to both |
|---|---|---|
| 2026-07-23 → 07-30 (pre-swap) | 662 / 234 = **2.83×** | 131 / 234 = **0.56×** |
| 2026-06-25 → 07-30 (pre-swap) | 3819 / 1533 = **2.49×** | 959 / 1533 = **0.63×** |
| since the mic swap | 1028 / 270 = **3.81×** | 371 / 270 = **1.37×** |

Hyde Park had **zero** sub-0.9 Hadada rows in every window — the signature of a
filter running on one side only. The direction reverses under correction: on
high-confidence Hadada the Pi 5 led.

The earlier entry's "2.8×" ratio reproduces on the 2026-07-23 → 07-30 window,
but its absolute counts (478 / 170) match no window tried here; treat those two
numbers as unreliable and the ratio as the real claim.

**What the corrected comparison says.** Restricted to the 122 hours both were
listening since the mic swap, same 0.5 floor, same 0.9 species floors applied to
both sides: tbb-test 8,394 detections / 56 species vs Hyde Park 6,120 / 47 —
**1.37× detections, 1.19× species**. Robust rather than a single-species
artifact: median per-species ratio 1.40, tbb ahead on 16 of 17 species with ≥20
combined detections, 1.47× with the dominant Cape Robin-Chat (65–67% of both
totals) removed, and a flat 1.2–1.9× lead across every hour of the day.

**Do not read that as a sensor verdict.** The two mics sit ~550 m apart
(-26.124 vs -26.129) in different micro-locations, so this is a site comparison
as much as a hardware one. What it does establish is that the Pi Zero is not
compute-limited: both analyse identical 3.0 s windows at the same cadence, and
central's serialized detector sits at ~60% capacity (18 workers × ~100 ms /
3000 ms), so neither side is dropping chunks. Level and noise plausibly explain
the rest — tbb-test measures -36.7 dBFS vs -37.8, with spectral flatness 0.003
vs 0.009 (Hyde Park's signal is ~3× more noise-like, which masks quiet calls).

**The fix.** `ingest.ingest_batch` now applies both rules before upserting and
reports a `filtered` count in its response. The lookup is uncached and
deliberately not exception-wrapped: a failed read rejects the batch rather than
falling back to storing unfiltered rows, matching the rule `check_schema`
already follows — a stalled backlog is visible on the unit's own page, wrongly
admitted rows are not. Units advance their sync cursor on any 2xx and never
inspect the counts, so filtering here cannot cause a resend loop. Covered by
`test_ingest_applies_central_species_floor` and
`test_ingest_applies_species_suppressions`.

Historical rows are left as-is. Any analysis spanning this timestamp must apply
the floors in the query, as the table above does.
