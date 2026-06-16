from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, Text
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
    # Perceptual fingerprint of the saved clip (see africam.audio_hash). Lets
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


class RuntimeSourceRow(Base):
    """Sources added at runtime via the web UI. Picked up by the pipeline
    supervisor without a restart. Soft-deleted (deleted_at != NULL) so the
    supervisor can stop the worker without losing history.

    Fields mirror :class:`SourceConfig` (the file-based equivalent) but live
    in SQL so the web app can mutate them without touching files.
    """

    __tablename__ = "runtime_sources"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # "youtube" | "rtsp"
    url: Mapped[str] = mapped_column(String(1024))
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    multisite: Mapped[bool] = mapped_column(default=False)
    cookies_from_browser: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cookies_file: Mapped[str | None] = mapped_column(String(512), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SpeciesNoteRow(Base):
    """A short curated comment about a species — displayed in the audition
    modal whenever a detection of that species is opened. Used to flag known
    false-positive-prone species, biome plausibility, ID confusions, etc.
    Keyed by scientific name since BirdNET reports it consistently."""

    __tablename__ = "species_notes"

    scientific_name: Mapped[str] = mapped_column(String(256), primary_key=True)
    common_name: Mapped[str] = mapped_column(String(256))
    note: Mapped[str] = mapped_column(Text)
    # Optional flag that styles the banner in the modal.
    # 'reliable' (green) | 'suspect' (amber) | 'rare' (blue) | NULL (neutral)
    tag: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # IUCN Red List code (LC/NT/VU/EN/CR/EW/EX/DD/NE), populated from the
    # Wikipedia article's status icon. NULL until first looked up.
    conservation_status: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Per-species detection threshold override. When set, the pipeline drops
    # detections of this species below this confidence even if the source's
    # min_confidence is lower. Lets us suppress loud common species (Egyptian
    # Goose, Hadada Ibis) that otherwise drown out quieter detections.
    min_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Provenance for AI-generated notes. NULL = curated/manual note (or no
    # note yet). When present, the background notes worker is allowed to
    # regenerate; when NULL but note is non-empty, the worker leaves it
    # alone so manual edits stick.
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Hash of the evidence inputs at the time of last generation. The worker
    # uses this to detect when the underlying detection profile has shifted
    # enough to be worth regenerating before the age-based trigger fires.
    evidence_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detection_count_at_gen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Cached Wikipedia media URLs: a representative photo and the natural-range
    # map. Populated lazily on first page view and proactively by the web
    # process's background media sweeper. media_fetched_at stamps the last
    # successful lookup attempt so we don't re-hit Wikipedia for species that
    # simply have no range map, and can refresh periodically. NULL = never tried.
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    image_page_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    range_map_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    range_map_page_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Geo-referenced occurrence tile (GBIF, iNat fallback) for the live Leaflet
    # range map that carries our site dots. Stored separately from the static
    # range_map_url image so a species can show both the IUCN polygon (as a
    # small reference thumbnail) and the dotted occurrence map.
    range_tile_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    range_tile_page_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    media_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


class SiteNoteRow(Base):
    """AI-generated commentary about a site/source as a sonic place — what
    defines its soundscape, its diurnal rhythm, signature species, contrasts
    with other sites. Written by the background notes worker and refreshed
    on a slow cadence (weekly aged, or when detection volume doubles).

    Keyed by source_name to match how detections are grouped today (single-
    site sources only; the `site` column on DetectionRow is always NULL).
    """

    __tablename__ = "site_notes"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    note: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detection_count_at_gen: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SpeciesSiteNoteRow(Base):
    """AI-generated commentary on how one species sounds at one specific site,
    contrasted with its pattern across the network. The cross-product of
    ``species_notes`` (global per species) and ``site_notes`` (global per site).

    Composite primary key (scientific_name, source_name) — one row per pair.
    Stored as a few telegraphic bullets, one per line, mirroring the per-site
    bullets style we use on the daily brief.
    """

    __tablename__ = "species_site_notes"

    scientific_name: Mapped[str] = mapped_column(String(256), primary_key=True)
    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    common_name: Mapped[str] = mapped_column(String(256))
    note: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detection_count_at_gen: Mapped[int | None] = mapped_column(Integer, nullable=True)


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


class SourceDisableRow(Base):
    """Marks a source (by name) as administratively disabled. Used to soft-
    turn-off file-managed sources from sources.toml — runtime sources have
    their own soft-delete via ``runtime_sources.deleted_at``, so for those
    this row is just a redundant signal.

    Keyed by source name. Re-enabling = deleting the row. The supervisor
    polls _all_sources() every 15s; entering or leaving this table starts /
    stops the worker on the next tick without needing a process restart.
    """

    __tablename__ = "source_disable_overrides"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    disabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DailyBriefRow(Base):
    """One-paragraph cross-site digest of a single UTC date. Generated once
    after midnight UTC and never regenerated — the day's data is fixed.
    Surfaces as a banner on the dashboard and an archive list at /briefs."""

    __tablename__ = "daily_briefs"

    date_utc: Mapped[date] = mapped_column(Date, primary_key=True)
    brief_text: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generated_by: Mapped[str] = mapped_column(String(64))
    # Cached aggregates for the archive listing — saves a recount per row.
    total_detections: Mapped[int] = mapped_column(Integer, default=0)
    distinct_species: Mapped[int] = mapped_column(Integer, default=0)
    evidence_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AnomalyEventRow(Base):
    """A notable day at a source — surfaced by the SQL anomaly detectors and
    optionally interpreted by Claude.

    Composite PK is (source_name, date_utc, kind) so the same calendar day
    can carry multiple flavours of anomaly: e.g. a migration wave shows up
    as ``volume_spike``, ``nocturnal_burst`` AND ``new_species_wave`` at
    once — each gets its own row and its own LLM-written interpretation if
    interesting enough to spend a token budget on.

    Detection is cheap (SQL group-bys against ``detections``); interpretation
    is the Claude call. ``interpretation`` starts NULL when a detector fires
    and gets filled in by the notes-worker anomaly tick on the next pass."""

    __tablename__ = "anomaly_events"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    date_utc: Mapped[date] = mapped_column(Date, primary_key=True)
    # 'volume_spike' | 'nocturnal_burst' | 'new_species_wave' (extensible).
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Raw counts that triggered the rule.
    detection_count: Mapped[int] = mapped_column(Integer)
    baseline_count: Mapped[float | None] = mapped_column(Float, nullable=True)
    # n/baseline (volume_spike), night/day (nocturnal_burst), or new-species
    # count (new_species_wave) — interpretation depends on ``kind``.
    magnitude: Mapped[float] = mapped_column(Float)
    # Compact JSON snapshot of the triggering data so we can re-interpret
    # without re-querying the detections table. Schema is per-kind.
    evidence_json: Mapped[str] = mapped_column(Text)
    # Claude's interpretation, written lazily. NULL = not yet interpreted.
    interpretation: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_anomaly_source_date", "source_name", "date_utc"),
    )


class WeatherObservationRow(Base):
    """One row per (location, UTC hour) of observed weather.

    Keyed by coordinate (lat/lon scaled to integer thousandths) rather than
    source name so a single observation covers every source pointed at the
    same place, and multi-site sources don't fragment history when they
    move between sites. Filled by the background weather worker on an
    hourly cadence from Open-Meteo.

    All metric columns are nullable — Open-Meteo can omit any of them per
    hour, and we prefer to store the partial row than to drop the whole hour.
    """

    __tablename__ = "weather_observations"

    # Integer-scaled lat/lon (round(value*1000)) — 3dp ≈ 110 m, matches the
    # in-process cache key in africam.weather and keeps the primary key
    # exact and easy to index.
    lat_e3: Mapped[int] = mapped_column(Integer, primary_key=True)
    lon_e3: Mapped[int] = mapped_column(Integer, primary_key=True)
    observed_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    precipitation_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_kph: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_cover_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    wmo_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_weather_time", "observed_at_utc"),
    )


class HighlightIntervalRow(Base):
    """One row per continuous span a source spent showing a replayed highlight
    reel (the orange HIGHLIGHT banner). Opened when the HighlightWatcher first
    sees the banner, closed when it clears. ``ended_at`` is NULL while ongoing.
    Mirrors WorkerDowntimeRow so /admin can answer 'how long in highlights'."""

    __tablename__ = "highlight_intervals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_name: Mapped[str] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PlaybackStateRow(Base):
    """Current live-vs-highlight state of a monitored source, written by the
    HighlightWatcher every tick. ``checked_at`` doubles as a freshness
    heartbeat: a stale value means the watcher isn't running, so /admin should
    show 'unknown' rather than a frozen state. Only sources with highlight
    gating enabled get a row."""

    __tablename__ = "playback_state"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    in_highlight: Mapped[bool] = mapped_column(default=False)
    # When the current state (live or highlight) began.
    since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Last time the watcher checked a frame — freshness signal.
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AudioQualityMetricRow(Base):
    """Current audio-quality snapshot per source, written by the pipeline
    (~every 60s) and read by /admin and the site page. Pure acoustic — how
    usable the feed's audio is for detection — see africam.audio.quality.
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


class SourceStateRow(Base):
    """Mutable per-source runtime state. The pipeline reads it on every chunk
    and the web app writes manual overrides to it. Used to coordinate the
    "which site is the camera at right now?" answer across processes."""

    __tablename__ = "source_state"

    source_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    site: Mapped[str | None] = mapped_column(String(128), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    # "manual" / "ocr" / None — how the current site was determined.
    detected_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # If set, manual overrides remain authoritative until this timestamp.
    manual_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
