"""Confusion candidates for the audition pane's A/B comparison.

The panel answers "what else could this be", and the ranking is the whole
feature: a genus-mate recorded at the same camera is a live hypothesis, one
from the other end of the country is trivia.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database
from birdbrain.web.app import create_app


def _app(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
        media_cache_enabled=False,
    )
    return create_app(cfg), Database(cfg.db_url)


def _add(db, *, sci, common, source, n=1, clip=True, label=None,
         confidence=0.8, rating=None):
    """Insert n detections and return the id of the last one.

    ``insert_detections`` returns a row *count*, not ids, so read the id back
    rather than assuming the two coincide -- they only do while the subject of
    the test happens to be the first row inserted, which is exactly the kind of
    accident that makes a later test silently assert against the wrong row.
    """
    base = datetime.now(UTC)
    for i in range(n):
        db.insert_detections(
            [Detection(source_name=source, started_at=base + timedelta(seconds=3 * i),
                       duration_s=3.0, scientific_name=sci, common_name=common,
                       confidence=confidence)],
            clip_path=f"/c/{source}-{sci}-{i}.ogg" if clip else None,
        )
    with db.session() as s:
        last = s.execute(
            sa_text("SELECT MAX(id) FROM detections WHERE scientific_name = :sci"),
            {"sci": sci},
        ).scalar_one()
        if label is not None or rating is not None:
            s.execute(
                sa_text("UPDATE detections SET label = :l, sound_rating = :r"
                        " WHERE id = :i"),
                {"l": label, "r": rating, "i": last},
            )
            s.commit()
    return last


def _similar(client, det_id):
    r = client.get(f"/api/detections/{det_id}/similar")
    assert r.status_code == 200, r.text
    return r.json()


class TestCandidates:
    def test_genus_mates_are_candidates_and_self_is_not(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Cisticola aridulus", common="Desert Cisticola", source="Cam")
        _add(db, sci="Cisticola natalensis", common="Croaking Cisticola", source="Other")

        body = _similar(c, det)
        names = [x["scientific_name"] for x in body["candidates"]]
        assert "Cisticola juncidis" not in names, "the species itself is not its own candidate"
        assert set(names) == {"Cisticola aridulus", "Cisticola natalensis"}
        assert body["genus"] == "Cisticola"

    def test_other_genera_are_not_candidates(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Ploceus velatus", common="Southern Masked-Weaver", source="Cam")

        assert _similar(c, det)["candidates"] == []

    def test_a_genus_prefix_is_not_a_genus(self, tmp_path):
        """``Apalis`` must not drag in ``Apalisoides``-style names — the match
        is on the genus token, not a string prefix."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Apalis flavida", common="Yellow-breasted Apalis", source="Cam")
        _add(db, sci="Apalisoides fictus", common="Not An Apalis", source="Cam")

        assert _similar(c, det)["candidates"] == []


class TestRanking:
    def test_heard_at_this_site_outranks_a_commoner_stranger(self, tmp_path):
        """THE ranking rule. The stranger has 50x the detections, but it has
        never occurred at this camera, so the local bird is the better
        hypothesis and must come first."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Cisticola aridulus", common="Desert Cisticola", source="Cam", n=1)
        _add(db, sci="Cisticola natalensis", common="Croaking Cisticola", source="Far", n=50)

        cands = _similar(c, det)["candidates"]
        assert [x["common_name"] for x in cands] == ["Desert Cisticola", "Croaking Cisticola"]
        assert cands[0]["here"] is True
        assert "heard at this site" in cands[0]["reason"]
        assert cands[1]["here"] is False

    def test_within_a_tier_the_commoner_bird_comes_first(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Cisticola aridulus", common="Desert Cisticola", source="Far", n=2)
        _add(db, sci="Cisticola natalensis", common="Croaking Cisticola", source="Far", n=9)

        cands = _similar(c, det)["candidates"]
        assert [x["common_name"] for x in cands] == ["Croaking Cisticola", "Desert Cisticola"]
        assert cands[0]["n"] == 9


class TestEdges:
    def test_missing_detection_is_404(self, tmp_path):
        app, _db = _app(tmp_path)
        assert TestClient(app).get("/api/detections/999999/similar").status_code == 404

    def test_resolves_the_subject_wherever_it_was_inserted(self, tmp_path):
        """Guards the fixture as much as the endpoint: the candidate list must
        come from the detection asked for, not from whichever row happens to
        be first in the table."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        _add(db, sci="Ploceus velatus", common="Southern Masked-Weaver", source="Cam")
        _add(db, sci="Ploceus intermedius", common="Lesser Masked-Weaver", source="Cam")
        det = _add(db, sci="Cisticola juncidis", common="Zitting Cisticola", source="Cam")
        _add(db, sci="Cisticola aridulus", common="Desert Cisticola", source="Cam")

        body = _similar(c, det)
        assert body["species"]["common_name"] == "Zitting Cisticola"
        assert [x["common_name"] for x in body["candidates"]] == ["Desert Cisticola"]

    def test_species_with_no_genus_mates_yields_an_empty_list(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Otus senegalensis", common="African Scops-Owl", source="Cam")
        body = _similar(c, det)
        assert body["candidates"] == []
        assert body["species"]["common_name"] == "African Scops-Owl"


class TestClipAvailability:
    def test_candidates_report_how_many_clips_survive(self, tmp_path):
        """Retention prunes most clips, so "we have heard it" and "we can play
        it" are different questions and the panel has to distinguish them."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Passer diffusus", common="Southern Gray-headed Sparrow",
                   source="Cam")
        _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
             source="Cam", n=3)
        _add(db, sci="Passer melanurus", common="Cape Sparrow", source="Cam",
             n=2, clip=False)

        by_name = {x["common_name"]: x for x in _similar(c, det)["candidates"]}
        assert by_name["Northern Gray-headed Sparrow"]["clips"] == 3
        assert by_name["Cape Sparrow"]["n"] == 2
        assert by_name["Cape Sparrow"]["clips"] == 0


class TestReferenceClips:
    def test_returns_our_own_clips_of_the_candidate(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Passer diffusus", common="Southern Gray-headed Sparrow",
                   source="Cam")
        _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
             source="Cam", n=2)

        r = c.get(f"/api/detections/{det}/reference-clips?sci=Passer%20griseus")
        assert r.status_code == 200
        clips = r.json()["clips"]
        assert len(clips) == 2
        assert all(x["same_source"] for x in clips)

    def test_the_clip_under_review_is_never_its_own_reference(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
                   source="Cam", n=2)

        r = c.get(f"/api/detections/{det}/reference-clips?sci=Passer%20griseus")
        assert det not in [x["id"] for x in r.json()["clips"]]

    def test_rows_without_a_clip_are_not_offered(self, tmp_path):
        """A player pointed at a pruned clip is a dead end, so these must not
        reach the picker at all."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Passer diffusus", common="Southern Gray-headed Sparrow",
                   source="Cam")
        _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
             source="Cam", n=3, clip=False)

        r = c.get(f"/api/detections/{det}/reference-clips?sci=Passer%20griseus")
        assert r.json()["clips"] == []

    def test_a_confirmed_clip_outranks_a_more_confident_one(self, tmp_path):
        """THE ranking rule: a human said yes to this one. That is worth more
        as a reference than a higher number from the same model whose output
        is the thing under review."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Passer diffusus", common="Southern Gray-headed Sparrow",
                   source="Cam")
        _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
             source="Far", confidence=0.99)
        good = _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
                    source="Far", confidence=0.40, label="good")

        clips = c.get(
            f"/api/detections/{det}/reference-clips?sci=Passer%20griseus"
        ).json()["clips"]
        assert clips[0]["id"] == good
        assert clips[0]["label"] == "good"

    def test_the_same_camera_keeps_slots_from_the_global_best(self, tmp_path):
        """Half the slots are reserved for the reviewer's own camera; without
        that the louder sites take every one and the same-stream comparison --
        same distance, same background, same codec -- is never offered."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Passer diffusus", common="Southern Gray-headed Sparrow",
                   source="Cam")
        _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
             source="Cam", n=2, confidence=0.30)
        _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
             source="Far", n=10, confidence=0.99)

        clips = c.get(
            f"/api/detections/{det}/reference-clips?sci=Passer%20griseus&limit=4"
        ).json()["clips"]
        assert any(x["same_source"] for x in clips), "the local camera lost every slot"
        assert any(not x["same_source"] for x in clips), "the global best lost every slot"


class TestPaletteOverride:
    """The A/B view forces one colour ramp across both panes. Without it the
    server picks each species' ramp from its note tag, and two spectrograms in
    different ramps cannot be compared by eye -- which is what that view is
    for."""

    def test_an_unknown_palette_is_rejected(self, tmp_path):
        app, db = _app(tmp_path)
        det = _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
                   source="Cam")
        r = TestClient(app).get(f"/spectrograms/{det}.png?palette=../../etc/passwd")
        assert r.status_code == 400

    def test_known_palettes_are_accepted(self, tmp_path):
        """Every ramp the tag map uses must get past validation. What happens
        afterwards is this fixture's business -- its clip path is outside the
        clips root, so the request dies on the path guard -- so assert only
        that none of them is the 400 an unknown palette earns."""
        app, db = _app(tmp_path)
        det = _add(db, sci="Passer griseus", common="Northern Gray-headed Sparrow",
                   source="Cam")
        c = TestClient(app)
        for pal in ("green", "fire", "cool", "fiery"):
            r = c.get(f"/spectrograms/{det}.png?palette={pal}")
            assert r.status_code != 400, f"{pal} was rejected"


class TestSoundsLike:
    """Acoustic confusions read out of the call descriptions.

    The genus rule cannot reach these: a Hadada Ibis is confused with an
    Egyptian Goose and a Hamerkop, which is three families and one harsh honk.
    """

    def _note(self, db, sci, text_):
        db.set_species_call_description(sci, text_)

    def test_a_named_confusion_becomes_a_candidate_across_genera(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Bostrychia hagedash", common="Hadada Ibis", source="Cam")
        _add(db, sci="Alopochen aegyptiaca", common="Egyptian Goose", source="Cam")
        self._note(db, "Bostrychia hagedash",
                   "A brassy shout, most easily confused with the Egyptian Goose.")

        cands = _similar(c, det)["candidates"]
        assert [x["common_name"] for x in cands] == ["Egyptian Goose"]
        assert cands[0]["sounds_like"] is True
        assert cands[0]["same_genus"] is False
        assert "sounds like" in cands[0]["reason"]

    def test_the_link_runs_both_ways(self, tmp_path):
        """Which of a confusable pair got the sentence written into its
        description is an accident of authorship, not evidence."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        _add(db, sci="Bostrychia hagedash", common="Hadada Ibis", source="Cam")
        goose = _add(db, sci="Alopochen aegyptiaca", common="Egyptian Goose",
                     source="Cam")
        # Only the ibis's description mentions the goose.
        self._note(db, "Bostrychia hagedash",
                   "A brassy shout, most easily confused with the Egyptian Goose.")

        cands = _similar(c, goose)["candidates"]
        assert [x["common_name"] for x in cands] == ["Hadada Ibis"]

    def test_a_longer_name_is_not_matched_as_a_shorter_one(self, tmp_path):
        """THE parsing trap. "Cape Sparrow" sits inside "Cape Sparrow-Weaver",
        and matching the short name first would invent a confusion nobody
        wrote down."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Passer diffusus", common="Southern Gray-headed Sparrow",
                   source="Cam")
        _add(db, sci="Passer melanurus", common="Cape Sparrow", source="Cam")
        _add(db, sci="Plocepasser mahali", common="Cape Sparrow-Weaver", source="Cam")
        self._note(db, "Passer diffusus", "Chirps, much like the Cape Sparrow-Weaver.")

        by_name = {x["common_name"]: x for x in _similar(c, det)["candidates"]}
        assert by_name["Cape Sparrow-Weaver"]["sounds_like"] is True
        # Cape Sparrow is still offered -- it is a congener -- but not as a
        # sound-alike, because the description never named it.
        assert by_name["Cape Sparrow"]["sounds_like"] is False
        assert by_name["Cape Sparrow"]["same_genus"] is True

    def test_a_species_naming_itself_is_not_its_own_candidate(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Bostrychia hagedash", common="Hadada Ibis", source="Cam")
        self._note(db, "Bostrychia hagedash", "The Hadada Ibis gives a brassy shout.")

        assert _similar(c, det)["candidates"] == []

    def test_a_sound_alike_outranks_a_commoner_congener(self, tmp_path):
        """THE ranking rule. Somebody wrote down that these two are confusable;
        the congener is merely related and happens to be abundant."""
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cecropis abyssinica", common="Lesser Striped Swallow",
                   source="Cam")
        _add(db, sci="Cecropis daurica", common="Red-rumped Swallow",
             source="Far", n=40)
        _add(db, sci="Cecropis cucullata", common="Greater Striped Swallow",
             source="Far", n=2)
        self._note(db, "Cecropis abyssinica",
                   "Nasal chatter; the Greater Striped Swallow is the usual trap.")

        names = [x["common_name"] for x in _similar(c, det)["candidates"]]
        assert names == ["Greater Striped Swallow", "Red-rumped Swallow"], names

    def test_both_reasons_show_when_both_apply(self, tmp_path):
        app, db = _app(tmp_path)
        c = TestClient(app)
        det = _add(db, sci="Cecropis abyssinica", common="Lesser Striped Swallow",
                   source="Cam")
        _add(db, sci="Cecropis cucullata", common="Greater Striped Swallow",
             source="Cam")
        self._note(db, "Cecropis abyssinica",
                   "The Greater Striped Swallow is the usual trap.")

        c0 = _similar(c, det)["candidates"][0]
        assert c0["sounds_like"] and c0["same_genus"] and c0["here"]
        assert c0["reason"] == "sounds like · same genus · heard at this site"
