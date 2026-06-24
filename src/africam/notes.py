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
import re
import threading
import time
import copy
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from africam.config import AppConfig, SourceConfig
from africam.logging import get_logger
from africam.storage import Database
from africam.weather import daily_weather_summary, recent_weather_summary

log = get_logger(__name__)


# Per-site descriptive blurbs the prompts reference. Update this dict when
# adding a new site (same maintenance burden as the SOURCE_COLORS palette
# in web/app.py). A site that's live but missing from this dict still
# appears in the prompt — it just shows up with a "(no description on
# file)" tail so the model knows about its existence even before we fill
# in the blurb.
#
# Each line ends with the per-site UTC offset in parentheses; the prompt
# header instructs Claude to use that offset when discussing diurnal rhythm.
_SITE_DESCRIPTIONS: dict[str, str] = {
    "Olifants (Naledi)":   "24°S, riverine bushveld, Kruger NP (Limpopo, UTC+2) — southern hemisphere",
    "Tembe":               "27°S, coastal sand forest, KZN / Mozambique border (UTC+2) — southern hemisphere",
    "Timbavati":           "24°S, Lowveld savanna, private reserve adjoining Kruger (UTC+2) — southern hemisphere",
    "Twin Pan":            "20°S, Kalahari pan, Nxai Pan area, northern Botswana (UTC+2) — southern hemisphere",
    "Safarihoek":          "19°S, Etosha Heights escarpment, northern Namibia (UTC+2) — southern hemisphere",
    "Tau Game Lodge":      "25°S, Madikwe Game Reserve bushveld, NW South Africa (UTC+2) — southern hemisphere",
    "Tortilis Camp":       "3°S, Amboseli, semi-arid grassland under Kilimanjaro, southern Kenya (UTC+3) — equatorial",
    "Mara River":          "1°S, Maasai Mara Triangle, riverine savanna, southwestern Kenya (UTC+3) — equatorial",
    "Mpala Watering Hole": "0° (equator), Laikipia plateau acacia-Commiphora bushland, ~1800 m, research-station stream, central Kenya (UTC+3) — equatorial",
    "Stony Point":         "34°S, Coastal African Penguin colony with Cape Cormorants, Betty's Bay, Western Cape (UTC+2) — southern hemisphere",
    "Elephant Pan":        "22°S, Northern Tuli Block riparian waterhole, Mashatu area, eastern Botswana (UTC+2) — southern hemisphere",
}

# Counting word for the opening line ("watches eight cams"). Falls back to
# the digit beyond what's listed — the prompt still reads naturally.
_NUM_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve",
}


def _live_source_names(
    static_sources: Iterable[SourceConfig], db: Database
) -> list[str]:
    """Source list as the pipeline actually sees it: sources.toml entries
    minus admin-disabled ones, plus undeleted runtime sources added via
    /admin. Mirrors pipeline._desired_sources, kept here so notes doesn't
    have a cyclic dep on pipeline. Returns names sorted so the prompt is
    stable across ticks (cache-friendly)."""
    disabled = db.list_disabled_source_names()
    names = {s.name for s in static_sources if s.name not in disabled}
    for row in db.list_runtime_sources():
        if row.deleted_at is None and row.name not in disabled:
            names.add(row.name)
    return sorted(names)


def _build_sites_context(source_names: list[str]) -> str:
    """Build the shared context block for all four prompts from the live
    source list. Cheap (microseconds); rebuilt every tick so adding or
    removing a source is reflected on the next generation without a
    restart."""
    n = len(source_names)
    count_word = _NUM_WORDS.get(n, str(n))
    width = max((len(name) for name in source_names), default=0)
    bullets = "\n".join(
        f"  • {name:<{width}}  — {_SITE_DESCRIPTIONS.get(name, '(no description on file)')}"
        for name in source_names
    )
    return (
        f"The monitor watches {count_word} African live-stream wildlife cams "
        f"across two broad regions (Southern Africa: UTC+2 SAST/CAT; "
        f"East Africa: UTC+3 EAT):\n\n"
        f"{bullets}\n\n"
        f"A BirdNET-Analyzer classifier runs continuously over the audio. "
        f"Times in the dossiers are ALREADY in each site's LOCAL clock — the "
        f"fields say so (hourly_local, peak_hour_local, first_seen_local, with "
        f"a local_timezone tag). Use them as given; do NOT convert. The "
        f"bracketed offsets above are context only."
    )


# Prompts are built from the current source list every tick — see
# species_system_prompt() / site_system_prompt() / species_site_system_prompt()
# / daily_brief_system_prompt() below. The body of each prompt is constant;
# only the {sites_context} block changes, so as long as the source list is
# stable between ticks the rendered prompt is byte-identical and Anthropic's
# prompt cache stays warm.
_SPECIES_PROMPT_TEMPLATE = """\
You are the resident analyst for an African live-stream bird monitor network.
{sites_context}

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
  • For Palearctic migrants, check seasonality BEFORE geography. The
    species' African non-breeding window is roughly Sep–Apr; they are
    physically absent from the continent May–Aug while breeding in
    Eurasia. Detections inside the absence window are diagnostic of a
    false positive regardless of where in Africa they came from — lead
    with that argument over range-edge geography, which is a weaker
    case. Same logic in reverse for intra-African migrants: a southern-
    hemisphere breeding visitor heard at a southern site in austral
    winter (Jun–Aug) is just as suspicious. The note should name the
    expected window when invoking this argument.
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


_SITE_PROMPT_TEMPLATE = """\
You are the resident analyst for an African live-stream bird monitor network.
{sites_context}

The operator gives you a JSON evidence dossier for ONE site: its total
detection count, distinct species count, hour-of-day histogram (``hourly_local``,
in this site's local clock), top
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


_SPECIES_SITE_PROMPT_TEMPLATE = """\
You are the resident analyst for an African live-stream bird monitor network,
writing a SHORT, SITE-SPECIFIC commentary on how one species is heard at one
particular site, in the context of how it sounds across the network.
{sites_context}

The operator gives you a JSON dossier for ONE (species, site) pair:
its detection_count at this site, its share_of_network (this site's count as
a fraction of all detections of this species across all sites), confidence
stats, first/last seen (local), hourly_local histogram AT THIS SITE (already in
this site's local clock), per_source_network
totals (this species' count by site for contrast), label tallies at this site,
and newly_heard_at_site (true when this site only first heard it in the last
7 days — a "new arrival" signal).

Output your commentary as **2 to 4 BULLETS, ONE PER LINE.** Plain text, no
JSON, no leading dash/asterisk markers — just the bullet text. Telegraphic
fragments, not sentences. Lead with the fact. Pick from these angles:

  • When (peak hour) — read it straight from hourly_local; it is already this
    site's local clock, so quote the hour as-is (no conversion).
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


_DAILY_BRIEF_PROMPT_TEMPLATE = """\
You are the resident analyst for an African live-stream bird monitor network,
writing a daily soundscape brief. The brief's whole value is telling the
operator what was DIFFERENT today — not re-describing the same loud residents
that top the charts every single day.
{sites_context}

The operator gives you a JSON evidence dossier for ONE UTC date. Read it for
CHANGE, not volume. Key fields:
  • per_site[].count + ``count_ratio`` — today's detections ÷ this site's
    typical day. >1.5 = unusually busy; <0.5 = unusually quiet; ~1 = normal
    (not a story). ``typical_daily_count`` is the baseline. ``recently_added``
    = too new for a baseline, so go easy on it.
  • per_site[].top_species[].``days_seen_prior_7d`` — on how many of the last
    7 days that species was already among the site's regulars. 5–7 = a USUAL
    SUSPECT (e.g. the resident sandgrouse/nightjar/goose); mention it ONLY if
    there's a twist (record loudness, odd hour, suddenly absent). 0–1 = genuinely
    notable — lead with these.
  • peak_hour_local — already in the site's local clock; quote as-is. A peak
    shifted from the site's usual dawn/dusk is worth a bullet.
  • newly_heard_this_week — species new to a site vs its prior 7 days (firsts
    from brand-new cameras are already filtered out). ``newly_heard_omitted``
    counts any capped overflow.
  • anomalies — days the detector already flagged (volume_spike, nocturnal_burst,
    new_species_wave …) with any interpretation. Strong story angles.
  • high_confidence_standouts — only newsworthy if the species is NOT a usual
    suspect; a 1.0-confidence resident is not news.
  • weather — per-site local conditions; use only when it explains the day
    (rain killing the dawn chorus, a front, fog at sunrise).
  • recent_headlines — the LAST FEW DAYS' ledes. Do NOT reuse the same species
    or site as the opener; pick a different angle so the brief reads fresh.

Return STRICT JSON, nothing else:

{{
  "overall": "<≤45-word headline of what changed across the network today>",
  "sites": [
    {{"site": "<exact source_name from the evidence>",
      "bullets": ["<short note>", "<short note>"]}}
  ]
}}

The "overall" leads with the day's most genuinely unusual thing: a real first,
a site well above/below its norm, an anomaly, a shifted peak — explicitly NOT
"<resident species> dominates <site>" unless something about it actually
changed. Vary it from recent_headlines.

Each "sites" entry gets 1–3 TELEGRAPHIC bullets — fragments, not sentences,
leading with the concrete change. Good bullets:
  • "New this week: Black Cuckoo"
  • "2.4× a normal day — busiest since the front"
  • "Near silent (0.3×) — overnight rain"
  • "Dawn peak slid to 08:00 (usually 05:00)"
  • "Fish Eagle at 0.97 — first standout here in a week"
Bad bullets (these are the ruts to avoid):
  • "Namaqua Sandgrouse dominates — 1,432 detections" (usual suspect + raw count)
  • "Fiery-necked Nightjar peaks at dusk" (true every day)

Rules:
  • Output ONLY the JSON object — no markdown fences, no preamble.
  • Use the EXACT source_name strings from the evidence for "site".
  • Cover the 6–8 MOST NEWSWORTHY sites only, most interesting first. Skip any
    site having an ordinary day — silence is fine, don't pad.
  • Species by common name. Convey shape with ratios/relative terms; do NOT
    recite raw detection counts.
  • No throat-clearing, no sign-offs, no full-sentence padding in bullets."""


_ANOMALY_PROMPT_TEMPLATE = """\
You are the resident analyst for an African live-stream bird monitor network.
{sites_context}

A simple SQL detector has flagged ONE specific (site, date) as anomalous
because it crosses a numerical threshold. You're given the evidence dossier
and have to decide WHY this day was unusual in 2-3 sentences of plain prose
that an operator can act on.

Anomaly kinds you'll see:
  • volume_spike — count of detections this day is ≥3× the prior-week median.
  • nocturnal_burst — night-time (local 18:00–06:00) detections ≥2× daytime,
    with night count ≥30. Often a nocturnal flight-call signature.
  • new_species_wave — N species heard on this day that weren't recorded at
    this site in the prior 30 days. Often migration; sometimes a single
    weather front pushing a guild of arrivals.

YOU ARE WRITING FROM AFRICA, ABOUT AFRICAN BIRDS, FOR READERS WHO LIVE HERE.
Centre African ecology. Don't reach for European reference points
("autumn", "spring", "Palearctic migration") to anchor what an African
soundscape is doing — anchor it in the African seasons and the African
resident community first.

For most days at most sites, three things are moving, in this order of
importance:

(1) AFRICAN RESIDENTS — the default explanation. Across most days at
most sites the loudest, most-detected species are residents that live
here year-round: hornbills, doves, lapwings, francolins, weavers,
robin-chats, sunbirds, kingfishers, hadedas, fish-eagles, scops-owls,
nightjars, go-away-birds, coucals. Most volume_spikes and
nocturnal_bursts are residents responding to a local cue: an insect
hatch after warm rain, the start of the breeding season, a wind moving
roosting flocks, a kill drawing scavengers, a moonlit night with
extended owl activity. A chorus of African Scops-Owl + Fiery-necked
Nightjar + Hornbill is the night-shift and day-shift of the resident
community, not migration. Reach for migration only when both the
species AND the season fit.

(2) INTRA-AFRICAN MIGRATION — the home system. Many African breeders
move with the rains.
  • Southern-hemisphere sites (lat ≤ −10°, most of our sites): summer
    breeders ARRIVE September–November as the rains start, breed
    through the wet austral summer (December–March), then depart
    NORTHBOUND April–June at the start of the dry season, heading to
    equatorial refugia until the next rains. From a Limpopo, KZN, or
    Namibian site, "May" is the early dry season, not spring or autumn
    in any European sense — it is the departure window. Iconic
    departing voices: Diederik Cuckoo, Klaas's Cuckoo, Red-chested
    Cuckoo, Black Cuckoo, Woodland Kingfisher, African Paradise-
    Flycatcher, Lesser Striped Swallow, White-throated Swallow.
  • Equatorial sites (Tortilis Camp, Mara River, Mpala — near 0°):
    these ARE the equatorial refugia. Use East African seasonal
    vocabulary at these sites, NOT austral / Southern-African labels:
      – long rains: March–May
      – cool/dry season: June–September
      – short rains: October–December
      – warm/dry season: January–February
    Mara/Amboseli/Laikipia receive intra-African arrivals from
    Southern Africa in April–June (their breeders heading north as
    the southern dry season begins), and Palearctic arrivals from
    the north in September–November as the short rains start.
    April–May here is a busy northbound transit period at the tail
    end of the long rains. Do not call late May at Mara "tail end of
    the austral dry season" — at 1°S that is the tail end of the
    LONG RAINS.

(3) PALEARCTIC VISITORS — the away system. Only invoke this layer when
the species AND the timing both fit.
  • Eurasian breeders are visitors here. They arrive September–November
    escaping the northern winter, spread across the continent through
    our wet season, depart northbound March–May to breed in Eurasia,
    and ARE ABSENT FROM AFRICA June–August.
  • Diagnostic species (Eurasian breeders): Common Swift, Bank Swallow,
    House Martin, European Roller, Spotted Flycatcher, Lesser Kestrel,
    European Bee-eater, Sedge / Marsh / Olivaceous Warbler, Common /
    Wood Sandpiper, Common Snipe, White Stork.
  • Do not invent a Palearctic story when the species in the dossier
    are residents.

Frame movement RELATIVE TO THE SITE, not the equator. A southern site
in May is seeing summer breeders LEAVING — regardless of whether they
are heading to Botswana, Kenya, or Spain. An equatorial site in May is
seeing TRANSIT: African migrants from the south arriving, Eurasian
visitors departing.

Your job:
  1. First ask whether the dominant species in the dossier are
     residents, intra-African migrants, or Palearctic visitors. The
     answer almost always shapes the explanation.
  2. State plainly what most likely happened (1 sentence), using
     African seasonal language where natural: "early dry season",
     "build-up to the rains", "the long rains", "the short rains",
     "high summer on the lowveld". Avoid bare "spring" / "autumn" —
     they invert across hemispheres.
  3. Cite the 1–3 most diagnostic species or hourly cues from the
     dossier.
  4. If the evidence is ambiguous, or could equally be a microphone
     artefact, camera-angle change, insect hatch, or detector noise,
     say so. Don't invent specificity that isn't in the data.

Style:
  • Plain prose. No bullets. 60–120 words total.
  • Start with the inference, not "Based on the evidence...".
  • Use SCIENTIFIC names sparingly — common names are friendlier.
  • Refer to the site by its short name (Tortilis Camp, Tembe, etc.).

Output the explanation as plain text, nothing else."""


# ---------- prompt builders ----------
# Each builder formats its template with the current sites context, so adding
# or removing a source via /admin shows up in the very next tick — no restart
# needed. The body of each prompt is constant, so as long as the source list
# is stable between ticks the rendered prompt stays byte-identical and
# Anthropic's prompt cache keeps hitting.


def species_system_prompt(sites_context: str) -> str:
    return _SPECIES_PROMPT_TEMPLATE.format(sites_context=sites_context)


def site_system_prompt(sites_context: str) -> str:
    return _SITE_PROMPT_TEMPLATE.format(sites_context=sites_context)


def species_site_system_prompt(sites_context: str) -> str:
    return _SPECIES_SITE_PROMPT_TEMPLATE.format(sites_context=sites_context)


def daily_brief_system_prompt(sites_context: str) -> str:
    return _DAILY_BRIEF_PROMPT_TEMPLATE.format(sites_context=sites_context)


def anomaly_system_prompt(sites_context: str) -> str:
    return _ANOMALY_PROMPT_TEMPLATE.format(sites_context=sites_context)


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


def _site_offset(tz_name: str) -> tuple[int, str]:
    """Whole-hour UTC offset + short zone name for a site. The African zones we
    cover have no DST, so an integer offset is exact."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    off = round((now.utcoffset() or timedelta(0)).total_seconds() / 3600)
    return off, (now.tzname() or "UTC")


def _tz_for_source(name: str, src_list, db) -> str:
    """Resolve a source's IANA timezone from static config, then runtime
    sources (added via /admin), defaulting to UTC."""
    for s in src_list:
        if s.name == name:
            return s.timezone or "UTC"
    try:
        for row in db.list_runtime_sources():
            if row.name == name:
                return row.timezone or "UTC"
    except Exception:
        pass
    return "UTC"


def _localize_evidence(evidence: dict, tz_name: str) -> dict:
    """Return a copy of a single-site dossier with its UTC time fields rewritten
    into the site's local clock, so the model reads local times directly rather
    than converting them itself (and getting it wrong). hourly_utc→hourly_local
    (rotated by the offset), first/last_seen_utc→…_local, plus a local_timezone
    tag. The original dict is left intact for signatures/persistence."""
    ev = copy.deepcopy(evidence)
    off, abbr = _site_offset(tz_name)
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    h = ev.pop("hourly_utc", None)
    if isinstance(h, list) and len(h) == 24:
        ev["hourly_local"] = [h[(i - off) % 24] for i in range(24)]
    elif h is not None:
        ev["hourly_local"] = h
    for k in ("first_seen", "last_seen"):
        v = ev.pop(f"{k}_utc", None)
        if not v:
            continue
        try:
            dt = datetime.fromisoformat(v)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            ev[f"{k}_local"] = dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ev[f"{k}_local"] = v
    ev["local_timezone"] = abbr
    return ev


def _localize_brief_evidence(evidence: dict, src_list, db) -> dict:
    """Return a copy of the daily-brief dossier with each per-site peak hour
    rewritten into that site's local clock (peak_hour_utc→peak_hour_local)."""
    ev = copy.deepcopy(evidence)
    for site in ev.get("per_site", []):
        off, abbr = _site_offset(_tz_for_source(site.get("source_name"), src_list, db))
        ph = site.pop("peak_hour_utc", None)
        site["peak_hour_local"] = ((ph + off) % 24) if ph is not None else None
        site["local_timezone"] = abbr
    return ev


def _recent_brief_headlines(db: Database, before_date, limit: int = 3) -> list[dict]:
    """The last few days' brief ledes, so the model can avoid repeating the
    same headline species/site day after day. Returns [{date, overall}] newest
    first; skips stub/no-data days."""
    out: list[dict] = []
    try:
        rows = db.list_daily_briefs(limit=limit + 6)
    except Exception:
        return out
    for r in rows:
        if r.date_utc >= before_date:
            continue
        raw = (r.brief_text or "").strip()
        m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.S | re.I)
        if m:
            raw = m.group(1).strip()
        overall = ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                overall = (data.get("overall") or "").strip()
        except (ValueError, TypeError):
            overall = raw[:200]
        if overall and not overall.lower().startswith("no detections"):
            out.append({"date": r.date_utc.isoformat(), "overall": overall})
        if len(out) >= limit:
            break
    return out


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


def _species_tick(
    db: Database, cfg: AppConfig,
    sources: Iterable[SourceConfig], client,
) -> str | None:
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

    src_list = list(sources)
    # Cross-site species note: localise the aggregate histogram to the species'
    # dominant (loudest) site's clock, so "peak hour" reads as one sensible local
    # time rather than UTC. Per-site exactness lives in the species-site bullets.
    _per_src = evidence.get("per_source") or []
    _dominant = (
        max(_per_src, key=lambda x: x.get("count", 0)).get("source")
        if _per_src else None
    )
    prompt_evidence = (
        _localize_evidence(evidence, _tz_for_source(_dominant, src_list, db))
        if _dominant else evidence
    )
    sites_context = _build_sites_context(_live_source_names(src_list, db))
    note_text = _call_claude(
        system_prompt=species_system_prompt(sites_context),
        user_text=(
            "Write a commentary on this species' detections at our sites.\n\n"
            f"{_format_evidence_for_prompt(prompt_evidence)}"
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
    # candidate_names must include runtime sources (added via /admin),
    # otherwise per-(species, runtime-site) pairs never become candidates.
    candidate_names = _live_source_names(src_list, db)
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

    sites_context = _build_sites_context(_live_source_names(src_list, db))
    note_text = _call_claude(
        system_prompt=species_site_system_prompt(sites_context),
        user_text=(
            "Write a 2–4 bullet commentary on this species at this site.\n\n"
            f"{_format_evidence_for_prompt(_localize_evidence(evidence, _tz_for_source(source_name, src_list, db)))}"
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
    # Runtime sources (added via /admin, e.g. Safarihoek) need to be in the
    # candidate set too — otherwise they're invisible to the site picker and
    # never get a narrative even with thousands of detections.
    source_name = db.pick_stale_site_for_note(
        candidate_sources=_live_source_names(src_list, db),
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

    sites_context = _build_sites_context(_live_source_names(src_list, db))
    note_text = _call_claude(
        system_prompt=site_system_prompt(sites_context),
        user_text=(
            "Write a commentary on this site's soundscape.\n\n"
            f"{_format_evidence_for_prompt(_localize_evidence(evidence, _tz_for_source(source_name, src_list, db)))}"
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

        localized = _localize_brief_evidence(evidence, src_list, db)
        # Give the model the last few days' ledes so it can deliberately vary
        # the headline instead of re-running "X dominates" every day.
        localized["recent_headlines"] = _recent_brief_headlines(db, d)

        sites_context = _build_sites_context(_live_source_names(src_list, db))
        brief_text = _call_claude(
            system_prompt=daily_brief_system_prompt(sites_context),
            user_text=(
                "Return the JSON soundscape digest for this date.\n\n"
                f"{_format_evidence_for_prompt(localized)}"
            ),
            model=cfg.notes_model,
            client=client,
            # Overall paragraph + up to ~8 selected sites × ≤3 bullets. The
            # prompt caps site coverage so this no longer truncates mid-JSON
            # the way an all-sites dump did once the roster passed ~15.
            max_tokens=2000,
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


# ---------- anomaly detection & interpretation -----------


def _site_utc_offset(name: str, sources: Iterable[SourceConfig],
                     db: Database) -> int:
    """Hours east of UTC for a site's local clock. Reads source.timezone
    (Africa/Nairobi, Africa/Johannesburg, etc.) and computes the current
    offset via zoneinfo. DST in southern/East Africa is effectively zero,
    so the "current" offset is the correct one all year."""
    from zoneinfo import ZoneInfo
    tz_name = None
    for s in sources:
        if s.name == name:
            tz_name = s.timezone
            break
    if tz_name is None:
        for r in db.list_runtime_sources():
            if r.name == name and r.deleted_at is None:
                tz_name = r.timezone
                break
    try:
        return int(datetime.now(ZoneInfo(tz_name or "UTC"))
                   .utcoffset().total_seconds() // 3600)
    except Exception:
        return 0


def scan_anomalies_for_window(
    db: Database, sources: Iterable[SourceConfig],
    *, lookback_days: int = 14,
) -> int:
    """Sweep every (source, date) in the lookback window and record any
    anomalies the detectors fire.

    Three classes of detector run here:

    1. **first_live_day** (once per source, ever) — the source's first day
       of detections. Deterministic interpretation, no LLM cost. The
       lookback window is irrelevant here; we just check the row's
       existence and create it if missing.
    2. **down_day** (per (source, date) in lookback) — days with ≥1 h of
       cumulative downtime. Also deterministic.
    3. **volume_spike / nocturnal_burst / new_species_wave** — biological
       anomalies that get a Claude interpretation on the next anomaly_tick.

    Returns the count of new rows inserted."""
    from datetime import timedelta as _td
    src_list = list(sources)
    candidate_sources = _live_source_names(src_list, db)
    today = datetime.now(UTC).date()
    new_rows = 0
    for src_name in candidate_sources:
        # first_live_day — one row per source, ever. Cheap to attempt every
        # tick: record_anomaly bails fast on the existing-row case.
        first = db.detect_first_live(src_name)
        if first is not None:
            if db.record_anomaly(src_name, first["date"], first):
                new_rows += 1

        offset = _site_utc_offset(src_name, src_list, db)
        for delta in range(1, lookback_days + 1):  # skip today (incomplete)
            d = today - _td(days=delta)

            # Biological detectors (volume_spike / nocturnal_burst /
            # new_species_wave) — left for Claude to interpret.
            for a in db.detect_anomalies_for(
                src_name, d, tz_utc_offset_hours=offset,
            ):
                if db.record_anomaly(src_name, d, a):
                    new_rows += 1

            # down_day — deterministic interpretation, written at record
            # time so the worker doesn't burn a Haiku call on it.
            down = db.detect_down_day(src_name, d)
            if down is not None:
                if db.record_anomaly(src_name, d, down):
                    new_rows += 1
    return new_rows


def _format_anomaly_evidence(row, offset: int = 0, tz_abbr: str = "UTC") -> str:
    """Build the dossier text the LLM sees. Compact JSON-ish key/value layout so
    it reads naturally in the prompt window. ``offset``/``tz_abbr`` localise the
    hourly fields to the site's clock (the date stays the UTC date key)."""
    import json as _json
    ev = _json.loads(row.evidence_json or "{}")
    parts = [
        f"site: {row.source_name}",
        f"date_utc: {row.date_utc.isoformat()}",
        f"anomaly_kind: {row.kind}",
        f"detection_count: {row.detection_count}",
        f"baseline_count: {row.baseline_count}",
        f"magnitude: {row.magnitude:.2f}",
    ]
    if row.kind == "volume_spike":
        parts.append("top_species_on_day: " + ", ".join(
            f"{x['common']} ({x['n']}, max_conf {x['max_conf']:.2f})"
            for x in ev.get("top_species", [])[:10]
        ))
        parts.append("baseline_day_counts: " + ", ".join(
            str(n) for n in ev.get("baseline_days", [])
        ))
    elif row.kind == "nocturnal_burst":
        h = ev.get("hourly_utc", {})
        night = ev.get("night_hours_utc", [])
        h_local = {(int(k) + offset) % 24: v for k, v in h.items()}
        night_local = sorted({(int(x) + offset) % 24 for x in night})
        parts.append(f"hourly_local ({tz_abbr}): " + " ".join(
            f"{k:02d}={h_local[k]}" for k in sorted(h_local)
        ))
        parts.append(f"night_hours_local: {night_local}")
        parts.append("top_species_on_day: " + ", ".join(
            f"{x['common']} ({x['n']})"
            for x in ev.get("top_species", [])[:10]
        ))
    elif row.kind == "new_species_wave":
        parts.append(f"novelty_window_days: {ev.get('novelty_window_days')}")
        parts.append("species_NEW_today: " + ", ".join(
            f"{x['common']} ({x['n']}, max_conf {x['max_conf']:.2f})"
            for x in ev.get("new_species", [])
        ))
    return "\n".join(parts)


def _anomaly_tick(
    db: Database, cfg: AppConfig,
    sources: Iterable[SourceConfig], client,
) -> str | None:
    """Generate ONE interpretation for the oldest uninterpreted anomaly.
    Returns the row key string on success.

    First call per worker startup also scans for new anomalies (cheap pure
    SQL) so the queue is fresh."""
    pending = db.list_uninterpreted_anomalies(limit=1)
    if not pending:
        return None
    row = pending[0]
    src_list = list(sources)
    _off, _abbr = _site_offset(_tz_for_source(row.source_name, src_list, db))
    sites_context = _build_sites_context(_live_source_names(src_list, db))
    text = _call_claude(
        system_prompt=anomaly_system_prompt(sites_context),
        user_text=(
            "Explain why this day was anomalous at this site.\n\n"
            + _format_anomaly_evidence(row, _off, _abbr)
        ),
        model=cfg.notes_model,
        client=client,
        max_tokens=400,
    )
    if not text:
        log.warning(
            "notes.empty_response", kind="anomaly",
            source=row.source_name, date=row.date_utc.isoformat(),
            anomaly_kind=row.kind,
        )
        return None
    db.set_anomaly_interpretation(
        row.source_name, row.date_utc, row.kind,
        interpretation=text,
        generated_by=cfg.notes_model,
    )
    log.info(
        "anomaly.interpreted",
        source=row.source_name,
        date=row.date_utc.isoformat(),
        kind=row.kind,
        chars=len(text),
    )
    return f"{row.source_name}|{row.date_utc.isoformat()}|{row.kind}"


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

    # First-pass cheap SQL scan for fresh anomalies — runs once per worker
    # startup. Subsequent scans piggy-back on the periodic anomaly_tick
    # below, since the tick itself just pops the oldest uninterpreted row.
    try:
        n = scan_anomalies_for_window(db, sources, lookback_days=14)
        if n:
            log.info("anomaly.scanned", new_rows=n, window_days=14)
    except Exception as e:
        log.warning("anomaly.scan_failed", error=str(e)[:300])

    last_anomaly_scan = time.monotonic()

    while True:
        try:
            # Re-scan for new anomalies once an hour. Pure SQL — cheap.
            now = time.monotonic()
            if now - last_anomaly_scan > 3600:
                try:
                    n = scan_anomalies_for_window(db, sources, lookback_days=14)
                    if n:
                        log.info("anomaly.scanned", new_rows=n)
                except Exception as e:
                    log.warning("anomaly.scan_failed", error=str(e)[:300])
                last_anomaly_scan = now

            # Priority order: brief (time-sensitive) → site (rare but
            # high-value) → anomaly interpretation → species (always
            # something to do). First to return truthy wins this tick.
            did = _daily_brief_tick(db, cfg, sources, client)
            if not did:
                did = _site_tick(db, cfg, sources, client)
            if not did:
                did = _anomaly_tick(db, cfg, sources, client)
            if not did:
                # Drain both per-species and per-(species,site) backlogs in
                # parallel: each tick generates ONE of each. They're
                # independent, fast (one Claude call apiece), and at 5-min
                # ticks the combined rate is well within budget.
                _species_tick(db, cfg, sources, client)
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
    src_list = list(sources)
    if src_list:
        _enrich_brief_evidence_with_weather(evidence, src_list)
    sites_context = _build_sites_context(_live_source_names(src_list, db))
    brief_text = _call_claude(
        system_prompt=daily_brief_system_prompt(sites_context),
        user_text=(
            "Return the JSON soundscape digest for this date.\n\n"
            f"{_format_evidence_for_prompt(_localize_brief_evidence(evidence, src_list, db))}"
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
