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
    # Audio stays on the unit; central fetches it on demand if it wants it.
    has_clip: bool = False


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
