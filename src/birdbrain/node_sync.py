"""Stream-ingest node → central sync agent.

A second kind of satellite. Where ``tbb_sync`` carries one Pi Zero's *mic* up to
central, this carries a Pi 5's *stream workers* — many of them, each a different
place on the map.

## Why this exists rather than a second ``tbb_sync``

TBB's shape is one unit, one mic, one coordinate, so its agent syncs the whole
local database under a single ``unit_id`` and never looks at ``source_name``.
Central matches that shape deliberately: ``ingest.ingest_batch`` writes
``source_name=device.unit_id`` with ``device.lat``/``device.lon``, so **a token
authorises exactly one source at exactly one location**.

An ingest node breaks the first assumption and not the second. It runs the
ordinary ``birdbrain run`` supervisor over a ``sources.toml`` of a dozen-odd
YouTube/RTSP cams scattered across the continent, all writing into one local
database and distinguished only by ``DetectionRow.source_name``. Point
``tbb_sync`` at that and every cam's detections arrive under one unit id, at one
lat/lon — Hwange's hornbills plotted on Sabi Sand.

So the node keeps central's one-token-one-source rule and satisfies it the
honest way: **one enrolled device per stream**, and an agent that partitions the
local database by ``source_name`` before it sends. Each cam lands on central as
its own site, with its own coordinates, through the existing ingest endpoint.

The payoff is that central needs no changes at all. The wire format is
unmodified (``wire.SCHEMA_VERSION`` 1), so ``/ingest/detections`` cannot tell an
ingest node from a Pi Zero — auto-registration, the ``external`` flag that stops
central's supervisor from running the cam locally, heartbeat liveness, species
floors and suppressions all apply as they already do. Nothing on the production
box has to move for a node to start reporting.

## What it inherits from tbb_sync

The transport, and for the same reasons: keep-alive on one session, POST a
capped batch, advance a persisted high-water mark **only** on a 2xx, treat a
schema 409 as permanent rather than retryable. Those are imported rather than
re-implemented, so a fix to the wire handling reaches both agents.

What differs is bookkeeping. The mark is per-source (a dict keyed by
``source_name``), because sources drain independently: one cam bot-gated by
YouTube for an hour must not hold back the eleven that are healthy.

Placement: its own process (``birdbrain node-sync``, see
``scripts/birdbrain-node-sync.service``), beside the pipeline rather than inside
it. A node runs no web service to host a background thread the way ``tbb-web``
does for TBB, and keeping sync in a separate unit means a crash-looping agent
can never take the capture workers down with it.
"""
from __future__ import annotations

import threading
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import requests
from pydantic import BaseModel, Field
from sqlalchemy import select

from birdbrain.logging import get_logger
from birdbrain.statefile import StateRead, read_json_state, write_json_atomic
from birdbrain.storage import Database, DetectionRow
from birdbrain.sync_status import jittered
from birdbrain.tbb_sync import post_batch
from birdbrain.wire import SCHEMA_VERSION

log = get_logger(__name__)


class SourceLink(BaseModel):
    """One local source wired to one enrolled device on central.

    ``unit`` is central's ``devices.unit_id``, which is also the ``source_name``
    every row of this link lands under — so it, not the local ``source``, is
    what a reader sees on the dashboard. They are separate fields because the
    local roster is free to rename a cam without orphaning its central history.
    """

    # Must match a `name` in this node's sources.toml.
    source: str = Field(min_length=1, max_length=128)
    # Central's unit_id for that cam, from `birdbrain tbb-device-add`.
    unit: str = Field(min_length=1, max_length=64)
    # The bearer token minted alongside it. Central stores only a SHA-256 of
    # this, so a lost token cannot be recovered — mint a new device instead.
    token: str = Field(min_length=1)


class NodeSyncConfig(BaseModel):
    """``node.toml`` — the roster of links plus transport knobs.

    Deliberately its own file rather than fields on ``AppConfig``: it carries
    bearer tokens, and it is the one piece of node configuration that must never
    be readable from the dashboard or land in the repo. ``node.example.toml``
    documents the shape; ``node.toml`` is gitignored.
    """

    # Central's base URL. On a LAN node prefer the private address
    # (http://192.168.x.x:8765) over the public hostname: same endpoint, but the
    # batches stay off the Cloudflare tunnel and skip TLS on a link that is
    # already trusted.
    central_url: str
    interval_seconds: int = Field(default=25, ge=5)
    # Rows per POST. Central caps a body at 2000 detections (wire.WireBatch), so
    # anything above that is rejected wholesale rather than truncated.
    batch_size: int = Field(default=200, ge=1, le=2000)
    # Seconds between empty keep-alive posts for an idle-but-healthy source.
    # 0 disables them, and the source then reads "stale" on central whenever it
    # goes a while without hearing a bird.
    #
    # Must stay comfortably under central's 60s staleness cutoff
    # (`web/app.py::_hb_status`: "running" requires a heartbeat inside 60s), or
    # every quiet cam flaps to stale between birds — which destroys the signal
    # the dashboard exists to give, since a genuinely dead cam then looks
    # exactly like a healthy one at a waterhole where nothing is calling.
    # 20s against a 25s tick (±15% jitter, so ≤29s worst case) heartbeats on
    # every pass with room to spare.
    #
    # tbb_sync defaults to 300 for the opposite reason — a Pi Zero on a metered
    # link, where empty batches cost ~2MB/day of real money. A node talks to
    # central over the LAN, where they cost nothing.
    keepalive_seconds: int = Field(default=20, ge=0)
    # A source whose local worker has not touched its heartbeat within this many
    # seconds is treated as wedged: real backlog still flushes, keep-alives do
    # not. See _worker_is_live.
    worker_stale_seconds: float = Field(default=90.0, gt=0)
    state_file: Path = Path("data/node_sync_state.json")
    links: list[SourceLink] = Field(default_factory=list)


def load_node_config(path: Path) -> NodeSyncConfig:
    """Parse ``node.toml``. Raises FileNotFoundError with the copy-this hint the
    rest of the CLI uses for missing config."""
    if not path.exists():
        raise FileNotFoundError(
            f"Node sync config not found at {path}. "
            "Copy node.example.toml to node.toml and fill in the device tokens."
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)
    links = raw.pop("link", [])
    return NodeSyncConfig.model_validate({**raw, "links": links})


class MarkStore:
    """Per-source high-water marks, persisted as one JSON file.

    A dict rather than ``tbb_sync.SyncState``'s single integer, because each
    link advances on its own schedule. Falling back to an empty dict is safe for
    the same reason a TBB unit can replay from zero: central upserts on the
    natural key and every row carries a stable ``client_id``, so a replay costs
    bandwidth and produces no duplicates.
    """

    def __init__(self, path: Path, marks: dict[str, int] | None = None) -> None:
        self.path = path
        self.marks: dict[str, int] = marks or {}

    @classmethod
    def load(cls, path: Path) -> MarkStore:
        data, outcome = read_json_state(path)
        if data is None:
            if outcome is StateRead.CORRUPT:
                log.warning(
                    "node_sync.state_unreadable",
                    path=str(path),
                    action="replaying from 0 for every source; central dedupes, "
                           "so this costs bandwidth rather than duplicates",
                )
            return cls(path, {})
        if outcome is StateRead.RECOVERED:
            log.warning("node_sync.state_recovered_from_backup", path=str(path))
        marks: dict[str, int] = {}
        for name, value in (data.get("marks") or {}).items():
            try:
                marks[str(name)] = int(value)
            except (TypeError, ValueError):
                # One unparseable entry replays one source, not all of them.
                log.warning("node_sync.mark_unparseable", source=name, value=value)
        return cls(path, marks)

    def get(self, source: str) -> int:
        return self.marks.get(source, 0)

    def set(self, source: str, value: int) -> None:
        self.marks[source] = value

    def save(self) -> None:
        write_json_atomic(self.path, {"marks": self.marks})


def fetch_batch(
    db: Database, source_name: str, since_id: int, limit: int
) -> list[DetectionRow]:
    """This source's detections with id > since_id, oldest first.

    The ``source_name`` predicate is the whole difference from
    ``tbb_sync.fetch_batch``, and it is what keeps a cam's rows inside its own
    link. ``ix_detections_source_time`` covers the filter.
    """
    with db.session() as s:
        return list(
            s.scalars(
                select(DetectionRow)
                .where(DetectionRow.source_name == source_name)
                .where(DetectionRow.id > since_id)
                .order_by(DetectionRow.id.asc())
                .limit(limit)
            )
        )


def detections_payload(link: SourceLink, rows: list[DetectionRow], timezone: str | None) -> dict:
    """Build a ``/ingest/detections`` body for one link.

    Shaped exactly like ``tbb_sync.detections_payload`` because it is the same
    wire contract — central must not be able to distinguish the senders. Two
    fields it does not carry:

    ``audio_quality`` — TBB measures its own mic because central cannot hear it.
    A node's audio is a public stream central could sample itself, and the
    quality accumulator lives in the pipeline process, not this one. Omitted
    rather than faked; the field is optional precisely so a sender without a
    measurement can skip it.

    lat/lon — not in the wire format at all. Central takes them from the
    enrolled device, which is why each cam needs its own device rather than a
    coordinate on the row.
    """
    return {
        "unit": link.unit,
        "schema": SCHEMA_VERSION,
        "timezone": timezone,
        "detections": [
            {
                # Namespaced by unit so two links can never collide on it, and
                # stable across resends so a retry dedupes.
                "client_id": f"{link.unit}:{r.id}",
                "started_at": (r.started_at.isoformat() if r.started_at else None),
                "duration_s": r.duration_s,
                "scientific_name": r.scientific_name,
                "common_name": r.common_name,
                "confidence": r.confidence,
                "has_clip": r.clip_path is not None,
            }
            for r in rows
        ],
    }


def _worker_is_live(db: Database, source_name: str, stale_after_s: float) -> bool:
    """Is the local worker for this source still processing chunks?

    Gates keep-alives only. Central stamps a heartbeat on every ingest including
    an empty one, so a node whose YouTube worker has wedged would keep posting
    "I'm here" and read as healthy on the dashboard while hearing nothing — the
    one failure the dashboard exists to surface, hidden by the keep-alive meant
    to help. Staying quiet instead lets central's own stale-heartbeat logic mark
    the source offline. Same reasoning as ``tbb_sync``'s ``capture_is_live``,
    against the pipeline's heartbeat rather than a mic's.

    Real backlog is never gated on this: rows captured before a wedge are still
    good, and holding them back would lose data to protect a status light.
    """
    hb = db.get_worker_heartbeat(source_name)
    if hb is None or hb.last_heartbeat_at is None:
        return False
    seen = hb.last_heartbeat_at
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return (datetime.now(UTC) - seen).total_seconds() <= stale_after_s


def sync_link_once(
    db: Database,
    cfg: NodeSyncConfig,
    link: SourceLink,
    marks: MarkStore,
    timezone: str | None = None,
    session: requests.Session | None = None,
) -> int:
    """Drain one link's backlog in capped batches. Returns rows acked.

    Stops at the first failed POST and leaves the mark where it was, so the
    backlog drains on a later tick instead of being skipped. Each batch's mark
    is saved as it is acked rather than once at the end — a node killed
    mid-drain then resumes from the last acked batch rather than replaying the
    whole run.
    """
    sent = 0
    while True:
        rows = fetch_batch(db, link.source, marks.get(link.source), cfg.batch_size)
        if not rows:
            break
        payload = detections_payload(link, rows, timezone)
        if not post_batch(cfg.central_url, link.token, payload, session=session):
            break  # offline / rejected — don't advance; retry next tick
        marks.set(link.source, rows[-1].id)  # rows are id-ascending
        marks.save()
        sent += len(rows)
        if len(rows) < cfg.batch_size:
            break  # fully drained
    return sent


def sync_once(
    db: Database,
    cfg: NodeSyncConfig,
    marks: MarkStore,
    timezones: dict[str, str] | None = None,
    session: requests.Session | None = None,
    keepalive_due: dict[str, float] | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """One pass over every link. Returns ``{source: rows_acked}``.

    Links are independent by construction: a raised exception or a refused POST
    on one is logged and the loop moves on, because a single bot-gated cam must
    not stall the rest of the roster behind it.
    """
    results: dict[str, int] = {}
    for link in cfg.links:
        tz = (timezones or {}).get(link.source)
        try:
            sent = sync_link_once(db, cfg, link, marks, tz, session)
            results[link.source] = sent
            if sent:
                log.info(
                    "node_sync.flushed",
                    source=link.source, unit=link.unit,
                    count=sent, last_synced_id=marks.get(link.source),
                )
            elif keepalive_due is not None and cfg.keepalive_seconds > 0:
                _maybe_keepalive(db, cfg, link, tz, session, keepalive_due, now)
        except Exception:
            results[link.source] = 0
            log.exception("node_sync.link_failed", source=link.source, unit=link.unit)
    return results


def _maybe_keepalive(
    db: Database,
    cfg: NodeSyncConfig,
    link: SourceLink,
    timezone: str | None,
    session: requests.Session | None,
    due_at: dict[str, float],
    now: float | None,
) -> None:
    """Post an empty batch for an idle source, on its own clock and only while
    its worker is demonstrably alive (see ``_worker_is_live``)."""
    stamp = time.monotonic() if now is None else now
    last = due_at.get(link.source)
    if last is not None and stamp - last < cfg.keepalive_seconds:
        return
    if not _worker_is_live(db, link.source, cfg.worker_stale_seconds):
        log.warning(
            "node_sync.keepalive_suppressed",
            source=link.source, unit=link.unit,
            reason="worker heartbeat is stale — letting central see this cam go offline",
        )
        return
    if post_batch(
        cfg.central_url, link.token, detections_payload(link, [], timezone), session=session
    ):
        due_at[link.source] = stamp


def run_node_sync(
    db: Database,
    cfg: NodeSyncConfig,
    stop_event: threading.Event,
    timezones: dict[str, str] | None = None,
) -> None:
    """The service loop. Blocks until ``stop_event`` is set.

    One :class:`requests.Session` per link, not one shared: they authenticate as
    different devices, and a pooled connection carries the ``Authorization``
    header set on the session. Sharing one would send every cam's rows under
    whichever token was installed last — accepted for exactly one link and
    rejected with a unit mismatch for the rest.
    """
    marks = MarkStore.load(cfg.state_file)
    sessions = {
        link.source: _session_for(link.token) for link in cfg.links
    }
    keepalive_due: dict[str, float] = {}
    log.info(
        "node_sync.start",
        central=cfg.central_url,
        links=len(cfg.links),
        interval_seconds=cfg.interval_seconds,
        keepalive_seconds=cfg.keepalive_seconds,
    )
    try:
        while True:
            for link in cfg.links:
                if stop_event.is_set():
                    return
                try:
                    tz = (timezones or {}).get(link.source)
                    sent = sync_link_once(db, cfg, link, marks, tz, sessions[link.source])
                    if sent:
                        log.info(
                            "node_sync.flushed",
                            source=link.source, unit=link.unit,
                            count=sent, last_synced_id=marks.get(link.source),
                        )
                    elif cfg.keepalive_seconds > 0:
                        _maybe_keepalive(
                            db, cfg, link, tz, sessions[link.source], keepalive_due, None
                        )
                except Exception:
                    log.exception(
                        "node_sync.link_failed", source=link.source, unit=link.unit
                    )
            if stop_event.wait(jittered(cfg.interval_seconds)):
                return
    finally:
        for s in sessions.values():
            s.close()


def _session_for(token: str) -> requests.Session:
    """A keep-alive session bound to one device token. Mirrors
    ``tbb_sync.make_session``; kept separate so the per-link binding above is
    explicit rather than implied."""
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s
