#!/usr/bin/env bash
# TinyBirdBrain unit bootstrap — takes a freshly-flashed Raspberry Pi OS Lite
# (Zero 2 W) all the way to a running TBB unit: base packages, uv, the birdbrain
# checkout + tflite venv, then hands off to tbb-finish.sh to write .env and
# enable the services + stability tooling. Idempotent and RESUMABLE — safe to
# re-run (e.g. after the Codec Zero reboot). See docs/tbb-provisioning.md.
#
# Run this ON THE UNIT, not on central. Transfer it with scp (no copy-paste):
#   scp scripts/tbb-bootstrap.sh pi@bnc1.local:~/
#   ssh pi@bnc1.local
#   # USB mic (plug it in first):
#   bash ~/tbb-bootstrap.sh bnc1 --lat -26.129 --lon 28.034 --usb
#   # Codec Zero onboard mic (installs overlay, reboots, re-run to finish):
#   bash ~/tbb-bootstrap.sh bnc1 --lat -26.129 --lon 28.034 --codec
#
set -euo pipefail

UNIT_ID="${1:?usage: tbb-bootstrap.sh <unit-id> [--lat L] [--lon L] [--usb|--codec|--mic DEV] [--branch tbb]}"
shift

LAT=""; LON=""; MIC=""; MICKIND="auto"; BRANCH="tbb"
REPO="https://github.com/CaiusX/birdbrain.git"
DEST="$HOME/birdbrain"
while [ $# -gt 0 ]; do
  case "$1" in
    --lat)    LAT="$2"; shift 2;;
    --lon)    LON="$2"; shift 2;;
    --mic)    MIC="$2"; MICKIND="explicit"; shift 2;;
    --usb)    MICKIND="usb"; shift;;
    --codec)  MICKIND="codec"; shift;;
    --branch) BRANCH="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

log(){ printf '\n=== %s ===\n' "$*"; }

# --- 1. base packages ---------------------------------------------------------
log "1/5 base packages (apt)"
sudo apt-get update -y
sudo apt-get install -y ffmpeg git alsa-utils time curl

# --- 2. uv --------------------------------------------------------------------
log "2/5 uv package manager"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# shellcheck disable=SC1091
[ -f "$HOME/.local/bin/env" ] && . "$HOME/.local/bin/env"
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { echo "uv not on PATH after install" >&2; exit 1; }

# --- 3. checkout + tflite venv + deps ----------------------------------------
# Python 3.11 because tflite-runtime only ships a cp311 aarch64 wheel; the unit
# uses tflite-runtime (190 MB) NOT full TensorFlow (474 MB, too big for 512 MB).
# --no-deps -e . leaves the checkout unmodified so the self-updater can ff/roll-back.
log "3/5 birdbrain checkout + tflite venv (branch $BRANCH)"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" fetch origin "$BRANCH"
  git -C "$DEST" checkout "$BRANCH"
  git -C "$DEST" reset --hard "origin/$BRANCH"
else
  git clone -b "$BRANCH" "$REPO" "$DEST"
fi
cd "$DEST"
[ -d .venv ] || uv venv --python 3.11 .venv
uv pip install -r deploy/tbb/requirements-tbb.txt   # ~10-20 min first time on a Zero 2 W
uv pip install --no-deps -e .
log "verify deps (tflite present, tensorflow absent)"
.venv/bin/python -c "import tflite_runtime, birdnetlib, birdbrain; print('tflite', tflite_runtime.__version__, 'ok')"
.venv/bin/python -c "import tensorflow" 2>&1 | head -1 || true

# --- 4. resolve the mic device -----------------------------------------------
log "4/5 microphone ($MICKIND)"
case "$MICKIND" in
  codec)
    MIC="plughw:CARD=Zero,DEV=0"
    # The Codec Zero is I2S — the overlay only takes effect after a reboot, and
    # its ALSA card is named "Zero" ONLY with the explicit overlay (bare EEPROM
    # probe names it IQaudIOCODEC and breaks CARD=Zero). See tbb-provisioning §2b.
    if ! arecord -l 2>/dev/null | grep -qi "card .*Zero"; then
      if ! grep -q '^dtoverlay=rpi-codeczero' /boot/firmware/config.txt; then
        sudo sed -i 's/^dtparam=audio=on/#dtparam=audio=on/' /boot/firmware/config.txt || true
        echo 'dtoverlay=rpi-codeczero' | sudo tee -a /boot/firmware/config.txt >/dev/null
      fi
      [ -d "$HOME/Pi-Codec" ] || git clone --depth 1 https://github.com/raspberrypi/Pi-Codec.git "$HOME/Pi-Codec"
      sudo cp scripts/tbb-codec.service /etc/systemd/system/ 2>/dev/null || true
      sudo systemctl daemon-reload 2>/dev/null || true
      sudo systemctl enable tbb-codec 2>/dev/null || true
      echo
      echo ">>> Codec Zero overlay installed but needs a reboot. Then re-run to finish:"
      echo ">>>   sudo reboot"
      echo ">>>   bash ~/tbb-bootstrap.sh $UNIT_ID ${LAT:+--lat $LAT} ${LON:+--lon $LON} --codec"
      exit 0
    fi
    # card present → (re)apply the onboard-mic mixer routing now
    sudo alsactl restore -f "$HOME/Pi-Codec/Codec_Zero_OnboardMIC_record_and_SPK_playback.state" 2>/dev/null || true
    ;;
  usb|auto)
    # first capture card's short name → plughw:CARD=<name>,DEV=0
    CARD=$(arecord -l 2>/dev/null | sed -nE 's/^card [0-9]+: ([A-Za-z0-9_]+).*/\1/p' | head -1)
    MIC="plughw:CARD=${CARD},DEV=0"; [ -n "$CARD" ] || MIC="plughw:0,0"
    ;;
  explicit) : ;;  # MIC already provided via --mic
esac
echo "mic device: $MIC"
if ! arecord -l 2>/dev/null | grep -qiE "card [0-9]"; then
  echo "WARNING: no capture device detected — plug in the USB mic (or reboot for the codec) and re-run." >&2
fi

# --- 5. finalize (.env + services + stability tooling) -----------------------
log "5/5 finalize via tbb-finish.sh"
bash scripts/tbb-finish.sh "$UNIT_ID" "$LAT" "$LON" "$MIC"

log "done — $UNIT_ID"
echo "Verify:  systemctl --user status tbb-pipeline tbb-web"
echo "         curl -s localhost:8080/healthz"
echo "Phone :  http://${UNIT_ID}.local:8080   (Now = live detections)"
echo "Reporting: both targets start OFF."
echo "  BirdNET-Cloud (the usual one) — add a token with scripts/save-token.sh,"
echo "                                  then BIRDBRAIN_BIRDNETCLOUD_ENABLED=true"
echo "  Central birdbrain (optional)  — enroll from the unit's /setup page"
