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
