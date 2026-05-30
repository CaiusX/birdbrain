"""Background commentary generator.

Single daemon thread, three task types in priority order:

  1. Daily soundscape brief — one paragraph per UTC date, generated once
     after midnight UTC. Highest priority because it's time-bounded:
     yesterday's brief is the headline on the dashboard.
  2. Site-level note — one per source/site, refreshed weekly or when
     detection volume doubles. Low cardinality (4 sites), low cadence.
  3. Species note — per-species commentary refreshed weekly or on growth.
     The highest-volume work; falls through after the higher-priority
     queues are quiet.

Each tick tries the queues in order and runs the first one that has work.
Most ticks land on species (or are no-ops if everything is fresh). Stays
dormant when ANTHROPIC_API_KEY is missing so the pipeline still boots
cleanly on a Pi with no API access.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterable

from africam.config import AppConfig, SourceConfig
from africam.logging import get_logger
from africam.storage import Database
from africam.weather import daily_weather_summary, recent_weather_summary

log = get_logger(__name__)


# Shared site context — referenced by all three prompts so the model has
# a consistent mental map of the sites regardless of which task type fires.
_SITES_CONTEXT = """\
The monitor watches four Southern African live-stream wildlife cams:

  • Olifants (Naledi)  — riverine bushveld, Kruger NP (Limpopo)
  • Tembe              — coastal sand forest, KZN/Mozambique border
  • Timbavati          — Lowveld savanna, private reserve adjoining Kruger
  • Twin Pan           — Kalahari pan, Nxai Pan area, northern Botswana

A BirdNET-Analyzer classifier runs continuously over the audio. All times
in the evidence dossiers are UTC; local clock is UTC+2 (SAST or CAT)."""


# Stable across species → eligible for prompt caching once the prompt
# crosses the model's cache-floor token count.
SPECIES_SYSTEM_PROMPT = f"""\
You are the resident analyst for a Southern African live-stream bird monitor.
{_SITES_CONTEXT}

For each species the operator gives you a JSON evidence dossier drawn from
our own detections: total count, per-source distribution, hour-of-day
histogram, confidence stats, audition labels the operator has applied to
clips, and a few top clips' timestamps.

Your job is to write a 1–2 paragraph commentary, ~120–200 words, that helps
the operator interpret what BirdNET is detecting at these specific sites.
Lead with what the evidence shows here, then add brief species context
(habitat, vocal character, biome plausibility) only where it sharpens the
interpretation.

Be honest about BirdNET's failure modes:
  • Mid-band whistles and trills (3–6 kHz cisticolas, sandpipers, finches)
    overlap heavily and are often confused with each other.
  • Forest robin-chats and some bushshrikes are accomplished mimics — a
    Telophorus detection can be a mimicking robin-chat.
  • Detections of species far outside their published range (e.g. a Western
    Yellow Wagtail at Twin Pan) are almost always misidentifications of a
    local congener. Say so plainly.
  • Loud common species (Egyptian Goose, Hadada Ibis) trigger a per-species
    confidence floor; if the evidence shows the floor is doing useful work,
    note that.

Style:
  • Plain prose. No markdown headers, no bullet lists, no asterisks for
    emphasis. Backticks only around `scientific_names` if you use them.
  • Don't restate every number in the dossier — summarize.
  • If the evidence is thin (few detections, one source, no labels), say
    so and keep the note short. Never invent specificity that isn't in the
    data.
  • Skip throat-clearing ("Based on the evidence provided, …"). Start with
    what's interesting.
  • Refer to sources by the short names above (Tembe, Twin Pan, etc.).

Output the commentary as plain text, nothing else."""


SITE_SYSTEM_PROMPT = f"""\
You are the resident analyst for a Southern African live-stream bird monitor.
{_SITES_CONTEXT}

The operator gives you a JSON evidence dossier for ONE site: its total
detection count, distinct species count, hour-of-day histogram (UTC), top
species (by count, with max confidence), audition labels on this site's
clips, the three highest-confidence clips, and a ``weather_recent`` block
summarizing the last week's conditions at the site (temps, rainfall days,
wind, dominant condition). ``weather_recent`` may be missing if Open-Meteo
didn't respond — handle gracefully.

Your job is to write a 2–3 paragraph commentary, ~180–280 words, that
captures this site as a sonic *place*. Cover:

  • What defines this site's soundscape — the resident voices that dominate
    the top-species list, what they tell you about the habitat.
  • The diurnal rhythm — when this site comes alive, the dawn/dusk story,
    any nocturnal signals. Read it from the hourly histogram.
  • What's distinctive vs. what you'd expect — anything unusual for the
    biome, any species the evidence shows are *defining* of this place vs.
    common everywhere.
  • Honest caveats — short detection window, BirdNET confusions worth
    flagging, dominant species crowding out quieter ones.

Use weather when it shapes interpretation: lots of recent rain at a normally
dry site, a hot dry stretch coinciding with low afternoon activity, a windy
week explaining quieter readings. Don't force weather into a note when
conditions are unremarkable — silence is fine.

Style:
  • Plain prose. No markdown headers, no bullet lists.
  • Open with the site's character, not a recap of its name and coordinates.
  • Name the most distinctive species in prose; don't recite the top-10 list.
  • If the data is thin (low count, narrow window, one obvious dominant),
    say so plainly and keep it short.
  • Skip throat-clearing.

Output the commentary as plain text, nothing else."""


SPECIES_SITE_SYSTEM_PROMPT = f"""\
You are the resident analyst for a Southern African live-stream bird monitor,
writing a SHORT, SITE-SPECIFIC commentary on how one species is heard at one
particular site, in the context of how it sounds across the network.
{_SITES_CONTEXT}

The operator gives you a JSON dossier for ONE (species, site) pair:
its detection_count at this site, its share_of_network (this site's count as
a fraction of all detections of this species across all sites), confidence
stats, first/last seen UTC, hourly_utc histogram AT THIS SITE, per_source_network
totals (this species' count by site for contrast), label tallies at this site,
and newly_heard_at_site (true when this site only first heard it in the last
7 days — a "new arrival" signal).

Output your commentary as **2 to 4 BULLETS, ONE PER LINE.** Plain text, no
JSON, no leading dash/asterisk markers — just the bullet text. Telegraphic
fragments, not sentences. Lead with the fact. Pick from these angles:

  • When (peak hour, local) — convert the UTC histogram to the site's local
    clock (most sites are UTC+2 / SAST or UTC+2 / CAT).
  • Relative loudness — is this site the loudest for this species, the
    quietest, or middle-of-the-pack? Use share_of_network and per_source_network
    to back this up without reciting raw counts.
  • Newly heard — if newly_heard_at_site is true, lead with that ("New
    arrival — first heard <when>").
  • Confidence character at this site — consistently strong, lots of distant
    weak detections, etc.
  • Any signature behavior the data hints at (dawn dominant, nocturnal,
    midday only, etc.).

Examples of good bullets:
  Dawn dominant — peak 04:00–05:00 SAST
  Loudest of all 5 sites — 60% of all detections here
  New arrival — first heard this week
  Quiet here — only 8 calls vs hundreds at Tembe
  Confidence consistently strong (mean 0.65)

Skip anything that doesn't tell the reader something they couldn't get from
the global per-species page. No throat-clearing, no recap of the species
name, no closing sentence. If the data is too thin (count < 30 or pattern
unclear), output a single bullet noting that.

Output as plain text, one bullet per line, nothing else."""


DAILY_BRIEF_SYSTEM_PROMPT = f"""\
You are the resident analyst for a Southern African live-stream bird monitor,
writing a daily newspaper-style soundscape brief.
{_SITES_CONTEXT}

The operator gives you a JSON evidence dossier for ONE UTC date: per-site
detection counts, each site's top species and peak hour, high-confidence
standouts across all sites, a "newly heard this week" list (species that
appeared at a site yesterday but not in the prior seven days at the same
site — the most interesting story angle), and a ``weather`` block on each
per-site entry summarizing that date's local conditions at that site
(min/max/mean temp, total rain in mm, hours of rain, sunrise/sunset, modal
condition). ``weather`` may be missing if Open-Meteo didn't respond.

Your job is a skimmable digest: ONE short overall paragraph, then crisp
per-site bullet points. Return it as STRICT JSON, nothing else:

{{
  "overall": "<2-3 sentence highlights paragraph for the whole network>",
  "sites": [
    {{"site": "<exact source_name from the evidence>",
      "bullets": ["<short note>", "<short note>"]}}
  ]
}}

The "overall" paragraph (≤55 words) is the day's headline across all
sites — usually a "newly heard" species or a striking confidence
standout, plus the broad shape of the day (who was loud, who was quiet).

Each "sites" entry gets 1–4 TELEGRAPHIC bullets — fragments, not
sentences. Lead with the concrete fact. Good bullets:
  • "New this week: Black Cuckoo"
  • "Busiest dawn of the week (peak 05:00)"
  • "Fish Eagle standout — conf 0.97"
  • "Near silent — overnight rain"
Cover, per site, whatever's worth flagging: newly-heard species, a top
or high-confidence call (common name), peak timing, notable silence, and
weather ONLY when it's part of the story (rain suppressing dawn, a front,
fog at sunrise). Skip weather when conditions are unremarkable.

Rules:
  • Output ONLY the JSON object — no markdown fences, no preamble.
  • Use the EXACT source_name strings from the evidence for "site".
  • Include a site only if it has something worth saying; order the most
    interesting sites first.
  • Species by common name. Convey shape, don't recite raw counts.
  • No throat-clearing, no sign-offs, no full-sentence padding in bullets."""


# ---------- evidence signatures (re-trigger when these change) ----------


def _species_signature(evidence: dict) -> str:
    """Inputs whose change should re-trigger species-note regeneration. We
    deliberately omit exact counts (use the count-factor trigger instead)
    and per-clip timestamps (constant churn shouldn't invalidate the note)."""
    hourly = evidence["hourly_utc"]
    peak = max(hourly) if hourly else 0
    canonical = {
        "common_name": evidence["common_name"],
        "count_bucket": (evidence["detection_count"] // 25) * 25,
        "sources": sorted(
            (s["source"], s["count"] // 10 * 10) for s in evidence["per_source"]
        ),
        "peak_hours": sorted(
            i for i, c in enumerate(hourly) if c >= peak * 0.5 and c > 0
        ),
        "labels": evidence["audition_labels"],
        "conservation_status": evidence["conservation_status"],
    }
    return _hash(canonical)


def _site_signature(evidence: dict) -> str:
    hourly = evidence["hourly_utc"]
    peak = max(hourly) if hourly else 0
    canonical = {
        "count_bucket": (evidence["detection_count"] // 100) * 100,
        "distinct_species_bucket": (evidence["distinct_species"] // 5) * 5,
        # Top species by name only — small reorderings or count drift below
        # the species_signature threshold shouldn't churn the site note.
        "top_species": [s["scientific_name"] for s in evidence["top_species"]],
        "peak_hours": sorted(
            i for i, c in enumerate(hourly) if c >= peak * 0.5 and c > 0
        ),
        "labels": evidence["audition_labels"],
    }
    return _hash(canonical)


def _species_site_signature(evidence: dict) -> str:
    """Coarse signature so small drift doesn't churn the per-pair note. Buckets
    count to nearest 25, hashes the peak-hour set and the loudest-site name."""
    hourly = evidence.get("hourly_utc") or []
    peak = max(hourly) if hourly else 0
    per_source = evidence.get("per_source_network") or []
    loudest = max(per_source, key=lambda x: x["count"])["source"] if per_source else ""
    canonical = {
        "count_bucket": (evidence["detection_count"] // 25) * 25,
        "peak_hours": sorted(
            i for i, c in enumerate(hourly) if c >= peak * 0.5 and c > 0
        ),
        "loudest_site": loudest,
        "newly_heard": bool(evidence.get("newly_heard_at_site")),
    }
    return hashlib.sha1(
        json.dumps(canonical, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _brief_signature(evidence: dict) -> str:
    canonical = {
        "date_utc": evidence["date_utc"],
        "total_bucket": (evidence["total_detections"] // 50) * 50,
        "distinct": evidence["distinct_species"],
        "per_site": sorted(
            (p["source_name"], p["count"] // 20 * 20, p["peak_hour_utc"])
            for p in evidence["per_site"]
        ),
        "newly_heard": sorted(
            (p["source_name"], p["scientific_name"])
            for p in evidence["newly_heard_this_week"]
        ),
    }
    return _hash(canonical)


def _hash(canonical: dict) -> str:
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


def _format_evidence_for_prompt(evidence: dict) -> str:
    return json.dumps(evidence, indent=2, sort_keys=True)


# ---------- Claude call ----------


def _call_claude(
    *,
    system_prompt: str,
    user_text: str,
    model: str,
    client,
    max_tokens: int,
) -> str:
    """Single API call. Returns the response text; raises on API error."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_text}],
    )
    text_blocks = [b.text for b in response.content if b.type == "text"]
    return "\n\n".join(t.strip() for t in text_blocks if t.strip())


def _make_client():
    """Lazy import so importing this module doesn't require anthropic to be
    installed when the worker is disabled."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic  # noqa: PLC0415  — deliberately lazy
    except ImportError:
        log.warning("notes.anthropic_sdk_missing")
        return None
    return anthropic.Anthropic()


# ---------- tick functions: one per task type, each returns truthy on work ----------


def _species_tick(db: Database, cfg: AppConfig, client) -> str | None:
    sci = db.pick_stale_species_for_note(
        max_age_days=cfg.notes_stale_days,
        min_detections=cfg.notes_min_detections,
        regen_count_factor=cfg.notes_regen_count_factor,
    )
    if sci is None:
        return None

    evidence = db.gather_species_evidence(sci)
    if evidence is None:
        return None

    note_text = _call_claude(
        system_prompt=SPECIES_SYSTEM_PROMPT,
        user_text=(
            "Write a commentary on this species' detections at our sites.\n\n"
            f"{_format_evidence_for_prompt(evidence)}"
        ),
        model=cfg.notes_model,
        client=client,
        max_tokens=600,
    )
    if not note_text:
        log.warning("notes.empty_response", kind="species", scientific=sci)
        return None

    db.set_generated_species_note(
        sci,
        common_name=evidence["common_name"],
        note=note_text,
        generated_by=cfg.notes_model,
        evidence_signature=_species_signature(evidence),
        detection_count_at_gen=evidence["detection_count"],
    )
    log.info(
        "notes.generated",
        scientific=sci,
        common=evidence["common_name"],
        chars=len(note_text),
        detections=evidence["detection_count"],
    )
    return sci


def _species_site_tick(
    db: Database, cfg: AppConfig, sources: Iterable[SourceConfig], client
) -> str | None:
    """Generate one (species, site) note. Returns "sci|source" on success."""
    src_list = list(sources)
    candidate_names = [s.name for s in src_list]
    pick = db.pick_stale_species_site_for_note(
        candidate_sources=candidate_names,
        max_age_days=cfg.notes_stale_days * 2,  # per-pair ages slower
        min_detections=cfg.notes_species_site_min_detections,
        regen_count_factor=cfg.notes_regen_count_factor,
    )
    if pick is None:
        return None
    sci, source_name = pick

    evidence = db.gather_species_site_evidence(sci, source_name)
    if evidence is None:
        return None

    note_text = _call_claude(
        system_prompt=SPECIES_SITE_SYSTEM_PROMPT,
        user_text=(
            "Write a 2–4 bullet commentary on this species at this site.\n\n"
            f"{_format_evidence_for_prompt(evidence)}"
        ),
        model=cfg.notes_model,
        client=client,
        max_tokens=300,
    )
    if not note_text:
        log.warning(
            "notes.empty_response", kind="species_site", scientific=sci, source=source_name
        )
        return None

    db.set_generated_species_site_note(
        sci,
        source_name,
        common_name=evidence["common_name"],
        note=note_text,
        generated_by=cfg.notes_model,
        evidence_signature=_species_site_signature(evidence),
        detection_count_at_gen=evidence["detection_count"],
    )
    log.info(
        "species_site_note.generated",
        scientific=sci,
        common=evidence["common_name"],
        source=source_name,
        chars=len(note_text),
        detections=evidence["detection_count"],
    )
    return f"{sci}|{source_name}"


def _site_tick(
    db: Database, cfg: AppConfig, sources: Iterable[SourceConfig], client
) -> str | None:
    src_list = list(sources)
    source_name = db.pick_stale_site_for_note(
        candidate_sources=[s.name for s in src_list],
        max_age_days=cfg.notes_stale_days,
        min_detections=cfg.notes_min_detections,
        regen_count_factor=cfg.notes_regen_count_factor,
    )
    if source_name is None:
        return None

    evidence = db.gather_site_evidence(source_name)
    if evidence is None:
        return None

    # Inject prevailing weather at this site (last week). Open-Meteo failure
    # is non-fatal — the prompt is told to handle a missing weather field.
    src_cfg = next((s for s in src_list if s.name == source_name), None)
    if src_cfg and src_cfg.lat is not None and src_cfg.lon is not None:
        weather = recent_weather_summary(
            src_cfg.lat, src_cfg.lon, src_cfg.timezone, lookback_days=7
        )
        if weather:
            evidence["weather_recent"] = weather

    note_text = _call_claude(
        system_prompt=SITE_SYSTEM_PROMPT,
        user_text=(
            "Write a commentary on this site's soundscape.\n\n"
            f"{_format_evidence_for_prompt(evidence)}"
        ),
        model=cfg.notes_model,
        client=client,
        max_tokens=800,
    )
    if not note_text:
        log.warning("notes.empty_response", kind="site", source=source_name)
        return None

    db.set_generated_site_note(
        source_name,
        note=note_text,
        generated_by=cfg.notes_model,
        evidence_signature=_site_signature(evidence),
        detection_count_at_gen=evidence["detection_count"],
    )
    log.info(
        "site_note.generated",
        source=source_name,
        chars=len(note_text),
        detections=evidence["detection_count"],
        species=evidence["distinct_species"],
    )
    return source_name


def _enrich_brief_evidence_with_weather(
    evidence: dict, sources: Iterable[SourceConfig]
) -> dict:
    """Mutate (and return) the brief-evidence dict by adding a ``weather``
    sub-dict to each per-site entry. Best-effort: missing API responses
    leave the field absent. Date comes from ``evidence['date_utc']``."""
    from datetime import date as _date

    by_name = {s.name: s for s in sources}
    target_date = _date.fromisoformat(evidence["date_utc"])
    for entry in evidence["per_site"]:
        src = by_name.get(entry["source_name"])
        if not src or src.lat is None or src.lon is None:
            continue
        w = daily_weather_summary(src.lat, src.lon, target_date, src.timezone)
        if w:
            entry["weather"] = w
    return evidence


def _daily_brief_tick(
    db: Database, cfg: AppConfig, sources: Iterable[SourceConfig], client
):
    """Returns the date (as ISO string) for which a brief was generated, or
    None if no missing-brief had data."""
    src_list = list(sources)
    missing = db.missing_daily_briefs(lookback_days=cfg.notes_brief_lookback_days)
    for d in missing:
        evidence = db.gather_daily_evidence(d)
        if evidence is None:
            # No data that day (pipeline was down) — record an empty stub
            # so we don't keep retrying. Use a tiny placeholder text.
            db.set_daily_brief(
                d,
                brief_text=f"No detections recorded on {d.isoformat()} — the "
                "monitor was offline or all sources were silent.",
                generated_by="stub",
                total_detections=0,
                distinct_species=0,
                evidence_signature="empty",
            )
            log.info("daily_brief.stub", date=d.isoformat())
            continue

        _enrich_brief_evidence_with_weather(evidence, src_list)

        brief_text = _call_claude(
            system_prompt=DAILY_BRIEF_SYSTEM_PROMPT,
            user_text=(
                "Return the JSON soundscape digest for this date.\n\n"
                f"{_format_evidence_for_prompt(evidence)}"
            ),
            model=cfg.notes_model,
            client=client,
            # Room for an overall paragraph + ~9 sites × 4 bullets. 800 was
            # truncating mid-JSON once the cam roster grew past 5.
            max_tokens=1800,
        )
        if not brief_text:
            log.warning("notes.empty_response", kind="brief", date=d.isoformat())
            continue

        db.set_daily_brief(
            d,
            brief_text=brief_text,
            generated_by=cfg.notes_model,
            total_detections=evidence["total_detections"],
            distinct_species=evidence["distinct_species"],
            evidence_signature=_brief_signature(evidence),
        )
        log.info(
            "daily_brief.generated",
            date=d.isoformat(),
            chars=len(brief_text),
            detections=evidence["total_detections"],
            species=evidence["distinct_species"],
        )
        return d.isoformat()
    return None


# ---------- worker loop: priority router ----------


def _worker_loop(
    db: Database, cfg: AppConfig, sources: list[SourceConfig]
) -> None:
    client = _make_client()
    if client is None:
        log.info("notes.dormant", reason="no ANTHROPIC_API_KEY in env")
        return

    log.info(
        "notes.worker_started",
        model=cfg.notes_model,
        tick_s=cfg.notes_tick_seconds,
        stale_days=cfg.notes_stale_days,
        sources=[s.name for s in sources],
    )

    # Jitter so the worker doesn't fire the instant the pipeline boots.
    time.sleep(min(60, cfg.notes_tick_seconds))

    while True:
        try:
            # Priority order: brief (time-sensitive) → site (rare but
            # high-value) → species (always something to do). First to
            # return truthy wins this tick.
            did = _daily_brief_tick(db, cfg, sources, client)
            if not did:
                did = _site_tick(db, cfg, sources, client)
            if not did:
                # Drain both per-species and per-(species,site) backlogs in
                # parallel: each tick generates ONE of each. They're
                # independent, fast (one Claude call apiece), and at 5-min
                # ticks the combined rate is well within budget.
                _species_tick(db, cfg, client)
                _species_site_tick(db, cfg, sources, client)
        except Exception as e:
            log.warning("notes.tick_failed", error=str(e)[:300])
        time.sleep(cfg.notes_tick_seconds)


def start_notes_worker(
    db: Database, cfg: AppConfig, sources: list[SourceConfig]
) -> threading.Thread | None:
    """Spawn the notes worker as a daemon thread. Returns the thread, or
    None when the feature is disabled by config. The thread itself decides
    whether to do any work (based on ANTHROPIC_API_KEY)."""
    if not cfg.notes_enabled:
        log.info("notes.disabled", reason="cfg.notes_enabled=False")
        return None
    t = threading.Thread(
        target=_worker_loop,
        args=(db, cfg, list(sources)),
        name="notes-worker",
        daemon=True,
    )
    t.start()
    return t


# ---------- manual / shell entry points ----------


def generate_brief_for_date(
    db: Database,
    cfg: AppConfig,
    date_utc,
    sources: Iterable[SourceConfig] = (),
) -> str | None:
    """One-shot: generate (or overwrite) a brief for a specific UTC date.
    Used by the verification step and as an admin escape hatch. Skips the
    missing_daily_briefs gate so it can be run against today. Pass
    ``sources`` to attach per-site weather; omit for a weather-less brief."""
    client = _make_client()
    if client is None:
        raise RuntimeError("ANTHROPIC_API_KEY not set in env")
    evidence = db.gather_daily_evidence(date_utc)
    if evidence is None:
        return None
    if sources:
        _enrich_brief_evidence_with_weather(evidence, list(sources))
    brief_text = _call_claude(
        system_prompt=DAILY_BRIEF_SYSTEM_PROMPT,
        user_text=(
            "Return the JSON soundscape digest for this date.\n\n"
            f"{_format_evidence_for_prompt(evidence)}"
        ),
        model=cfg.notes_model,
        client=client,
        max_tokens=1800,
    )
    if not brief_text:
        return None
    db.set_daily_brief(
        date_utc,
        brief_text=brief_text,
        generated_by=cfg.notes_model,
        total_detections=evidence["total_detections"],
        distinct_species=evidence["distinct_species"],
        evidence_signature=_brief_signature(evidence),
    )
    return brief_text
