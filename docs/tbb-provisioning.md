# TBB Provisioning — golden-image build (Phase 1)

How to turn a blank Raspberry Pi Zero 2 W into a working standalone TinyBirdBrain
unit: boots, detects from the USB mic, stores locally, and serves the LAN-only
web UI — **no internet required**. Sync to central is Phase 2; enrollment/claim
and first-boot wifi captive AP are Phase 3.

For the one-off bench bring-up (flashing, wifi, mic check) see
[`tbb-phase0-bench.md`](tbb-phase0-bench.md) — this doc reuses those steps and
adds the config profile + services that make the unit run unattended.

---

## 1. Base image

- **Raspberry Pi OS Lite, 64-bit (Bookworm).** Flash with Raspberry Pi Imager;
  in OS customisation set hostname `tbb-XXXX` (→ reachable at `tbb-XXXX.local`
  via the avahi/mDNS that Pi OS runs by default), enable SSH, and bake the
  **2.4 GHz** wifi SSID + **wireless country** (the Zero 2 W is 2.4 GHz only).
- First boot, SSH in, then base packages:
  ```sh
  sudo apt update
  sudo apt install -y ffmpeg git alsa-utils time
  ```

## 2. Python deps — Python 3.11 + tflite-runtime (NOT full TensorFlow)

Phase 0 measured full TensorFlow at ~474 MB — too big for 512 MB. The unit runs
`tflite-runtime` instead (190 MB peak, proven on metal). `birdnetlib` prefers
`tflite_runtime` automatically, and `tflite-runtime` only ships a **cp311**
aarch64 wheel, so the unit pins **Python 3.11**.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
git clone -b tbb https://github.com/CaiusX/birdbrain.git ~/birdbrain
cd ~/birdbrain

# Interim tbb dependency profile: drop full TF, add tflite-runtime, target 3.11.
# (Central's pyproject stays on TensorFlow/3.12 — do NOT commit this edit; it is
#  the unit's local install only. A first-class tbb dependency profile is a
#  tracked follow-up — see the Phase 0 decision record in tbb-build-plan.md.)
sed -i 's/^requires-python = .*/requires-python = ">=3.11"/' pyproject.toml
sed -i '/^    "tensorflow>=2.16",/d' pyproject.toml
sed -i 's/^    "birdnetlib>=0.18.0",/&\n    "tflite-runtime>=2.14",/' pyproject.toml

uv python pin 3.11      # sticky — every `uv run` uses 3.11, not uv's default 3.12
uv sync                 # ~10-20 min on a Zero 2 W
uv run python -c "import tflite_runtime; print('tflite', tflite_runtime.__version__)"
```

## 3. Unit configuration (`.env`)

The unit runs the same `africam` package under the `tbb` profile, driven by
`AFRICAM_TBB_*` env. Put a `.env` in `~/birdbrain` (read automatically; also what
the `/setup` page writes):

```ini
# ~/birdbrain/.env
AFRICAM_TBB_UNIT_ID=tbb-a1b2
AFRICAM_TBB_MIC_DEVICE=plughw:0,0      # from `arecord -l`
AFRICAM_TBB_LAT=-25.75                 # optional; enables BirdNET locality filter
AFRICAM_TBB_LON=28.23
AFRICAM_TBB_TIMEZONE=Africa/Johannesburg
AFRICAM_TBB_CLIP_RETENTION_DAYS=14
AFRICAM_TBB_SYNC_ENABLED=false         # Phase 2
AFRICAM_DB_URL=sqlite:////home/pi/birdbrain/data/africam.sqlite
AFRICAM_CLIPS_DIR=/home/pi/birdbrain/data/clips
```

Smoke-test before installing services:

```sh
uv run africam tbb-listen --device plughw:0,0 --seconds 30   # detections + timing
uv run africam tbb-pipeline    # Ctrl-C after you see a heartbeat/detection
```

## 4. Services (systemd **user** units)

Two long-running user services, mirroring the central deploy. Templates are in
[`scripts/`](../scripts): `tbb-pipeline.service` (mic → detector → SQLite +
clips) and `tbb-web.service` (LAN web UI on :8080). They use `%h/birdbrain` as
the working dir and `%h/.local/bin/uv` to launch.

```sh
mkdir -p ~/.config/systemd/user
cp ~/birdbrain/scripts/tbb-pipeline.service ~/.config/systemd/user/
cp ~/birdbrain/scripts/tbb-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tbb-pipeline tbb-web

# Run the services without an active login session (survives reboot headless):
loginctl enable-linger "$USER"
```

The `pi` user must be able to read the mic — it's in the `audio` group by
default on Pi OS (`groups | grep audio`; `sudo usermod -aG audio "$USER"` if not).

## 5. Verify

```sh
systemctl --user status tbb-pipeline tbb-web
journalctl --user -u tbb-pipeline -f          # watch detections land
curl -s localhost:8080/healthz                # {"ok": true, "listening": true, ...}
```

From a phone on the same wifi: open **http://tbb-XXXX.local:8080** → the **Now**
page should show live detections, **Today** the species list. Reboot the Pi and
confirm both services come back and detections persist (SQLite + clips on the
SD card). Clips older than `AFRICAM_TBB_CLIP_RETENTION_DAYS` are pruned by the
pipeline automatically.

---

## Acceptance (Phase 1)

- [ ] Fresh Zero 2 W + mic, **no internet**: detections appear on `Now`, persist
      across reboot, clips saved and pruned per retention.
- [ ] RAM stable over a multi-hour run; UI responsive on a phone over LAN.
- [ ] Central pipeline/app untouched and still green.
