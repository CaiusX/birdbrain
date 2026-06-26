#!/usr/bin/env bash
# Finalize a freshly-installed TinyBirdBrain unit: write its .env and enable the
# systemd user services. Run on the unit AFTER the venv/deps are installed (see
# docs/tbb-provisioning.md). Idempotent — safe to re-run.
#
#   bash scripts/tbb-finish.sh <unit-id> [lat] [lon] [mic-device]
#   e.g.  bash scripts/tbb-finish.sh tbb-mems -26.129 28.034
#
# mic-device defaults to the Codec Zero onboard MIC (plughw:CARD=Zero,DEV=0).
# Sync to central is left OFF here — enable it after enrollment.
set -euo pipefail

UNIT_ID="${1:?usage: tbb-finish.sh <unit-id> [lat] [lon] [mic-device]}"
LAT="${2:-}"; LON="${3:-}"; MIC="${4:-plughw:CARD=Zero,DEV=0}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== deps check ==="
.venv/bin/python -c "import importlib.util as u;[print(m,bool(u.find_spec(m))) for m in ['numpy','scipy','birdnetlib','tflite_runtime','africam']]"

echo "=== mic devices (want a 'Zero' card) ==="
arecord -l 2>/dev/null | grep -iE "card [0-9]" || echo "  NO CAPTURE DEVICE — codec overlay not loaded (reboot needed)"

echo "=== writing .env ==="
{
  echo "AFRICAM_TBB_UNIT_ID=${UNIT_ID}"
  echo "AFRICAM_TBB_MIC_DEVICE=${MIC}"
  [ -n "$LAT" ] && echo "AFRICAM_TBB_LAT=${LAT}"
  [ -n "$LON" ] && echo "AFRICAM_TBB_LON=${LON}"
  echo "AFRICAM_TBB_TIMEZONE=Africa/Johannesburg"
  echo "AFRICAM_TBB_CLIP_RETENTION_DAYS=14"
  echo "AFRICAM_TBB_SYNC_ENABLED=false"
  echo "AFRICAM_DB_URL=sqlite:///${ROOT}/data/africam.sqlite"
  echo "AFRICAM_CLIPS_DIR=${ROOT}/data/clips"
} > "${ROOT}/.env"
echo "wrote ${ROOT}/.env:"; sed 's/^/  /' "${ROOT}/.env"

echo "=== installing + enabling user services ==="
mkdir -p "$HOME/.config/systemd/user"
cp scripts/tbb-pipeline.service scripts/tbb-web.service "$HOME/.config/systemd/user/"
cp scripts/tbb-update.service scripts/tbb-update.timer "$HOME/.config/systemd/user/" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now tbb-pipeline tbb-web
systemctl --user enable --now tbb-update.timer >/dev/null 2>&1 || true
loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" 2>/dev/null || true

echo "=== installing stability tooling (wifi power-save off + net-watchdog + health-log) ==="
# Pi Zero 2 W wifi defaults to power-save ON → the unit silently drops off the
# LAN. Disable it (system-wide, persists across reconnects). We only reload NM
# here (not a radio cycle) so we don't kill the SSH session running this script;
# it takes full effect on the next reconnect/reboot.
if [ -f scripts/wifi-powersave-off.conf ]; then
  sudo cp scripts/wifi-powersave-off.conf /etc/NetworkManager/conf.d/ 2>/dev/null \
    && sudo systemctl reload NetworkManager 2>/dev/null \
    && echo "  wifi-powersave: installed (effective after reconnect/reboot)" \
    || echo "  (could not install wifi-powersave-off.conf — need sudo + NetworkManager)"
fi
# Network watchdog (root timer): probes DNS each minute, cycles wifi after 5
# consecutive failures, reboots after 20 — recovers a wedged radio with no
# shared fate with the python process.
if [ -f scripts/africam-net-watchdog.sh ]; then
  if sudo install -m 0755 scripts/africam-net-watchdog.sh /usr/local/bin/africam-net-watchdog 2>/dev/null \
     && sudo cp scripts/africam-net-watchdog.service scripts/africam-net-watchdog.timer /etc/systemd/system/ 2>/dev/null; then
    sudo systemctl daemon-reload 2>/dev/null || true
    sudo systemctl enable --now africam-net-watchdog.timer 2>/dev/null || true
    echo "  net-watchdog: $(systemctl is-active africam-net-watchdog.timer 2>/dev/null)"
  else
    echo "  (could not install net-watchdog — need sudo)"
  fi
fi
# Health-log (user timer): per-minute resource sampler for hang diagnostics.
if [ -f scripts/health-log.service ]; then
  chmod +x scripts/health-log.sh 2>/dev/null || true
  cp scripts/health-log.service scripts/health-log.timer "$HOME/.config/systemd/user/" 2>/dev/null || true
  systemctl --user daemon-reload
  systemctl --user enable --now health-log.timer >/dev/null 2>&1 || true
  echo "  health-log: $(systemctl --user is-active health-log.timer 2>/dev/null)"
fi

sleep 4
echo "=== service state ==="
echo "tbb-pipeline: $(systemctl --user is-active tbb-pipeline)"
echo "tbb-web:      $(systemctl --user is-active tbb-web)"
echo "healthz:      $(curl -s -m 5 localhost:8080/healthz || echo unreachable)"
echo "TBB-FINISH-DONE"
