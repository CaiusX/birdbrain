#!/usr/bin/env bash
# africam-net-watchdog: restore networking if DNS stays broken. Runs as a root
# systemd timer every minute so it has no shared fate with the africam python
# process — that was the recovery gap on the 7-day silent stall: yt-dlp failed
# DNS forever, and the app had no way to kick the kernel/networking layer.
#
# Escalation on consecutive failures:
#   RESEED_AT  — if NM has no wifi profile at all, rewrite one from the
#                cloud-init seed on the boot partition. Cycling a radio cannot
#                help when there is nothing to connect *to*; see below.
#   WIFI_CYCLE_AT — cycle the radio (wedged association)
#   REBOOT_AT     — reboot (wedged kernel/driver)
#
# The reseed step exists because of tbb-test, 2026-07-23: both NetworkManager
# connection profiles were truncated to 0 bytes at 07:40 UTC and the unit then
# booted without wifi for six days. It kept recording the whole time, so nothing
# local looked wrong — it was simply unreachable. The old watchdog could only
# cycle the radio and reboot, neither of which can recreate a deleted profile.
set -u

PROBE_HOST="${PROBE_HOST:-www.youtube.com}"
RESEED_AT="${RESEED_AT:-3}"
WIFI_CYCLE_AT="${WIFI_CYCLE_AT:-5}"
REBOOT_AT="${REBOOT_AT:-20}"
STATE_FILE="${STATE_FILE:-/var/lib/africam-net-watchdog/fail_count}"
SEED_FILE="${SEED_FILE:-/boot/firmware/network-config}"
KEYFILE="${KEYFILE:-/etc/NetworkManager/system-connections/preconfigured.nmconnection}"
# Every nmcli call goes through this one absolute path, so a test harness can
# point it at a stub and no code path can escape to the real radio.
NMCLI="${NMCLI:-/usr/bin/nmcli}"

mkdir -p "$(dirname "$STATE_FILE")"

if getent hosts "$PROBE_HOST" >/dev/null 2>&1; then
    [ -f "$STATE_FILE" ] && rm -f "$STATE_FILE"
    exit 0
fi

count=$(( $(cat "$STATE_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$STATE_FILE"
logger -t africam-net-watchdog "DNS probe '$PROBE_HOST' failed (consecutive=$count)"

# Does NetworkManager know about any wifi connection at all?
have_wifi_profile() {
    "$NMCLI" -t -f TYPE connection show 2>/dev/null \
        | grep -q '^802-11-wireless$'
}

# Rewrite a wifi keyfile from the cloud-init seed that rpi-imager wrote to the
# boot partition. cloud-init itself will not redo this: the instance-id never
# changes, so it considers network config already applied.
reseed_wifi() {
    [ -r "$SEED_FILE" ] || { logger -t africam-net-watchdog "reseed: $SEED_FILE unreadable"; return 1; }

    local parsed ssid psk
    parsed=$(python3 - "$SEED_FILE" <<'PY' 2>/dev/null
import re, sys
text = open(sys.argv[1]).read()
m = re.search(
    r'access-points:\s*\n\s*"?([^"\n:]+)"?:\s*\n\s*password:\s*"?([0-9a-fA-F]+)"?',
    text)
if not m:
    sys.exit(1)
ssid, psk = m.group(1).strip(), m.group(2).strip()
if not re.fullmatch(r'[0-9a-fA-F]{64}', psk):
    sys.exit(1)          # not a PMK; refuse to guess the format
print(ssid); print(psk)
PY
    ) || { logger -t africam-net-watchdog "reseed: could not parse $SEED_FILE"; return 1; }

    ssid=$(printf '%s\n' "$parsed" | sed -n 1p)
    psk=$(printf '%s\n' "$parsed" | sed -n 2p)
    [ -n "$ssid" ] && [ -n "$psk" ] || return 1

    mkdir -p "$(dirname "$KEYFILE")"
    local tmp="${KEYFILE}.tmp.$$"
    ( umask 077; cat > "$tmp" <<EOF
[connection]
id=preconfigured
uuid=$(cat /proc/sys/kernel/random/uuid)
type=wifi
interface-name=wlan0
autoconnect=true
autoconnect-retries=0

[wifi]
mode=infrastructure
ssid=$ssid

[wifi-security]
key-mgmt=wpa-psk
psk=$psk

[ipv4]
method=auto

[ipv6]
method=auto
addr-gen-mode=default
EOF
    ) || { rm -f "$tmp"; return 1; }

    # NM ignores keyfiles that are group/world readable, and a half-written one
    # is what put us here — only publish it once it is complete on disk.
    chmod 0600 "$tmp"
    chown root:root "$tmp" 2>/dev/null || true
    if [ ! -s "$tmp" ]; then rm -f "$tmp"; return 1; fi
    mv -f "$tmp" "$KEYFILE"
    sync

    logger -t africam-net-watchdog "reseed: wrote wifi profile for SSID '$ssid' from $SEED_FILE"
    "$NMCLI" connection reload >/dev/null 2>&1 || true
    "$NMCLI" connection up preconfigured >/dev/null 2>&1 || true
    return 0
}

if [ "$count" -ge "$REBOOT_AT" ]; then
    logger -t africam-net-watchdog "rebooting (reached $REBOOT_AT consecutive failures)"
    rm -f "$STATE_FILE"
    /usr/bin/systemctl reboot
    exit 0
fi

# Check for a missing profile before falling back to the blunter remedies — a
# reseed fixes the one failure mode the others provably cannot.
if [ "$count" -ge "$RESEED_AT" ] && ! have_wifi_profile; then
    logger -t africam-net-watchdog "no wifi profile known to NetworkManager — reseeding"
    if reseed_wifi; then
        rm -f "$STATE_FILE"     # give the new profile a clean run at it
        exit 0
    fi
    logger -t africam-net-watchdog "reseed failed — falling through to radio cycle/reboot"
fi

if [ "$count" -eq "$WIFI_CYCLE_AT" ]; then
    logger -t africam-net-watchdog "cycling wifi (reached $WIFI_CYCLE_AT consecutive failures)"
    "$NMCLI" radio wifi off
    sleep 2
    "$NMCLI" radio wifi on
fi
