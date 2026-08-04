"""Live, in-memory view of where each sync target stands.

Both agents run as threads inside ``tbb-web``, so the page and ``/healthz`` can
read what they publish here directly. Deliberately **not** persisted: a unit's
whole problem is that it writes to an SD card, and "when did we last talk to the
cloud" is worth exactly nothing after a reboot.

This exists because the unit's own page could not tell the truth about itself.
``_sync_state`` was hardcoded to return "offline" whenever sync was enabled — a
leftover from before Phase 2 shipped — so a unit that had been syncing happily
for weeks still showed an offline dot to whoever walked up to it.
"""
from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

# Spread of the sync intervals, as a fraction either side of the nominal value.
_JITTER = 0.15


def jittered(seconds: float) -> float:
    """Interval with a little spread, so a fleet does not phase-lock.

    Units are flashed from one image and powered up together, so without this
    every unit on a site hits central and the cloud in the same second, forever.
    The update timer already does this (``RandomizedDelaySec=1h``); the sync
    loops did not.
    """
    return seconds * random.uniform(1.0 - _JITTER, 1.0 + _JITTER)


# How long after a successful exchange a target still counts as "synced". Needs
# to outlast the slowest configured interval (the field profile polls every
# 300s) or a perfectly healthy metered unit would flap to "offline" between
# ticks.
FRESH_AFTER_S = 900.0


@dataclass
class TargetStatus:
    """One sync destination, as last reported by its agent."""

    name: str
    enabled: bool = False
    blocked: str | None = None          # set when the agent refused to start
    last_success_at: datetime | None = None
    last_error: str | None = None
    queue_depth: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def succeeded(self, queue_depth: int = 0) -> None:
        with self._lock:
            self.last_success_at = datetime.now(UTC)
            self.last_error = None
            self.queue_depth = queue_depth

    def failed(self, error: str, queue_depth: int | None = None) -> None:
        with self._lock:
            self.last_error = error[:200]
            if queue_depth is not None:
                self.queue_depth = queue_depth

    def block(self, reason: str) -> None:
        """The agent will not run at all — e.g. an unreadable state file."""
        with self._lock:
            self.blocked = reason

    @property
    def state(self) -> str:
        """``synced`` | ``offline`` | ``blocked`` | ``disabled``.

        "offline" covers both never-connected and gone-stale, which is what
        someone standing next to the unit actually needs to know.
        """
        if self.blocked:
            return "blocked"
        if not self.enabled:
            return "disabled"
        if self.last_success_at is None:
            return "offline"
        age = (datetime.now(UTC) - self.last_success_at).total_seconds()
        return "synced" if age <= FRESH_AFTER_S else "offline"

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "enabled": self.enabled,
            "blocked": self.blocked,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_error": self.last_error,
            "queue_depth": self.queue_depth,
        }


class SyncStatus:
    """The unit's two targets. BirdNET-Cloud first — it is the principal
    consumer, and central sync is the option you enable when you run one."""

    def __init__(self) -> None:
        self.cloud = TargetStatus("birdnetcloud")
        self.central = TargetStatus("central")

    def as_dict(self) -> dict:
        return {"cloud": self.cloud.as_dict(), "central": self.central.as_dict()}


# Process-wide singleton. The agents are started by create_tbb_app in the same
# process that serves the pages, so a module global is the whole mechanism.
STATUS = SyncStatus()
