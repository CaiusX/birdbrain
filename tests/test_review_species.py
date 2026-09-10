"""/review leads with a species index, not a flat list of near-duplicate clips."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.storage import Database, DetectionRow
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


def _strip_scripts(html: str) -> str:
    """Rendered markup with <script> blocks removed.

    Same reasoning as ``_listed`` below, one layer further out: these helpers
    look for links, and script source is not a link. The spectrogram modal
    builds review URLs client-side, so its JavaScript can hold the very
    patterns these regexes hunt for, and a page that renders no drill-through
    at all would still appear to.
    """
    return re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)


def _listed(html: str) -> list[str]:
    """Scientific names the species index actually lists.

    Assert on these rather than on the whole page: base.html carries a global
    species search datalist holding every name ever heard, so a naive
    ``"X" in html`` is true for species the queue is not offering at all.
    """
    html = _strip_scripts(html)
    return re.findall(r"tab=(?:sites|detections)&(?:amp;)?sci=([^\"&]+)", html)


_seq = iter(range(1, 100_000))


def _add(db, *, sci, common, source="Cam", conf=0.8, n=1, clip=True, label=None, at=None):
    """Add ``n`` detections. ``label`` marks only the rows this call creates —
    labelling every row of the species instead would quietly make a test that
    mixes reviewed and unreviewed clips of one bird impossible to write."""
    base = at or datetime.now(UTC)
    made = []
    for _ in range(n):
        k = next(_seq)
        started = base + timedelta(seconds=3 * k)
        db.insert_detections(
            [Detection(source_name=source, started_at=started, duration_s=3.0,
                       scientific_name=sci, common_name=common, confidence=conf)],
            clip_path=f"/clips/{sci}-{source}-{k}.ogg" if clip else None,
        )
        made.append(started)
    if label:
        with db.session() as s, s.begin():
            for row in s.query(DetectionRow).filter(
                DetectionRow.scientific_name == sci,
                DetectionRow.started_at.in_(made),
            ):
                row.label = label


def test_the_default_tab_is_the_species_index(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="Pterocles namaqua", common="Namaqua Sandgrouse", n=35, conf=1.0)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", n=4, conf=0.9)
    html = TestClient(app).get("/review").text
    # One row per species, not one per clip: the 35-clip species appears once.
    listed = _listed(html)
    assert listed.count("Pterocles%20namaqua") == 1
    assert sorted(listed) == ["Otus%20senegalensis", "Pterocles%20namaqua"]
    assert "35" in html and "waiting" in html


def test_a_species_row_links_into_its_own_queue(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", n=3)
    html = TestClient(app).get("/review").text
    assert "Otus%20senegalensis" in _listed(html)

    drill = TestClient(app).get("/review?tab=detections&sci=Otus senegalensis").text
    assert "all species" in drill              # the way back
    assert "African Scops-Owl" in drill


def test_the_drill_down_is_an_exact_match_not_a_substring(tmp_path):
    """The free-text box is a substring match; the drill-down must not be, or
    'Otus' would drag in every scops-owl."""
    app, db = _app(tmp_path)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", n=2)
    _add(db, sci="Otus scops", common="Eurasian Scops-Owl", n=2)
    html = TestClient(app).get("/review?tab=detections&sci=Otus senegalensis").text
    rows = re.findall(r"id=\"audit-\d+\"", html)
    assert len(rows) == 2                       # only the two Otus senegalensis clips
    assert "Eurasian Scops-Owl" not in html.split("<ul")[-1]


def test_unreviewed_counts_only_clips_that_still_exist(tmp_path):
    """Retention prunes clips but keeps the rows. Counting every unlabelled row
    said 1.93M when only 720k could still be auditioned — a queue three times
    more hopeless than the real one."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Has clip", n=3, clip=True)
    _add(db, sci="B", common="Clip pruned", n=97, clip=False)
    assert db.review_backlog_total() == 3
    html = TestClient(app).get("/review?tab=detections").text
    assert "of 3 unreviewed" in html
    # ...and the pruned species is not offered for review at all.
    assert _listed(TestClient(app).get("/review").text) == ["A"]


def test_reviewed_species_drop_out_of_the_queue(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Done", n=2, label="good")
    _add(db, sci="B", common="Waiting", n=2)
    assert _listed(TestClient(app).get("/review").text) == ["B"]


def test_site_and_confidence_filters_narrow_the_species_index(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Loud Here", source="Cam1", conf=0.95, n=2)
    _add(db, sci="B", common="Quiet There", source="Cam2", conf=0.30, n=2)
    c = TestClient(app)
    assert _listed(c.get("/review?tab=species&source=Cam1").text) == ["A"]
    assert _listed(c.get("/review?tab=species&min_conf=0.5").text) == ["A"]


def test_the_filters_survive_the_click_through(tmp_path):
    """Picking a site then a species must not silently drop the site."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", n=2)
    html = TestClient(app).get("/review?tab=species&source=Cam1").text
    assert "A" in _listed(html)
    assert "source=Cam1" in html


def test_species_summary_reports_spread_and_reach(tmp_path):
    _, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", conf=0.30, n=2)
    _add(db, sci="A", common="Bird", source="Cam2", conf=0.95, n=3)
    row = db.review_species_summary()[0]
    assert row["n"] == 5
    assert row["sites"] == 2
    assert row["min_conf"] == 0.30 and row["max_conf"] == 0.95
    assert sum(row["bands"]) == 5          # every clip lands in exactly one band
    assert row["bands"][0] == 2 and row["bands"][-1] == 3


def test_ordering_options(tmp_path):
    _, db = _app(tmp_path)
    _add(db, sci="Few", common="Few", conf=0.99, n=1)
    _add(db, sci="Many", common="Many", conf=0.20, n=5)
    by_backlog = [r["scientific_name"] for r in db.review_species_summary(order="backlog")]
    by_conf = [r["scientific_name"] for r in db.review_species_summary(order="conf_desc")]
    assert by_backlog[0] == "Many"
    assert by_conf[0] == "Few"


def test_legacy_audition_still_lands_on_the_flat_list(tmp_path):
    app, _db = _app(tmp_path)
    r = TestClient(app).get("/audition", follow_redirects=False)
    assert r.status_code == 302
    assert "tab=detections" in r.headers["location"]


# --- reviewed counts, and the by-site level -------------------------------


def _sites_listed(html: str) -> list[str]:
    """Site names the by-site level lists, read off the drill-through links."""
    return re.findall(
        r"tab=detections&(?:amp;)?sci=[^\"&]+&(?:amp;)?source=([^\"&]+)",
        _strip_scripts(html),
    )


def test_the_species_index_shows_how_many_are_already_done(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", n=4)
    _add(db, sci="A", common="Bird", n=3, label="good")   # already reviewed
    assert db.reviewed_counts_by_species() == {"A": 3}
    row = next(r for r in db.review_species_summary() if r["scientific_name"] == "A")
    assert row["n"] == 4                                   # waiting excludes the done ones
    html = TestClient(app).get("/review").text
    assert "3 done" in html


def test_a_species_nobody_has_touched_shows_no_done_count(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", n=2)
    assert db.reviewed_counts_by_species() == {}
    # Match the "<n> done" badge its sibling test asserts, not a bare "done":
    # the modal markup on this page carries ids and labels of its own, and a
    # substring search over the whole document fails on any of them.
    assert not re.search(r"\d+ done", TestClient(app).get("/review").text)


def test_clicking_a_species_lands_on_its_sites_not_its_clips(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", n=5)
    _add(db, sci="A", common="Bird", source="Cam2", n=2)
    html = TestClient(app).get("/review").text
    assert "tab=sites&sci=A" in html

    sites = TestClient(app).get("/review?tab=sites&sci=A").text
    assert _sites_listed(sites) == ["Cam1", "Cam2"]        # busiest site first
    assert "Bird" in sites and "all species" in sites


def test_the_by_site_level_reports_each_sites_share(tmp_path):
    _, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", conf=0.4, n=5)
    _add(db, sci="A", common="Bird", source="Cam2", conf=0.9, n=1)
    rows = db.review_sites_for_species("A")
    assert [r["source_name"] for r in rows] == ["Cam1", "Cam2"]
    assert rows[0]["n"] == 5 and rows[1]["n"] == 1
    assert rows[1]["min_conf"] == 0.9


def test_a_site_row_opens_that_species_at_that_site(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", n=2)
    _add(db, sci="A", common="Bird", source="Cam2", n=3)
    html = TestClient(app).get("/review?tab=detections&sci=A&source=Cam2").text
    assert len(re.findall(r"id=\"audit-\d+\"", html)) == 3


def test_choosing_a_site_first_skips_the_by_site_step(tmp_path):
    """With a site already picked there is nothing left to break out, so the
    species row should go straight to the clips."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", n=2)
    html = TestClient(app).get("/review?tab=species&source=Cam1").text
    assert "tab=detections&sci=A" in html
    assert "tab=sites&sci=A" not in html


def test_the_by_site_level_needs_a_species(tmp_path):
    """/review?tab=sites with nothing to break out falls back rather than 500s."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", n=1)
    r = TestClient(app).get("/review?tab=sites")
    assert r.status_code == 200
    assert "By species" in r.text


def test_confidence_filter_carries_into_the_by_site_level(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Loud", conf=0.9, n=2)
    _add(db, sci="A", common="Bird", source="Quiet", conf=0.2, n=2)
    html = TestClient(app).get("/review?tab=sites&sci=A&min_conf=0.5").text
    assert _sites_listed(html) == ["Loud"]


# --- reaching the review queue from the species page -----------------------


def test_the_species_page_links_each_site_to_that_species_review_queue(tmp_path):
    """The By-site table's site name goes to the site's own page, which answers
    a different question and loses the species. The row now also carries a
    review link that keeps both halves."""
    app, db = _app(tmp_path)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", source="Twin Pan", n=3)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", source="Timbavati", n=2)

    html = TestClient(app).get("/species/Otus senegalensis").text
    for site in ("Twin%20Pan", "Timbavati"):
        assert f"/review?tab=detections&sci=Otus%20senegalensis&source={site}" in html
    # ...and the site's own page is still one click away.
    assert "/site/Twin%20Pan" in html


def test_that_link_lands_on_only_that_species_at_only_that_site(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", source="Twin Pan", n=3)
    _add(db, sci="Otus senegalensis", common="African Scops-Owl", source="Timbavati", n=2)
    _add(db, sci="Other sp", common="Other Bird", source="Twin Pan", n=4)

    html = TestClient(app).get(
        "/review?tab=detections&sci=Otus senegalensis&source=Twin Pan").text
    assert len(re.findall(r"id=\"audit-\d+\"", html)) == 3
    assert "Other Bird" not in html.split("<ul")[-1]


def test_the_site_filter_narrows_to_sites_holding_the_species(tmp_path):
    """Offering the whole roster once a species is chosen makes most of the
    menu a dead end — pick a site the bird has never been heard at and the
    queue comes back empty."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Everywhere", source="Cam1", n=2)
    _add(db, sci="A", common="Everywhere", source="Cam2", n=2)
    _add(db, sci="B", common="Elsewhere", source="Cam3", n=2)

    def sites(html):
        block = re.search(r'<select name="source".*?</select>', html, re.S).group(0)
        return re.findall(r'<option value="([^"]+)"', block)

    c = TestClient(app)
    assert sites(c.get("/review?tab=detections").text) == ["Cam1", "Cam2", "Cam3"]
    # Drilled into species A, Cam3 is gone — A has never been heard there.
    assert sites(c.get("/review?tab=detections&sci=A").text) == ["Cam1", "Cam2"]
    assert sites(c.get("/review?tab=detections&sci=B").text) == ["Cam3"]


def test_a_species_with_no_sites_falls_back_rather_than_emptying_the_filter(tmp_path):
    """An unknown species should not leave the operator with a filter that
    offers nothing at all."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", n=1)
    html = TestClient(app).get("/review?tab=detections&sci=Nothing here").text
    block = re.search(r'<select name="source".*?</select>', html, re.S).group(0)
    assert re.findall(r'<option value="([^"]+)"', block) == ["Cam1"]


def test_sites_for_species_is_scoped_and_sorted(tmp_path):
    _, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Zulu", n=1)
    _add(db, sci="A", common="Bird", source="Alpha", n=1)
    _add(db, sci="B", common="Other", source="Mike", n=1)
    assert db.sites_for_species("A") == ["Alpha", "Zulu"]
    assert db.sites_for_species("Nothing") == []


# --- the blank-queue hint --------------------------------------------------


def _empty_state(html: str) -> str:
    """The message the detections tab shows when it has no rows."""
    m = re.search(r'<p class="text-zinc-500 text-sm px-5 py-6">(.*?)</p>', html, re.S)
    return " ".join(re.sub(r"<[^>]+>", "", m.group(1)).split()) if m else ""


def test_a_queue_emptied_by_the_show_filter_says_so(tmp_path):
    """THE regression. A species whose only clips are already scored rendered
    a bare "No detections match these filters" under the default unreviewed
    filter, which reads as "we have nothing" when we have something. Rare
    birds hit this constantly -- one clip, already looked at."""
    app, db = _app(tmp_path)
    _add(db, sci="Zosterops eurycricotus", common="Kilimanjaro White-eye",
         conf=0.51, label="good")

    html = TestClient(app).get("/review?tab=detections&sci=Zosterops eurycricotus").text
    msg = _empty_state(html)
    assert "1 clip at another review state" in msg
    assert "show all" in msg
    assert "label_filter=all" in html


def test_the_hint_offers_a_link_that_actually_shows_them(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="Zosterops eurycricotus", common="Kilimanjaro White-eye",
         conf=0.51, label="good")
    c = TestClient(app)

    assert c.get("/review?tab=detections&sci=Zosterops eurycricotus").text.count(
        'id="audit-') == 0
    shown = c.get(
        "/review?tab=detections&label_filter=all&sci=Zosterops eurycricotus").text
    assert shown.count('id="audit-') == 1


def test_a_genuinely_empty_queue_does_not_claim_hidden_clips(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="Zosterops virens", common="Cape White-eye", conf=0.9)

    msg = _empty_state(
        TestClient(app).get("/review?tab=detections&sci=Nothing here").text)
    assert msg == "No detections match these filters."


def test_the_hint_counts_only_clips_matching_the_other_filters(tmp_path):
    """The count is the same query minus the label filter -- not a bare
    species total. Offering "show all" has to lead somewhere non-empty."""
    app, db = _app(tmp_path)
    _add(db, sci="Zosterops eurycricotus", common="Kilimanjaro White-eye",
         source="Tortilis", conf=0.51, label="good")
    _add(db, sci="Zosterops eurycricotus", common="Kilimanjaro White-eye",
         source="Elsewhere", conf=0.51, label="good", n=4)

    msg = _empty_state(TestClient(app).get(
        "/review?tab=detections&sci=Zosterops eurycricotus&source=Tortilis").text)
    assert "1 clip at another review state" in msg, msg


def test_unreviewed_clips_still_render_normally(tmp_path):
    """The hint must not fire when the default filter is doing its job."""
    app, db = _app(tmp_path)
    _add(db, sci="Zosterops eurycricotus", common="Kilimanjaro White-eye", n=2)

    html = TestClient(app).get("/review?tab=detections&sci=Zosterops eurycricotus").text
    assert html.count('id="audit-') == 2
    assert "at another review state" not in html


# --- confidence filters are per-page, not sticky ---------------------------


def test_the_species_drill_through_does_not_carry_the_confidence_filter(tmp_path):
    """A confidence set on the index used to follow the reviewer down every
    level, so clips vanished two pages later with nothing on screen saying
    why. Each level has its own boxes; the filter means "this page"."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", conf=0.9, n=2)

    html = TestClient(app).get("/review?min_conf=0.7&max_conf=0.95").text
    links = re.findall(r'href="(/review\?tab=(?:sites|detections)&sci=[^"]*)"', html)
    assert links, "expected at least one drill-through link"
    assert not any("min_conf" in u or "max_conf" in u for u in links), links


def test_the_by_site_drill_through_does_not_carry_it_either(tmp_path):
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", conf=0.9, n=2)

    html = TestClient(app).get("/review?tab=sites&sci=A&min_conf=0.7").text
    links = re.findall(r'href="(/review\?tab=detections&sci=[^"]*)"', html)
    assert links, "expected at least one site link"
    assert not any("min_conf" in u or "max_conf" in u for u in links), links


def test_the_filter_still_applies_on_the_page_it_is_set_on(tmp_path):
    """Dropping propagation must not disarm the control itself."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", conf=0.95)
    _add(db, sci="A", common="Bird", source="Cam1", conf=0.40)
    c = TestClient(app)

    assert c.get("/review?tab=detections&sci=A").text.count('id="audit-') == 2
    assert c.get("/review?tab=detections&sci=A&min_conf=0.9").text.count(
        'id="audit-') == 1


def test_the_show_all_hint_keeps_the_confidence_filter(tmp_path):
    """The opposite case: that link is "these same filters, every label", and
    its count is computed with the confidence filter applied — so it has to
    carry it or it would advertise a number its destination disagrees with."""
    app, db = _app(tmp_path)
    _add(db, sci="A", common="Bird", source="Cam1", conf=0.95, label="good")
    _add(db, sci="A", common="Bird", source="Cam1", conf=0.10, label="good")

    html = TestClient(app).get("/review?tab=detections&sci=A&min_conf=0.9").text
    assert "1 clip at another review state" in _empty_state(html)
    assert "min_conf=0.9" in html
