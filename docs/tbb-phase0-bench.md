# TBB Phase 0 — Pi Zero 2 W bench bring-up (co-work runbook)

Goal: get one Raspberry Pi Zero 2 W running `birdbrain tbb-listen` off a USB mic so
we can record the **real** per-chunk inference ms and peak RAM that the Phase 0
acceptance gate needs (the dev sandbox is x86 and can't produce these).

This is a *bench* runbook, not the golden image — it folds into
`docs/tbb-provisioning.md` in Phase 1. Work through it with Claude; the steps
that need a judgement call are marked **⟶ ping Claude**.

---

## 0. What you need (BOM)

| Item | Notes |
| --- | --- |
| Raspberry Pi Zero 2 W | The quad-A53, 512 MB board. (Not the original Zero.) |
| microSD card, ≥16 GB, A1/Class-10 | 16 GB gives room for the (possibly unused) TensorFlow wheel. |
| USB microphone | A cheap USB lavalier or "mini USB mic" dongle. Must be **USB** (the Pi has no analog mic in). |
| micro-USB **OTG** adapter | micro-USB male → USB-A female. Plugs the mic into the Pi's **data** port. |
| 5 V power supply, micro-USB, ≥2.5 A | Into the **PWR** port. Underpowered supplies cause random reboots. |
| microSD reader on your laptop | For flashing. |

The Zero 2 W has **two** micro-USB ports: the inner one (often silk-screened
`USB`) is data — the OTG mic goes there; the outer one (`PWR`) is power.

---

## 1. Flash the SD card (Raspberry Pi Imager)

Install Raspberry Pi Imager on your laptop, then:

1. **Device:** Raspberry Pi Zero 2 W.
2. **OS:** *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64-bit)**.
   64-bit is required; "Lite" = no desktop, which is what keeps us under 512 MB.
3. Click the **gear / "Edit settings"** (OS customisation) **before** writing:
   - **Hostname:** `tbb-bench` → reachable later as `tbb-bench.local`.
   - **Enable SSH** → "Use password authentication" (or paste your public key).
   - **Username / password:** set a user (e.g. `pi`) + password — Bookworm has no
     default login.
   - **Configure wireless LAN:** your **2.4 GHz** SSID + password, and set the
     **Wireless LAN country**. ⚠️ The Zero 2 W is **2.4 GHz only** — it will not
     see a 5 GHz-only network. Bake the creds here; editing them post-flash is
     unreliable on this board.
   - **Locale:** your timezone + keyboard.
4. **Write.** Then eject and insert the card into the Pi.

---

## 2. First boot + SSH in

1. Plug the **USB mic into the OTG adapter → the Pi's data port**.
2. Power the Pi via the **PWR** port. First boot takes ~60–90 s.
3. From your laptop terminal:
   ```sh
   ssh pi@tbb-bench.local
   ```
   If `.local` doesn't resolve (some Windows setups), find the Pi's IP in your
   router's client list and `ssh pi@<ip>` instead.

---

## 3. Base packages + confirm the mic

On the Pi:

```sh
sudo apt update
sudo apt install -y ffmpeg git alsa-utils
```

- `ffmpeg` — does the ALSA capture for `MicSource`.
- `alsa-utils` — gives `arecord` so we can find and test the mic.

Find the mic and note its card/device:

```sh
arecord -l
#   card 1: Device [USB Audio Device], device 0: ...   → your device is plughw:1,0
```

Sanity-record 5 s (no speaker needed — we just check the file grows):

```sh
arecord -D plughw:1,0 -f S16_LE -r 48000 -c 1 -d 5 /tmp/mic.wav
ls -l /tmp/mic.wav   # should be ~480 KB, not 44 bytes
```

**⟶ ping Claude** with your `arecord -l` output if the device isn't `plughw:1,0`
or the test file is empty — we'll sort the device string / mic before continuing.

---

## 4. Install uv + get the code

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"      # put uv on PATH for this shell
uv --version
```

Get the `birdbrain` source. The `tbb` branch (with `MicSource` + `tbb-listen`) is
**not on GitHub yet** — it's local on the dev machine.

**⟶ ping Claude** — say "push tbb" and I'll push the branch to
`github.com/CaiusX/birdbrain.git`. Then on the Pi:

```sh
git clone -b tbb https://github.com/CaiusX/birdbrain.git
cd birdbrain
```

---

## 5. Python deps — the 512 MB decision point  ⟶ do this with Claude

This is the crux of Phase 0. `birdnetlib` picks its interpreter like this:

```python
try:    import tflite_runtime.interpreter as tflite   # preferred — light
except: from tensorflow import lite as tflite          # fallback — heavy
```

So whichever is installed decides our RAM. We measure **both** if we can.

### Route A — full TensorFlow (the repo's declared dep; measures the heavy path)

x86 reference says this peaks ~474 MB *just for the detector* — likely too big
for 512 MB, so give it swap so it runs (slowly) instead of getting OOM-killed:

```sh
sudo dphys-swapfile swapoff
sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=1024/' /etc/dphys-swapfile
sudo dphys-swapfile setup && sudo dphys-swapfile swapon

uv sync          # installs tensorflow (large download on aarch64; be patient)
```

If `uv sync` can't get a TensorFlow wheel for the Pi's Python/arch, that failure
is itself a Phase 0 finding — **⟶ ping Claude** and we pivot straight to Route B.

### Route B — tflite-runtime (the recommended unit path) — **proven 2026-06-22**

Because birdnetlib *prefers* `tflite_runtime`, installing it means full TF is
never imported at all. The one gotcha: `tflite-runtime` 2.14 ships a **cp311
aarch64 wheel but no cp312** — so the unit must run on **Python 3.11**, and the
pin has to be sticky (a bare `uv sync --python 3.11` is forgotten by the next
`uv run`, which rebuilds on uv's default 3.12 and fails). Proven recipe, run in
`~/birdbrain`:

```sh
# bench-local dep tweak: target 3.11 and swap full TF for tflite-runtime
sed -i 's/^requires-python = .*/requires-python = ">=3.11"/' pyproject.toml
sed -i '/^    "tensorflow>=2.16",/d' pyproject.toml
sed -i 's/^    "birdnetlib>=0.18.0",/&\n    "tflite-runtime>=2.14",/' pyproject.toml

uv python pin 3.11          # writes .python-version so EVERY uv cmd uses 3.11
uv sync                     # ~10–20 min on a Zero 2 W (prebuilt wheels)
uv run python --version     # must say 3.11.x

# confirm the light path: tflite present, tensorflow absent
uv run python -c "import tflite_runtime; print('tflite', tflite_runtime.__version__)"
uv run python -c "import tensorflow" 2>&1 | head -1   # expect ModuleNotFoundError
```

Measured result: detector loads ~1.5 s, warm inference ~1.05 s/chunk, **peak
RSS ~190 MB, zero swap** — comfortable on 512 MB. If `uv sync` ever can't find a
`tflite-runtime` wheel for the Pi's Python, that's the cp312 trap — re-check the
`uv python pin 3.11` step before reaching for the `ai-edge-litert` successor.

---

## 6. Run the bench + capture the numbers

With deps installed, find the right `--device` from step 3, then:

```sh
/usr/bin/time -v uv run birdbrain tbb-listen --device plughw:1,0 --seconds 60
```

- `tbb-listen` prints **per-chunk inference ms** + a headroom multiple, and a
  summary (min/avg/max) at the end.
- `/usr/bin/time -v` prints **"Maximum resident set size"** = peak RAM. (If
  `time` isn't found: `sudo apt install -y time`.)

Watch it live in a second SSH session if you like:
```sh
watch -n1 free -m
```

---

## 7. What to send back (closes the Phase 0 gate)

For each route you managed to run:

- inference ms: **min / avg / max** (from the `tbb-listen` summary)
- whether avg is comfortably **< 3000 ms** (target < 1500 ms)
- **peak RAM** (`time -v` "Maximum resident set size", in KB → ÷1024 = MB)
- whether it ran in RAM or leaned on swap (`free -m` during the run)

That gives us the on-metal numbers to confirm (or revise) the
TensorFlow → tflite-runtime recommendation in `tbb-build-plan.md`.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ssh tbb-bench.local` won't resolve | Use the IP from your router. mDNS can be flaky on Windows. |
| Pi never joins wifi | It's 2.4 GHz only and needs the wifi **country** set; re-flash with both baked in. |
| Random reboots under load | Weak power supply — use ≥2.5 A into the PWR port. |
| `arecord -l` shows no card | Re-seat the OTG adapter in the **data** port (not PWR); try `lsusb` to confirm the mic enumerates. |
| Process "Killed" mid-run | OOM. That's the full-TF signal — add/enlarge swap (Route A) or switch to Route B. |
| `tbb-listen` prints "no chunks captured" | Wrong `--device`; re-check `arecord -l` and the `plughw:X,Y` numbers. |
