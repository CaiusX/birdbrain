#!/usr/bin/env bash
# apply-net-fixes.sh — the four fixes from the 2026-07-29 crash investigation.
#
# Both "crashes" (2026-07-26 6h15m outage, 2026-07-29 reboot) were wifi DNS
# failures, not hardware. This installs:
#   1. persistent journald   — so the next incident leaves evidence behind
#   2. wifi power-save off   — removes a known source of intermittent DNS loss
#   3. fallback DNS servers  — router is no longer a single point of failure
#   4. ratio-based watchdog  — fires on flapping DNS; reboot is rate-limited
#
# Idempotent: safe to re-run. Run with sudo from the repo root.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root: sudo $0" >&2
    exit 1
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
echo "repo: $REPO"

# install SRC DST MODE — copy only if changed, and verify it landed intact.
install_verified() {
    local src="$1" dst="$2" mode="$3"
    [ -s "$src" ] || { echo "  ERROR: $src missing or empty"; return 1; }
    mkdir -p "$(dirname "$dst")"
    install -m "$mode" -o root -g root "$src" "$dst"
    cmp -s "$src" "$dst" || { echo "  ERROR: $dst does not match $src after install"; return 1; }
    return 0
}

echo
echo "=== 1/4  persistent journal ==="
install_verified scripts/journald-persistent.conf \
    /etc/systemd/journald.conf.d/00-africam-persistent.conf 0644
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
systemctl restart systemd-journald
sleep 1
if journalctl --header 2>/dev/null | grep -q "/var/log/journal"; then
    echo "  journal now persistent: $(journalctl --disk-usage 2>/dev/null)"
else
    echo "  WARNING: journal still volatile — check journalctl --header"
fi

echo
echo "=== 2/4  wifi power-save off ==="
install_verified scripts/wifi-powersave-off.conf \
    /etc/NetworkManager/conf.d/wifi-powersave-off.conf 0644
echo "  installed (takes effect on reconnect, below)"

echo
echo "=== 3/4  fallback DNS ==="
CON="$(nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2=="802-11-wireless"{print $1; exit}')"
if [ -z "${CON:-}" ]; then
    echo "  WARNING: no active wifi connection found — skipping DNS change"
else
    DEV="$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2=="wifi"{print $1; exit}')"
    GW="$(nmcli -g IP4.GATEWAY device show "$DEV" | head -1)"
    [ -n "${GW:-}" ] || GW="$(ip -4 route show default | awk '{print $3; exit}')"
    echo "  connection='$CON' device='$DEV' gateway='$GW'"
    echo "  before: $(grep -c '^nameserver' /etc/resolv.conf) nameserver(s)"
    # Router first (fast, resolves LAN), then two public resolvers. glibc only
    # consults the first 3, so keep the list at exactly 3 and drop the
    # auto/IPv6 ones rather than letting them crowd the fallbacks out.
    nmcli connection modify "$CON" \
        ipv4.dns "${GW:-192.168.3.1},1.1.1.1,8.8.8.8" \
        ipv4.ignore-auto-dns yes \
        ipv6.ignore-auto-dns yes
    echo "  reconnecting '$CON' (brief wifi blip — also applies power-save)..."
    nmcli device reapply "$DEV" >/dev/null 2>&1 || nmcli connection up "$CON" >/dev/null 2>&1 || true
    sleep 3
    nmcli connection up "$CON" >/dev/null 2>&1 || true
    sleep 2
    echo "  after:"
    grep '^nameserver' /etc/resolv.conf | sed 's/^/     /'
fi

echo
echo "=== 4/4  ratio-based net-watchdog ==="
install_verified scripts/africam-net-watchdog.sh /usr/local/bin/africam-net-watchdog 0755
install_verified scripts/africam-net-watchdog.service \
    /etc/systemd/system/africam-net-watchdog.service 0644
install_verified scripts/africam-net-watchdog.timer \
    /etc/systemd/system/africam-net-watchdog.timer 0644
# Old consecutive-counter state; the new script keeps a rolling history instead.
rm -f /var/lib/africam-net-watchdog/fail_count
systemctl daemon-reload
systemctl enable --now africam-net-watchdog.timer >/dev/null 2>&1 || true
echo "  smoke test..."
if /usr/local/bin/africam-net-watchdog; then
    echo "  watchdog ran clean; timer=$(systemctl is-active africam-net-watchdog.timer)"
    echo "  history=$(cat /var/lib/africam-net-watchdog/history 2>/dev/null || echo '(none)')"
else
    echo "  WARNING: watchdog exited non-zero"
fi

echo
echo "=== verification ==="
echo "  power-save : $(iw dev "${DEV:-wlan0}" get power_save 2>/dev/null || echo '?')"
echo "  resolvers  : $(grep -c '^nameserver' /etc/resolv.conf)"
echo "  journal    : $(journalctl --header 2>/dev/null | grep -m1 'File path' || echo '?')"
echo "  timer      : $(systemctl is-active africam-net-watchdog.timer)"
echo
echo "done."
