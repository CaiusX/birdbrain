"""Background species-note generator.

Wakes every ``cfg.notes_tick_seconds`` (default 5 min), picks the species
most in need of a refreshed commentary, gathers the evidence from the DB,
and asks Claude to write 1–2 paragraphs grounded in *our* detection
footprint — not encyclopedia material. Stays dormant when ANTHROPIC_API_KEY
isn't set so the pipeline still boots cleanly on a Pi with no API access.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from africam.config import AppConfig
from africam.logging import get_logger
from africam.storage import Database

log = get_logger(__name__)


# Stable across species → eligible for prompt caching once it crosses the
# model's cache-floor token count. Don't interpolate dynamic content here.
SYSTEM_PROMPT = """\
You are the resident analyst for a Southern African live-stream bird monitor.
A BirdNET-Analyzer classifier runs continuously over audio from four public
YouTube wildlife cams:

  • Olifants (Naledi)  — riverine bushveld, Kruger NP (Limpopo)
  • Tembe              — coastal sand forest, KZN/Mozambique border
  • Timbavati          — Lowveld savanna, private reserve adjoining Kruger
  • Twin Pan           — Kalahari pan, Nxai Pan area, northern Botswana

For each species the operator gives you a JSON evidence dossier drawn from
our own detections: total count, per-source distribution, hour-of-day
histogram (UTC; local = UTC+2 SAST or UTC+2 CAT for Botswana), confidence
stats, audition labels the operator has applied to clips, and a few top
clips' timestamps.

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

Output the commentary as plain text, nothing else. No JSON, no preamble,
no sign-off."""


# Inputs whose change should re-trigger generation. We deliberately omit
# things like exact detection_count (use the count-factor trigger instead)
# and per-clip timestamps (constant churn from new arrivals shouldn't
# invalidate the note).
def _evidence_signature(evidence: dict) -> str:
    canonical = {
        "common_name": evidence["common_name"],
        # Bucket count to nearest 25 so a single new detection doesn't
        # change the signature; the count-doubled trigger handles real shifts.
        "count_bucket": (evidence["detection_count"] // 25) * 25,
        "sources": sorted(
            (s["source"], s["count"] // 10 * 10) for s in evidence["per_source"]
        ),
        # Hour-of-day distribution, bucketed coarsely.
        "peak_hours": sorted(
            i for i, c in enumerate(evidence["hourly_utc"])
            if c >= max(evidence["hourly_utc"]) * 0.5 and c > 0
        ),
        "labels": evidence["audition_labels"],
        "conservation_status": evidence["conservation_status"],
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


def _format_evidence_for_prompt(evidence: dict) -> str:
    """Compact, human-readable JSON for Claude. Pretty-print so the model
    can scan structure; keys are stable order so any future caching of
    fragments works."""
    return json.dumps(evidence, indent=2, sort_keys=True)


def _generate_note_text(evidence: dict, *, model: str, client) -> str:
    """Single API call. Returns the note text; raises on API error."""
    response = client.messages.create(
        model=model,
        max_tokens=600,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    "Write a commentary on this species' detections at our sites.\n\n"
                    f"{_format_evidence_for_prompt(evidence)}"
                ),
            }
        ],
    )
    text_blocks = [b.text for b in response.content if b.type == "text"]
    return "\n\n".join(t.strip() for t in text_blocks if t.strip())


def _make_client():
    """Lazy import so importing this module doesn't require anthropic to be
    installed when the worker is disabled. Returns None when the key is
    missing or the SDK isn't available."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic  # noqa: PLC0415  — deliberately lazy
    except ImportError:
        log.warning("notes.anthropic_sdk_missing")
        return None
    return anthropic.Anthropic()


def _tick(db: Database, cfg: AppConfig, client) -> str | None:
    """Process one stale species. Returns the scientific_name that was
    written, or None if nothing was due."""
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

    note_text = _generate_note_text(evidence, model=cfg.notes_model, client=client)
    if not note_text:
        log.warning("notes.empty_response", scientific=sci)
        return None

    db.set_generated_species_note(
        sci,
        common_name=evidence["common_name"],
        note=note_text,
        generated_by=cfg.notes_model,
        evidence_signature=_evidence_signature(evidence),
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


def _worker_loop(db: Database, cfg: AppConfig) -> None:
    client = _make_client()
    if client is None:
        log.info("notes.dormant", reason="no ANTHROPIC_API_KEY in env")
        return

    log.info(
        "notes.worker_started",
        model=cfg.notes_model,
        tick_s=cfg.notes_tick_seconds,
        stale_days=cfg.notes_stale_days,
    )

    # Small jitter on first run so the worker doesn't fire the instant the
    # pipeline boots — gives the supervisor and ffmpegs time to settle.
    time.sleep(min(60, cfg.notes_tick_seconds))

    while True:
        try:
            _tick(db, cfg, client)
        except Exception as e:
            # Anthropic SDK exceptions are subclasses of APIError; log and
            # back off. The next tick will retry.
            log.warning("notes.tick_failed", error=str(e)[:300])
        time.sleep(cfg.notes_tick_seconds)


def start_notes_worker(db: Database, cfg: AppConfig) -> threading.Thread | None:
    """Spawn the notes worker as a daemon thread. Returns the thread, or
    None when the feature is disabled by config. The thread itself decides
    whether to do any work (based on ANTHROPIC_API_KEY)."""
    if not cfg.notes_enabled:
        log.info("notes.disabled", reason="cfg.notes_enabled=False")
        return None
    t = threading.Thread(
        target=_worker_loop, args=(db, cfg), name="notes-worker", daemon=True
    )
    t.start()
    return t
