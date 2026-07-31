#!/usr/bin/env bash
# Finish the africam -> birdbrain rename for the root-owned pieces.
#
# Everything user-owned (the package, the venv, the database, the checkout, the
# systemd USER units) was renamed already. These four live outside $HOME and
# need root:
#
#   /etc/africam/secrets.env                     ANTHROPIC_API_KEY for AI notes
#   /var/lib/africam-net-watchdog/               watchdog history + last_reboot
#   /usr/local/bin/africam-net-watchdog          the watchdog itself
#   /etc/systemd/system/africam-net-watchdog.*   its unit + timer
#
# Nothing here is urgent: the watchdog runs from an absolute path and is
# unaffected by the checkout move, and birdbrain-pipeline.service already reads
# BOTH /etc/africam/secrets.env and /etc/birdbrain/secrets.env (each optional),
# so the API key keeps working before and after this runs.
#
# MOVING rather than recreating /var/lib state is deliberate: last_reboot is
# what rate-limits the watchdog to one reboot per 6h. Recreating it empty would
# hand back a free reboot.
#
#   sudo bash scripts/rename-root-cutover.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "must run as root: sudo $0" >&2; exit 1; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

echo "=== 1/4  secrets ==="
if [ -d /etc/africam ] && [ ! -d /etc/birdbrain ]; then
    mv /etc/africam /etc/birdbrain
    echo "  /etc/africam -> /etc/birdbrain ($(ls /etc/birdbrain | tr '\n' ' '))"
else
    echo "  nothing to do (/etc/birdbrain exists or /etc/africam absent)"
fi

echo "=== 2/4  watchdog state (preserves the reboot rate-limit) ==="
if [ -d /var/lib/africam-net-watchdog ] && [ ! -d /var/lib/birdbrain-net-watchdog ]; then
    mv /var/lib/africam-net-watchdog /var/lib/birdbrain-net-watchdog
    echo "  moved; contents: $(ls /var/lib/birdbrain-net-watchdog | tr '\n' ' ')"
else
    echo "  nothing to do"
fi

echo "=== 3/4  stop + remove the old watchdog units ==="
systemctl disable --now africam-net-watchdog.timer 2>/dev/null || true
systemctl stop africam-net-watchdog.service 2>/dev/null || true
rm -f /etc/systemd/system/africam-net-watchdog.service \
      /etc/systemd/system/africam-net-watchdog.timer \
      /usr/local/bin/africam-net-watchdog
systemctl daemon-reload

echo "=== 4/4  install the renamed watchdog ==="
install -m 0755 -o root -g root scripts/birdbrain-net-watchdog.sh /usr/local/bin/birdbrain-net-watchdog
install -m 0644 -o root -g root scripts/birdbrain-net-watchdog.service /etc/systemd/system/
install -m 0644 -o root -g root scripts/birdbrain-net-watchdog.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now birdbrain-net-watchdog.timer

echo
echo "=== verification ==="
echo "  timer:   $(systemctl is-active birdbrain-net-watchdog.timer)"
# A timer that is active but whose script is broken looks healthy and does
# nothing — run it once for real.
if /usr/local/bin/birdbrain-net-watchdog; then
    echo "  smoke:   passed"
else
    echo "  smoke:   FAILED — investigate before trusting the watchdog"
fi
echo "  state:   $(ls /var/lib/birdbrain-net-watchdog 2>/dev/null | tr '\n' ' ')"
echo "  history: $(cat /var/lib/birdbrain-net-watchdog/history 2>/dev/null || echo '(none yet)')"
echo "  leftovers: $(ls -d /etc/africam /var/lib/africam-net-watchdog \
                       /usr/local/bin/africam-net-watchdog 2>/dev/null | tr '\n' ' ' || echo none)"
echo
echo "done. The pipeline picks up /etc/birdbrain/secrets.env on its next restart;"
echo "no restart is needed now because it already reads both paths."
