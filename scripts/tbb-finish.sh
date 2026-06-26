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

sleep 4
echo "=== service state ==="
echo "tbb-pipeline: $(systemctl --user is-active tbb-pipeline)"
echo "tbb-web:      $(systemctl --user is-active tbb-web)"
echo "healthz:      $(curl -s -m 5 localhost:8080/healthz || echo unreachable)"
echo "TBB-FINISH-DONE"
