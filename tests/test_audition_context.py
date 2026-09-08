"""The audition pane answers "is this that bird, here" — a call description and
where the species has been heard — not the several-paragraph species note."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from birdbrain import notes as notes_mod
from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
from birdbrain.web.app import create_app

SCI = "Otus senegalensis"


def _app(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
        media_cache_enabled=False,
    )
    return create_app(cfg), Database(cfg.db_url), cfg


def _add(db, *, sci=SCI, common="African Scops-Owl", source="Cam", n=1,
         lat=None, lon=None, clip=True):
    base = datetime.now(UTC)
    for i in range(n):
        db.insert_detections(
            [Detection(source_name=source, started_at=base + timedelta(seconds=3 * i),
                       duration_s=3.0, scientific_name=sci, common_name=common,
                       confidence=0.8)],
            clip_path=f"/c/{source}{i}.ogg" if clip else None,
            latitude=lat, longitude=lon,
        )


def test_dispersion_is_shaped_by_concentration_not_just_totals(tmp_path):
    """A species that is 90% one site is a different proposition at the other
    10%, and that is the judgement the pane exists to support."""
    _, db, _ = _app(tmp_path)
    _add(db, source="Stronghold", n=90, lat=-24.0, lon=31.0)
    _add(db, source="Edge", n=10, lat=-2.0, lon=34.0)
    d = db.species_dispersion(SCI)
    assert d["sites"] == 2
    assert d["total"] == 100
    assert d["top"][0]["source"] == "Stronghold"
    assert round(d["top"][0]["share"], 2) == 0.90
    assert d["span_km"] > 2000          # two very different places


def test_dispersion_of_a_single_site_species_has_no_span(tmp_path):
    _, db, _ = _app(tmp_path)
    _add(db, source="OnlyHere", n=5, lat=-24.0, lon=31.0)
    d = db.species_dispersion(SCI)
    assert d["sites"] == 1 and d["span_km"] is None


def test_dispersion_of_an_unheard_species_is_empty_not_an_error(tmp_path):
    _, db, _ = _app(tmp_path)
    assert db.species_dispersion("Nothing here") == {
        "sites": 0, "total": 0, "top": [], "span_km": None
    }


def test_the_audition_endpoint_serves_call_and_geography_not_the_note(tmp_path):
    app, db, _ = _app(tmp_path)
    _add(db, source="Twin Pan", n=8, lat=-18.6, lon=23.5)
    db.set_species_note(SCI, common_name="African Scops-Owl",
                        note="A long narrative " * 60, tag="reliable")
    db.set_species_call_description(SCI, "A soft frog-like prrrp every few seconds.")

    body = TestClient(app).get(f"/api/species/{SCI}/audition-context").json()
    assert body["call_description"] == "A soft frog-like prrrp every few seconds."
    assert body["tag"] == "reliable"
    assert body["dispersion"]["top"][0]["source"] == "Twin Pan"
    # The narrative is deliberately absent from this payload.
    assert "note" not in body


def test_a_species_with_no_description_yet_still_gets_its_geography(tmp_path):
    app, db, _ = _app(tmp_path)
    _add(db, source="Cam", n=3, lat=-24.0, lon=31.0)
    body = TestClient(app).get(f"/api/species/{SCI}/audition-context").json()
    assert body["call_description"] == ""
    assert body["dispersion"]["sites"] == 1


# --- generating the description -------------------------------------------


class _FakeClient:
    """Stands in for the Anthropic client; records what it was asked."""

    def __init__(self, reply="A hollow two-note hoo-poo near 700 Hz."):
        self.reply = reply
        self.prompts: list[tuple[str, str]] = []
        self.messages = self

    def create(self, *, model, max_tokens, system, messages, **kw):
        # ``system`` arrives as cache-control blocks, not a bare string.
        sys_text = system if isinstance(system, str) else " ".join(
            b.get("text", "") for b in system
        )
        self.prompts.append((sys_text, messages[0]["content"]))
        block = type("B", (), {"type": "text", "text": self.reply})()
        return type("R", (), {"content": [block]})()


def test_the_tick_describes_the_biggest_backlog_first(tmp_path):
    """Whoever sits down to review meets the biggest pile first, so that is
    where an API call is worth spending."""
    _, db, cfg = _app(tmp_path)
    _add(db, sci="Big", common="Big Bird", n=60)
    _add(db, sci="Small", common="Small Bird", n=55)
    assert db.pick_species_missing_call_description(min_detections=50) == "Big"

    client = _FakeClient()
    got = notes_mod._call_description_tick(db, cfg, client)
    assert got == "Big"
    assert db.get_species_note("Big").call_description.startswith("A hollow")
    # The prompt names the bird and asks about the sound, not about our network.
    system, user = client.prompts[0]
    assert "Big Bird" in user
    assert "spectrogram" in system.lower()


def test_a_described_species_is_never_asked_about_again(tmp_path):
    """The description is reference material about the bird, not a reading of
    our data, so it does not go stale and must not be regenerated."""
    _, db, cfg = _app(tmp_path)
    _add(db, sci="A", common="Bird", n=60)
    client = _FakeClient()
    assert notes_mod._call_description_tick(db, cfg, client) == "A"
    assert notes_mod._call_description_tick(db, cfg, client) is None
    assert len(client.prompts) == 1


def test_species_below_the_threshold_are_left_alone(tmp_path):
    _, db, _ = _app(tmp_path)
    _add(db, sci="Rare", common="Rare Bird", n=3)
    assert db.pick_species_missing_call_description(min_detections=50) is None


def test_a_reviewed_species_with_nothing_waiting_is_not_picked(tmp_path):
    """The picker works off the review backlog, so a species with no clips
    waiting is not worth an API call however many it once had."""
    _, db, _ = _app(tmp_path)
    _add(db, sci="Done", common="Done Bird", n=60, clip=False)
    assert db.pick_species_missing_call_description(min_detections=50) is None
