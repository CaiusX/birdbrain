#!/usr/bin/env bash
# africam-net-watchdog: cycle wifi (and eventually reboot) if DNS to YouTube
# stays broken. Runs as a root systemd timer every minute so it has no shared
# fate with the africam python process — that was the recovery gap on the
# 7-day silent stall: yt-dlp failed DNS forever, and the app had no way to
# kick the kernel/networking layer.
set -u

PROBE_HOST="${PROBE_HOST:-www.youtube.com}"
WIFI_CYCLE_AT="${WIFI_CYCLE_AT:-5}"
REBOOT_AT="${REBOOT_AT:-20}"
STATE_FILE="${STATE_FILE:-/var/lib/africam-net-watchdog/fail_count}"

mkdir -p "$(dirname "$STATE_FILE")"

if getent hosts "$PROBE_HOST" >/dev/null 2>&1; then
    [ -f "$STATE_FILE" ] && rm -f "$STATE_FILE"
    exit 0
fi

count=$(( $(cat "$STATE_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$STATE_FILE"
logger -t africam-net-watchdog "DNS probe '$PROBE_HOST' failed (consecutive=$count)"

if [ "$count" -ge "$REBOOT_AT" ]; then
    logger -t africam-net-watchdog "rebooting (reached $REBOOT_AT consecutive failures)"
    rm -f "$STATE_FILE"
    /usr/bin/systemctl reboot
    exit 0
fi

if [ "$count" -eq "$WIFI_CYCLE_AT" ]; then
    logger -t africam-net-watchdog "cycling wifi (reached $WIFI_CYCLE_AT consecutive failures)"
    /usr/bin/nmcli radio wifi off
    sleep 2
    /usr/bin/nmcli radio wifi on
fi
