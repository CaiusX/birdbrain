"""The unit → central wire format. This module *is* the contract.

A unit and central were built from one codebase and shared ``models.py``, so the
ingest payload never needed writing down: whatever the unit had, central had.
That stops being true the moment the unit becomes its own repository. From then
on the only thing the two agree on is what crosses the wire, so it gets a
version, a single definition, and rules for what happens when they disagree.

Both sides import from here. On extraction this file is copied, not shared —
a unit that cannot reach central for a week must not also be unable to build.

## Versioning

``schema`` is a **major** version and it only moves for a *breaking* change: a
field removed, renamed, or given a new meaning. Adding a field is not breaking,
because both sides ignore what they do not recognise (pydantic's default), and
that tolerance is load-bearing — it is what lets a unit and central update in
either order, which they will, because units update on a daily timer and can be
offline for weeks.

Central accepts any version in :data:`SUPPORTED_SCHEMAS` and rejects the rest.
Rejecting is a real cost — the unit's high-water mark cannot advance, so its
backlog stalls — and that is the intended behaviour for a genuinely
incompatible payload: better a loud stall the operator can see on the unit's
own page than silently mis-parsed detections in the database. It is also why
the unit treats a schema rejection differently from a network failure: retrying
a 400 forever would just hide it (see ``tbb_sync.post_batch``).

## The payload

    {
      "unit":     "tbb-a1b2",        # must match the bearer token's unit
      "schema":   1,
      "timezone": "Africa/Johannesburg" | null,
      "audio_quality": {...} | null,  # the unit measures its own mic
      "detections": [ {...}, ... ]    # may be empty: a keep-alive
    }

Detections are deduplicated by central on the natural key
``(source_name, started_at, scientific_name)``, so resending a batch is safe and
the unit can retry freely. ``client_id`` rides along as a stable per-row
identity for tracing a row back to the unit that produced it.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

#: The version this build speaks. Bump ONLY for a breaking change.
SCHEMA_VERSION = 1

#: HTTP status central answers with when it cannot parse the wire version.
#: 409 Conflict, deliberately not 400: a unit has to be able to tell "we
#: disagree about the format" (permanent, stop and report) from "that payload
#: was malformed" (a bug, and retrying might help).
SCHEMA_CONFLICT_STATUS = 409

#: Versions central will accept. Keep older entries here for as long as any
#: unit in the field might still be running them — a unit that has been in a
#: tree for six months updates to whatever it can reach, when it can reach it.
SUPPORTED_SCHEMAS = frozenset({1})


class WireDetection(BaseModel):
    """One detection as it crosses the wire.

    Deliberately not ``DetectionRow``: central's table carries labels, scores,
    site resolution, audio hashes and media URLs, none of which a unit produces.
    """

    client_id: str | None = None
    started_at: datetime
    duration_s: float = 3.0
    scientific_name: str = Field(min_length=1, max_length=256)
    common_name: str = Field(default="", max_length=256)
    confidence: float = Field(ge=0.0, le=1.0)
    # Whether the unit holds audio for this row. A TBB unit keeps it and
    # central fetches on demand; an ingest node pushes it afterwards over
    # /ingest/clips (WireClipManifest).
    has_clip: bool = False


class WireClip(BaseModel):
    """One clip file in a ``/ingest/clips`` upload, as described by its
    manifest entry.

    A clip is shared by every detection BirdNET made in the same 3 s window, so
    one file can carry several ``client_ids``. ``part`` names the multipart
    field holding the bytes; the sender picks it, central never uses it as a
    filename. ``fmt`` is the container the bytes are in — central stores the
    file under its own name with this extension.
    """

    part: str = Field(min_length=1, max_length=64)
    client_ids: list[str] = Field(min_length=1, max_length=64)
    fmt: str = Field(default="ogg", pattern=r"^(ogg|wav|flac|mp3)$")


class WireClipManifest(BaseModel):
    """The JSON ``manifest`` field of a ``/ingest/clips`` upload."""

    unit: str = Field(min_length=1, max_length=64)
    schema_version: int = Field(alias="schema", default=SCHEMA_VERSION)
    clips: list[WireClip] = Field(default_factory=list, max_length=200)

    model_config = {"populate_by_name": True}


class WireHostMetrics(BaseModel):
    """``birdbrain.host.host_metrics()`` as a node reports it. Every field is
    optional: a probe that fails on the node is None here, and the pane shows
    n/a for it, the same as central's own card does."""

    load1: float | None = Field(default=None, ge=0, le=10_000)
    load5: float | None = Field(default=None, ge=0, le=10_000)
    load15: float | None = Field(default=None, ge=0, le=10_000)
    cpus: int | None = Field(default=None, ge=1, le=4096)
    mem_total: int | None = Field(default=None, ge=0)
    mem_available: int | None = Field(default=None, ge=0)
    temp_c: float | None = Field(default=None, ge=-50, le=200)
    throttled: dict[str, bool] | None = None
    uptime_s: float | None = Field(default=None, ge=0)


class WireDiskUsage(BaseModel):
    total: int = Field(ge=0)
    used: int = Field(ge=0)
    free: int = Field(ge=0)


class WireWorkerProblem(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=32)
    error: str | None = Field(default=None, max_length=300)


class WireNodeHealth(BaseModel):
    """One POST to ``/ingest/node-health``: an ingest node's own host and
    pipeline state, so central's admin page can show a node the way it shows
    itself. Sent every ``health_seconds`` by ``node_sync``; central keeps only
    the latest report per node and flags it stale after a few minutes."""

    model_config = {"populate_by_name": True}

    node: str = Field(min_length=1, max_length=64)
    schema_version: int = Field(default=SCHEMA_VERSION, alias="schema")
    reported_at: datetime
    version: str | None = Field(default=None, max_length=64)
    host: WireHostMetrics = Field(default_factory=WireHostMetrics)
    disk: WireDiskUsage | None = None
    db_bytes: int | None = Field(default=None, ge=0)
    workers_running: int = Field(default=0, ge=0, le=10_000)
    workers_total: int = Field(default=0, ge=0, le=10_000)
    worker_problems: list[WireWorkerProblem] = Field(default_factory=list, max_length=500)
    #: rows the node has not yet pushed to central, summed over its links
    rows_behind: int = Field(default=0, ge=0)
    #: rows whose clip has not yet followed, summed over its links
    clips_behind: int = Field(default=0, ge=0)
    last_detection_age_s: float | None = Field(default=None, ge=0)
    det_24h: int = Field(default=0, ge=0)


class WireAudioQuality(BaseModel):
    """A unit's own audio-quality snapshot, riding along with the batch.

    A push-fed unit keeps its audio locally, so central can never measure the
    feed itself — the unit's pipeline already computes this from the raw mic
    stream, so it reports it instead. Field-for-field the dict
    ``QualityAccumulator.snapshot()`` returns. Every bound is enforced here
    because this arrives over the public tunnel: a buggy or hostile unit may
    only write nonsense about *itself*, never a value that breaks the /admin
    and site-page rendering that reads these columns.
    """

    score: int = Field(ge=0, le=100)
    level_score: float = Field(ge=0.0, le=1.0)
    avail_score: float = Field(ge=0.0, le=1.0)
    structure_score: float = Field(ge=0.0, le=1.0)
    # dBFS is negative (0 = full scale). Bounded loosely — silence floors well
    # below -100 on a quiet mic, and a tiny positive overshoot is possible.
    level_dbfs: float = Field(ge=-200.0, le=20.0)
    silence_fraction: float = Field(ge=0.0, le=1.0)
    clip_fraction: float = Field(ge=0.0, le=1.0)
    flatness: float = Field(ge=0.0, le=1.0)
    fraction_good: float = Field(ge=0.0, le=1.0)
    issue_label: str = Field(default="", max_length=32)
    # NULL when the feed was too quiet to measure a band edge.
    band_hz_low: int | None = Field(default=None, ge=0, le=1_000_000)
    band_hz_high: int | None = Field(default=None, ge=0, le=1_000_000)


class WireBatch(BaseModel):
    """One POST to ``/ingest/detections``."""

    # max_length caps the body so a single POST can't be unbounded.
    model_config = {"populate_by_name": True}

    unit: str = Field(min_length=1, max_length=64)
    schema_version: int = Field(default=SCHEMA_VERSION, alias="schema")
    # IANA tz the unit reports (e.g. "Africa/Johannesburg"). Used only to set a
    # new unit's source timezone at first registration; None = leave at UTC.
    timezone: str | None = Field(default=None, max_length=64)
    detections: list[WireDetection] = Field(default_factory=list, max_length=2000)
    # Optional so an older unit (or one whose accumulator hasn't warmed up yet)
    # still ingests normally — absent just means "no quality update this tick".
    audio_quality: WireAudioQuality | None = None


class UnsupportedSchemaError(ValueError):
    """The payload declares a version this build cannot parse safely."""

    def __init__(self, got: int) -> None:
        self.got = got
        super().__init__(
            f"unsupported schema {got}; this build speaks "
            f"{sorted(SUPPORTED_SCHEMAS)}. Update the other side."
        )


def check_schema(version: int) -> None:
    """Raise :class:`UnsupportedSchemaError` if we cannot parse this payload.

    Called before anything is written. An unknown version means the sender may
    have changed what a field *means*, and guessing would put wrong data in the
    database — which is worse than a stalled backlog, because the stall is
    visible and the wrong data is not.
    """
    if version not in SUPPORTED_SCHEMAS:
        raise UnsupportedSchemaError(version)
