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

# Install a file and prove it actually landed. `install` has been seen to exit 0
# having written a 0-byte file (tbb-test, 2026-06-26: the net-watchdog was empty
# on disk for a month, so the unit had no recovery path when it lost wifi). Never
# trust the exit status alone, and never hide the error that explains it.
install_verified() {   # <src> <dst> <mode>
  local src="$1" dst="$2" mode="$3"
  # install(1) does not create parent dirs; journald.conf.d may not exist yet.
  sudo mkdir -p "$(dirname "$dst")"
  if ! sudo install -m "$mode" "$src" "$dst"; then
    echo "  FAILED: install $src -> $dst"; return 1
  fi
  sudo sync
  if ! sudo cmp -s "$src" "$dst"; then
    echo "  FAILED: $dst does not match $src after install (size: $(sudo stat -c %s "$dst" 2>/dev/null || echo '?') bytes)"
    return 1
  fi
  echo "  ok: $dst ($(sudo stat -c %s "$dst") bytes, mode $mode)"
  return 0
}

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

echo "=== installing stability tooling (persistent journal + wifi power-save off + net-watchdog + health-log) ==="
# Persistent journal. Without this every reboot destroys the logs explaining why
# the unit rebooted — and the net-watchdog reboots it on purpose after 20 failed
# DNS probes, so the most interesting logs are exactly the ones that vanish.
# Two traps here, both of which made a unit look configured but stay volatile:
#   1. Raspberry Pi OS ships /usr/lib/systemd/journald.conf.d/
#      40-rpi-volatile-storage.conf with Storage=volatile. Drop-ins apply in
#      FILENAME order, so ours must sort after it -> 99-, not 00-.
#   2. journald moves to /var/log/journal only at boot or on an explicit flush,
#      so flush rather than leaving it correct-on-paper but still in /run.
if [ -f scripts/journald-persistent-tbb.conf ]; then
  if install_verified scripts/journald-persistent-tbb.conf \
       /etc/systemd/journald.conf.d/99-tbb-persistent.conf 0644; then
    sudo mkdir -p /var/log/journal
    sudo systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
    sudo systemctl restart systemd-journald 2>/dev/null || true
    sudo journalctl --flush 2>/dev/null || true
    if journalctl --header 2>/dev/null | grep -q "/var/log/journal"; then
      echo "  journal: persistent ($(journalctl --disk-usage 2>/dev/null | sed 's/^.*take up //'))"
    else
      echo "  WARNING: journal still volatile — check journalctl --header"
    fi
  else
    echo "  (could not install journald conf — need sudo)"
  fi
fi
# Pi Zero 2 W wifi defaults to power-save ON → the unit silently drops off the
# LAN. Disable it (system-wide, persists across reconnects). We only reload NM
# here (not a radio cycle) so we don't kill the SSH session running this script;
# it takes full effect on the next reconnect/reboot.
if [ -f scripts/wifi-powersave-off.conf ]; then
  if install_verified scripts/wifi-powersave-off.conf \
       /etc/NetworkManager/conf.d/wifi-powersave-off.conf 0644; then
    sudo systemctl reload NetworkManager 2>/dev/null || true
    echo "  wifi-powersave: installed (effective after reconnect/reboot)"
  else
    echo "  (could not install wifi-powersave-off.conf — need sudo + NetworkManager)"
  fi
fi
# Network watchdog (root timer): probes DNS each minute, reseeds a missing wifi
# profile after 3 consecutive failures, cycles wifi after 5, reboots after 20 —
# recovers a wedged radio with no shared fate with the python process.
if [ -f scripts/africam-net-watchdog.sh ]; then
  if install_verified scripts/africam-net-watchdog.sh /usr/local/bin/africam-net-watchdog 0755 \
     && install_verified scripts/africam-net-watchdog.service /etc/systemd/system/africam-net-watchdog.service 0644 \
     && install_verified scripts/africam-net-watchdog.timer /etc/systemd/system/africam-net-watchdog.timer 0644; then
    sudo systemctl daemon-reload 2>/dev/null || true
    sudo systemctl enable --now africam-net-watchdog.timer 2>/dev/null || true
    echo "  net-watchdog: $(systemctl is-active africam-net-watchdog.timer 2>/dev/null)"
    # A timer that is active but whose script is empty looks healthy and does
    # nothing. Run it once now and confirm it is a real, working binary.
    if sudo /usr/local/bin/africam-net-watchdog && sudo systemctl start africam-net-watchdog.service; then
      echo "  net-watchdog: smoke test passed"
    else
      echo "  WARNING: net-watchdog installed but failed its smoke test"
    fi
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
