#!/usr/bin/env bash
# birdbrain-net-watchdog: restore networking if DNS stays broken. Runs as a root
# systemd timer every minute so it has no shared fate with the birdbrain python
# process — that was the recovery gap on the 7-day silent stall: yt-dlp failed
# DNS forever, and the app had no way to kick the kernel/networking layer.
#
# Escalation, on failures within a rolling WINDOW of the last N probes:
#   RESEED_AT     — if NM has no wifi profile at all, rewrite one from the
#                   cloud-init seed on the boot partition. Cycling a radio cannot
#                   help when there is nothing to connect *to*; see below.
#   WIFI_CYCLE_AT — cycle the radio (wedged association)
#   REBOOT_AT     — reboot (wedged kernel/driver), rate-limited; see below.
#
# WHY A WINDOW AND NOT A CONSECUTIVE COUNT (2026-07-29):
# The original version counted *consecutive* failures and reset the counter on
# any single success. On 2026-07-26 the main Pi lost DNS for 6h15m and the
# watchdog never fired once: the resolver was flapping, so an occasional lucky
# getent kept zeroing the counter while every yt-dlp call still failed. A
# ratio over a rolling window cannot be defeated that way — intermittent
# success no longer erases the history of failure.
#
# WHY THE REBOOT IS RATE-LIMITED:
# Rebooting cannot fix an upstream outage (dead router, ISP down). Under the
# old logic a genuine 6-hour outage would have produced ~17 pointless reboots,
# each one costing a minute of capture and risking a bad shutdown. last_reboot
# lives in /var/lib so it survives the reboot it is meant to throttle.
#
# The reseed step exists because of tbb-test, 2026-07-23: both NetworkManager
# connection profiles were truncated to 0 bytes at 07:40 UTC and the unit then
# booted without wifi for six days. It kept recording the whole time, so nothing
# local looked wrong — it was simply unreachable. The old watchdog could only
# cycle the radio and reboot, neither of which can recreate a deleted profile.
set -u

# Any one of these resolving counts as success, so a single host being pulled
# from DNS can never be mistaken for the resolver being down.
PROBE_HOSTS="${PROBE_HOSTS:-www.youtube.com cloudflare.com}"
WINDOW="${WINDOW:-30}"                  # probes retained (= minutes at 1/min)
RESEED_AT="${RESEED_AT:-3}"             # failures in window
WIFI_CYCLE_AT="${WIFI_CYCLE_AT:-5}"     # failures in window
REBOOT_AT="${REBOOT_AT:-20}"            # failures in window
WIFI_CYCLE_COOLDOWN_S="${WIFI_CYCLE_COOLDOWN_S:-600}"     # 10 min
REBOOT_MIN_INTERVAL_S="${REBOOT_MIN_INTERVAL_S:-21600}"   # 6 h

STATE_DIR="${STATE_DIR:-/var/lib/birdbrain-net-watchdog}"
HISTORY_FILE="${HISTORY_FILE:-$STATE_DIR/history}"
LAST_CYCLE_FILE="${LAST_CYCLE_FILE:-$STATE_DIR/last_cycle}"
LAST_REBOOT_FILE="${LAST_REBOOT_FILE:-$STATE_DIR/last_reboot}"

SEED_FILE="${SEED_FILE:-/boot/firmware/network-config}"
KEYFILE="${KEYFILE:-/etc/NetworkManager/system-connections/preconfigured.nmconnection}"
# Every nmcli call goes through this one absolute path, so a test harness can
# point it at a stub and no code path can escape to the real radio.
NMCLI="${NMCLI:-/usr/bin/nmcli}"
SYSTEMCTL="${SYSTEMCTL:-/usr/bin/systemctl}"
GETENT="${GETENT:-/usr/bin/getent}"

mkdir -p "$STATE_DIR"

now=$(date +%s)

# Read an epoch stamp, treating a missing/garbage file as "never".
read_stamp() {
    local v
    v=$(cat "$1" 2>/dev/null) || return 1
    case "$v" in (*[!0-9]*|'') return 1 ;; esac
    printf '%s\n' "$v"
}

probe_ok() {
    local h
    for h in $PROBE_HOSTS; do
        if "$GETENT" hosts "$h" >/dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}

if probe_ok; then result=0; else result=1; fi

# Append this probe and keep only the newest WINDOW samples.
history=$(cat "$HISTORY_FILE" 2>/dev/null | tr -cd '01')
history="${history}${result}"
if [ "${#history}" -gt "$WINDOW" ]; then
    history="${history: -$WINDOW}"
fi
printf '%s\n' "$history" > "$HISTORY_FILE"

fails=$(printf '%s' "$history" | tr -cd '1' | wc -c)
fails=$((fails))

# Healthy and nothing pending — stay quiet so the journal is not spammed.
if [ "$result" -eq 0 ] && [ "$fails" -eq 0 ]; then
    exit 0
fi

logger -t birdbrain-net-watchdog \
    "DNS probe result=$result ($fails/${#history} failed in window)"

[ "$fails" -eq 0 ] && exit 0

# Does NetworkManager know about any wifi connection at all?
have_wifi_profile() {
    "$NMCLI" -t -f TYPE connection show 2>/dev/null \
        | grep -q '^802-11-wireless$'
}

# Rewrite a wifi keyfile from the cloud-init seed that rpi-imager wrote to the
# boot partition. cloud-init itself will not redo this: the instance-id never
# changes, so it considers network config already applied.
reseed_wifi() {
    [ -r "$SEED_FILE" ] || { logger -t birdbrain-net-watchdog "reseed: $SEED_FILE unreadable"; return 1; }

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
    ) || { logger -t birdbrain-net-watchdog "reseed: could not parse $SEED_FILE"; return 1; }

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

    logger -t birdbrain-net-watchdog "reseed: wrote wifi profile for SSID '$ssid' from $SEED_FILE"
    "$NMCLI" connection reload >/dev/null 2>&1 || true
    "$NMCLI" connection up preconfigured >/dev/null 2>&1 || true
    return 0
}

if [ "$fails" -ge "$REBOOT_AT" ]; then
    last_reboot=$(read_stamp "$LAST_REBOOT_FILE") || last_reboot=0
    since=$(( now - last_reboot ))
    if [ "$since" -ge "$REBOOT_MIN_INTERVAL_S" ]; then
        logger -t birdbrain-net-watchdog \
            "rebooting ($fails/${#history} probes failed in window)"
        printf '%s\n' "$now" > "$LAST_REBOOT_FILE"
        : > "$HISTORY_FILE"     # do not re-trigger on the stale window after boot
        sync
        "$SYSTEMCTL" reboot
        exit 0
    fi
    logger -t birdbrain-net-watchdog \
        "reboot threshold met ($fails/${#history}) but last reboot was ${since}s ago (<${REBOOT_MIN_INTERVAL_S}s) — probably upstream, not us; not rebooting"
fi

# Check for a missing profile before falling back to the blunter remedies — a
# reseed fixes the one failure mode the others provably cannot.
if [ "$fails" -ge "$RESEED_AT" ] && ! have_wifi_profile; then
    logger -t birdbrain-net-watchdog "no wifi profile known to NetworkManager — reseeding"
    if reseed_wifi; then
        : > "$HISTORY_FILE"     # give the new profile a clean run at it
        exit 0
    fi
    logger -t birdbrain-net-watchdog "reseed failed — falling through to radio cycle/reboot"
fi

if [ "$fails" -ge "$WIFI_CYCLE_AT" ]; then
    last_cycle=$(read_stamp "$LAST_CYCLE_FILE") || last_cycle=0
    if [ $(( now - last_cycle )) -ge "$WIFI_CYCLE_COOLDOWN_S" ]; then
        logger -t birdbrain-net-watchdog \
            "cycling wifi ($fails/${#history} probes failed in window)"
        printf '%s\n' "$now" > "$LAST_CYCLE_FILE"
        "$NMCLI" radio wifi off
        sleep 2
        "$NMCLI" radio wifi on
    fi
fi
