"""Tables a TinyBirdBrain capture unit needs — and only those.

Split out of ``models.py`` because a unit carries 8 of the 23 tables central
defines. The rest are central's alone: users, devices, claim codes, weather,
site and species notes, anomalies, page views, playback state. A microphone in
a field has no use for any of them, and the standalone unit repo should not
have to explain why they are in its schema.

The split is by *ownership*, not by convenience — everything here is written by
the capture loop, its sync agents, or the unit's own web UI. Central imports all
of it back through ``models.py``, so nothing changes on that side.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DetectionRow(Base):
    __tablename__ = "detections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_s: Mapped[float] = mapped_column(Float)
    scientific_name: Mapped[str] = mapped_column(String(256), index=True)
    common_name: Mapped[str] = mapped_column(String(256))
    confidence: Mapped[float] = mapped_column(Float)
    clip_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # For push-fed (TBB) detections: the unit's "<unit_id>:<local_id>" client id
    # (the unit's own detection id), so central can fetch the clip/spectrogram
    # from the unit on demand. NULL for locally-captured detections.
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Site that the source camera was pointed at when this detection happened.
    # NULL for single-site sources or when the resolver couldn't determine it.
    site: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Manual audition label: 'good' | 'bad' | 'unsure' | NULL (unreviewed).
    label: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When label='bad', the rater can suggest what the call actually was.
    # Free-text (auto-completed from species we've already detected). NULL
    # whenever label is anything other than 'bad'.
    suggested_species: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Manual sound-quality rating from the review popup: 1 (faint/noisy) – 5
    # (crisp). Independent of ``label`` — a correct ID can still sound poor.
    # NULL when the rater didn't give one.
    sound_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Perceptual fingerprint of the saved clip (see birdbrain.audio_hash). Lets
    # us detect when a YouTube source replays a highlight reel or airs the
    # same advertisement: two replays produce the same hash, so we hide all
    # but the first occurrence on the same source. NULL = not yet hashed
    # (legacy rows before the backfill ran, or clip missing on disk).
    audio_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    __table_args__ = (
        Index("ix_detections_source_time", "source_name", "started_at"),
        # Covering index for the replay predicate. The NOT EXISTS subquery
        # filters on (source_name=, audio_hash=, started_at<) — matching
        # exactly the column order here lets SQLite resolve it entirely
        # inside the index, no table touch. Replaces the earlier 2-col
        # ix_detections_source_hash, which the planner kept refusing in
        # favour of ix_detections_source_time because of the started_at
        # inequality (turning a ~ms lookup into a multi-second per-row
        # scan). With ANALYZE seeded at boot, the planner picks this
        # consistently.
        Index(
            "ix_detections_source_hash_time",
            "source_name",
            "audio_hash",
            "started_at",
        ),
    )


class WorkerHeartbeatRow(Base):
    """Liveness signal written by each per-source worker thread. The web
    /admin page reads this to tell which sources are actually producing data
    right now (vs configured but stopped/dead)."""

    __tablename__ = "worker_heartbeats"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    # When the current worker incarnation started — bumped each time a new
    # thread takes over for this source.
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Touched ~every 15s by the worker while it's processing chunks.
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 'running' | 'backoff' (between retries) | 'stopped'
    state: Mapped[str] = mapped_column(String(16), default="running")
    # Last error message that triggered backoff (truncated). NULL when healthy.
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)


class WorkerDowntimeRow(Base):
    """One row per outage interval per source. Opened when a worker enters
    a non-running state (backoff / stale / stopped) from running; closed when
    the worker reports running again. ``ended_at`` is NULL while the outage is
    ongoing. Reason captures the first error message that triggered the open.

    Lets the admin/site pages answer 'how much downtime today' and 'why was
    this site silent' — both questions the point-in-time worker_heartbeats
    table can't answer.
    """

    __tablename__ = "worker_downtime"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)


class SpeciesSuppressionRow(Base):
    """A per-site false-positive suppression rule: drop detections of
    ``scientific_name`` at ``source_name`` in the pipeline. For the site-locked
    high-confidence phantoms (e.g. "Grey Plover @ Tau") that a coarse range
    filter can't veto. The worker polls these on its 60s species-floor cadence,
    so adding a rule on /admin takes effect within a minute, no restart; deleting
    the row re-enables the species at that site. Composite key = (site, species).
    """

    __tablename__ = "species_suppressions"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    scientific_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    common_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AudioQualityMetricRow(Base):
    """Current audio-quality snapshot per source, written by the pipeline
    (~every 60s) and read by /admin and the site page. Pure acoustic — how
    usable the feed's audio is for detection — see birdbrain.audio.quality.
    ``updated_at`` doubles as a freshness signal: a stale row means the worker
    isn't running, so the UI shows it greyed rather than current."""

    __tablename__ = "audio_quality_metrics"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    score: Mapped[int] = mapped_column(Integer)            # 0..100 composite
    level_score: Mapped[float] = mapped_column(Float)      # sub-scores, 0..1
    avail_score: Mapped[float] = mapped_column(Float)
    structure_score: Mapped[float] = mapped_column(Float)
    level_dbfs: Mapped[float] = mapped_column(Float)       # EMA RMS level
    silence_fraction: Mapped[float] = mapped_column(Float)
    clip_fraction: Mapped[float] = mapped_column(Float)
    flatness: Mapped[float] = mapped_column(Float)         # raw diagnostic
    fraction_good: Mapped[float] = mapped_column(Float)
    issue_label: Mapped[str] = mapped_column(String(32))   # dominant issue
    # Effective frequency band (Hz) the feed carries — a sharp upper edge well
    # below the ~16 kHz YouTube codec ceiling means a band-limited mic/source.
    # NULL when too quiet to measure.
    band_hz_low: Mapped[int | None] = mapped_column(Integer, nullable=True)
    band_hz_high: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AudioQualitySampleRow(Base):
    """Time-series of the audio-quality score per source (one sample every
    ~10 min) for the 24h trend sparkline on the site page. Pruned to ~7 days."""

    __tablename__ = "audio_quality_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(128), index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    score: Mapped[int] = mapped_column(Integer)
    level_dbfs: Mapped[float] = mapped_column(Float)
    structure_score: Mapped[float] = mapped_column(Float)

    __table_args__ = (
        Index("ix_audio_quality_samples_src_time", "source_name", "recorded_at"),
    )


class AppSettingRow(Base):
    """Key-value store for app-wide tunables that BOTH the web process (the
    writer) and the pipeline workers (readers, via polling) need to agree on
    without a restart. Cross-process coordination is the shared DB, same as
    runtime_sources / source_state. Values are stored as text and parsed by
    the typed accessors on :class:`Database`; an absent row means "unset".

    Current keys:
      - ``global_min_confidence``: site-wide detection floor that overrides
        every source's own ``min_confidence`` when present.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
