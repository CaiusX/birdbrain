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
aarch64 wheel, so the unit runs **Python 3.11**.

The unit's deps live in `deploy/tbb/requirements-tbb.txt` (the central deps with
tensorflow swapped for tflite-runtime). Central's `pyproject`/`uv.lock` stay
TensorFlow-based, so the unit installs into its **own venv** and the git
checkout stays **unmodified** — which is what lets the self-updater fast-forward
and roll back cleanly.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
git clone -b tbb https://github.com/CaiusX/birdbrain.git ~/birdbrain
cd ~/birdbrain

uv venv --python 3.11 .venv                          # 3.11 (tflite has no cp312 wheel)
uv pip install -r deploy/tbb/requirements-tbb.txt    # ~10-20 min on a Zero 2 W
uv pip install --no-deps -e .                        # the africam package itself
.venv/bin/python -c "import tflite_runtime; print('tflite', tflite_runtime.__version__)"
.venv/bin/python -c "import tensorflow" 2>&1 | head -1   # expect ModuleNotFoundError
```

The services and the updater call `~/birdbrain/.venv/bin/africam` directly (not
`uv run`, whose auto-sync would reinstall full TensorFlow from `pyproject`).

> **Migrating a unit that used the old sed-edit install:** discard the local
> edit and rebuild the venv —
> `git checkout -- pyproject.toml && git pull && rm -rf .venv` then the three
> `uv` commands above.

### 2b. Audio input options

The unit reads any ALSA device (`AFRICAM_TBB_MIC_DEVICE`), so the mic is a
hardware choice, not a code change:

- **USB mic** (e.g. a lavalier or USB headset): plug in, `arecord -l`, use its
  `plughw:<card>,<dev>`. No extra setup.
- **Raspberry Pi Codec Zero** (integrated HAT, onboard MEMS mic — cleaner for a
  puck). It's I2S, so:
  ```sh
  # enable the codec overlay, reboot
  sudo sed -i 's/^dtparam=audio=on/#dtparam=audio=on/' /boot/firmware/config.txt
  echo 'dtoverlay=rpi-codeczero' | sudo tee -a /boot/firmware/config.txt
  sudo reboot
  # load the mixer routing for the onboard mic (states from Raspberry Pi)
  git clone https://github.com/raspberrypi/Pi-Codec.git ~/Pi-Codec
  sudo alsactl restore -f ~/Pi-Codec/Codec_Zero_OnboardMIC_record_and_SPK_playback.state
  arecord -l   # shows card "Zero"  →  AFRICAM_TBB_MIC_DEVICE=plughw:CARD=Zero,DEV=0
  ```
  Other presets in that repo: `Codec_Zero_StereoMIC_record_and_HP_playback.state`
  (external MIC1/MIC2), `Codec_Zero_AUXIN_record_and_HP_playback.state` (line in).
  The codec loses its routing on power-off, so install
  [`scripts/tbb-codec.service`](../scripts/tbb-codec.service) to re-apply it at
  boot before the pipeline starts:
  ```sh
  sudo cp ~/birdbrain/scripts/tbb-codec.service /etc/systemd/system/
  sudo systemctl daemon-reload && sudo systemctl enable --now tbb-codec
  ```
  > **Always add the explicit `dtoverlay=rpi-codeczero`.** A genuine Codec Zero
  > HAT's EEPROM auto-probes and names the ALSA card **`IQaudIOCODEC`** (not
  > `Zero`) if you *don't* add the overlay — which breaks the `CARD=Zero` device
  > string, the stock `state.Zero` mixer file, and `tbb-codec.service`. With the
  > explicit overlay the card is `Zero` and everything lines up.
  >
  > Alternative to `tbb-codec.service`: after the `alsactl restore` above, run
  > `sudo alsactl store` once — the **standard** `alsa-restore.service` then
  > replays the saved `/var/lib/alsa/asound.state` at every boot, no custom unit.
  (Edit the unit's `ExecStart` path/preset if you cloned elsewhere or use a
  different mic.)

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
.venv/bin/africam tbb-listen --device plughw:0,0 --seconds 30   # detections + timing
.venv/bin/africam tbb-pipeline    # Ctrl-C after you see a heartbeat/detection
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

## 4b. Self-update (optional)

Deployed units can update themselves so you never have to touch them. The
updater (`scripts/tbb-update.sh`) fast-forwards the unit's branch, reinstalls
the tbb requirements, restarts the services, and checks `/healthz`. It's
fast-forward-only, and because the unit runs an unmodified checkout it **rolls
back** (`git reset --hard` + reinstall) if the new version fails its health
check.

```sh
cp ~/birdbrain/scripts/tbb-update.service ~/.config/systemd/user/
cp ~/birdbrain/scripts/tbb-update.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tbb-update.timer      # nightly, random ≤1h jitter
# update on demand + watch:
systemctl --user start tbb-update && journalctl --user -u tbb-update -n 20 --no-pager
```

(Override the tracked branch with `AFRICAM_TBB_UPDATE_REF`, e.g. a release tag.)

## 4c. Stability tooling (network resilience — REQUIRED on a Zero 2 W)

The Zero 2 W's wifi defaults to **power-save ON**, which makes a unit silently
drop off the LAN (mdns/ssh/web all go "no route to host") under light load or
idle — and without a watchdog it never rejoins until it happens to reboot. A
unit missing these will look healthy on the bench and then vanish in the field.
`scripts/tbb-finish.sh` installs all three automatically; the pieces are:

- **Wifi power-save off** — [`scripts/wifi-powersave-off.conf`](../scripts/wifi-powersave-off.conf)
  → `/etc/NetworkManager/conf.d/` (`wifi.powersave = 2`). The fix that keeps the
  radio up. Verify with `iw dev wlan0 get power_save` → `off`.
- **Network watchdog** — [`scripts/africam-net-watchdog.sh`](../scripts/africam-net-watchdog.sh)
  → `/usr/local/bin/`, with its `.service`/`.timer` as a **root** systemd timer.
  Probes DNS each minute; cycles wifi after 5 consecutive failures, reboots
  after 20. The safety net (no shared fate with the python process).
- **Health-log** — [`scripts/health-log.sh`](../scripts/health-log.sh) + user
  timer: per-minute resource sampler to `data/health.log` for diagnosing a hang
  after the fact (read the lines just before the gap).

To add them to an already-provisioned unit, just re-run `tbb-finish.sh` (it's
idempotent), or install by hand following the copies above.

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

## Acceptance (Phase 1) — verified on the bench Zero 2 W, 2026-06-22

- [x] Fresh Zero 2 W + mic, **no internet**: detections appear on `Now` (phone,
      over LAN), persist across reboot (3 rows / 3 species survived a `reboot`),
      clips saved under `data/clips/<unit>/<date>/`. Retention prune covered by
      unit test (`test_prune_clips_*`).
- [x] Both `tbb-pipeline` / `tbb-web` user services auto-start headless after
      reboot (linger); `/healthz` green, UI responsive on a phone over LAN.
- [x] Central pipeline/app untouched and still green (14 tests pass; central
      `web/app.py` imports; lint counts unchanged from baseline).
- [ ] Multi-hour RAM soak — left running; glance at `free -m` later to confirm
      no creep (steady-state footprint already comfortable on 512 MB).
