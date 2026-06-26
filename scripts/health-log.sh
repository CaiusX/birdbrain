#!/usr/bin/env bash
# Lightweight resource sampler for diagnosing TBB unit hangs. Appends one line
# per minute to data/health.log (run by health-log.timer). After a hang, read
# the lines just before the gap to see what climbed (memory, swap, load, temp,
# throttling) — the 512 MB Zero 2 W can swap-thrash or under-volt into a stall.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/data/health.log"
mkdir -p "$ROOT/data"

mem=$(free -m | awk '/^Mem:/{printf "free=%s avail=%s",$4,$7} /^Swap:/{printf " swap=%s",$3}')
load=$(cut -d' ' -f1-3 /proc/loadavg)
temp=$(vcgencmd measure_temp 2>/dev/null | sed 's/temp=//')
thr=$(vcgencmd get_throttled 2>/dev/null | sed 's/throttled=//')
top=$(ps -eo rss,comm --sort=-rss --no-headers 2>/dev/null | head -1 | awk '{printf "%dMB:%s",$1/1024,$2}')
printf '%s memMB %s load=%s temp=%s thr=%s top=%s\n' \
  "$(date '+%F %T')" "$mem" "$load" "$temp" "$thr" "$top" >> "$LOG"

# Keep the log bounded so it can't fill the SD card.
if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 10000 ]; then
  tail -5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
