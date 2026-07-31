"""Raspberry Pi / Linux host health metrics.

Read straight from ``/proc``, ``/sys`` and ``vcgencmd`` — no third-party deps.
Every probe degrades gracefully (returns ``None``) when a file or command is
missing, so the dashboard renders fine on a dev laptop or any non-Pi host.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
from pathlib import Path

# Bit flags returned by `vcgencmd get_throttled`. The low bits are "happening
# right now"; the high bits latch "has happened since boot".
_THROTTLE_BITS = {
    "under_voltage_now": 0x1,
    "freq_capped_now": 0x2,
    "throttled_now": 0x4,
    "soft_temp_limit_now": 0x8,
    "under_voltage_since_boot": 0x10000,
    "freq_capped_since_boot": 0x20000,
    "throttled_since_boot": 0x40000,
    "soft_temp_limit_since_boot": 0x80000,
}


def cpu_temp_c() -> float | None:
    """SoC temperature in °C. Prefers the sysfs thermal zone (no subprocess);
    falls back to ``vcgencmd measure_temp``."""
    try:
        milli = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
        if milli > 0:
            return round(milli / 1000.0, 1)
    except (OSError, ValueError):
        pass
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        m = re.search(r"temp=([\d.]+)", out.stdout)
        if m:
            return round(float(m.group(1)), 1)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def throttled_flags() -> dict | None:
    """Decode ``vcgencmd get_throttled`` into named booleans (+ raw int), or
    None when vcgencmd isn't available. ``*_now`` = currently happening;
    ``*_since_boot`` = latched since power-on. All-clear is every flag False."""
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"throttled=0x([0-9a-fA-F]+)", out.stdout)
    if not m:
        return None
    bits = int(m.group(1), 16)
    flags = {name: bool(bits & mask) for name, mask in _THROTTLE_BITS.items()}
    flags["raw"] = bits
    return flags


def _meminfo() -> tuple[int | None, int | None]:
    """(total_bytes, available_bytes) from /proc/meminfo, or (None, None)."""
    try:
        fields: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                fields[key.strip()] = int(parts[0]) * 1024  # kB → bytes
        return fields.get("MemTotal"), fields.get("MemAvailable")
    except (OSError, ValueError):
        return None, None


def host_metrics() -> dict:
    """Snapshot of host health: CPU load + core count, memory, SoC temperature,
    throttling/under-voltage flags, and uptime. Missing values are None."""
    load1 = load5 = load15 = None
    # getloadavg isn't available on every platform; leave the Nones if so.
    with contextlib.suppress(OSError, AttributeError):
        load1, load5, load15 = os.getloadavg()

    uptime_s: float | None = None
    with contextlib.suppress(OSError, ValueError):
        uptime_s = float(Path("/proc/uptime").read_text().split()[0])

    mem_total, mem_available = _meminfo()
    return {
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "cpus": os.cpu_count(),
        "mem_total": mem_total,
        "mem_available": mem_available,
        "temp_c": cpu_temp_c(),
        "throttled": throttled_flags(),
        "uptime_s": uptime_s,
    }
