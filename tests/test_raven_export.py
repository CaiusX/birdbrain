"""Raven Pro export: a sound selection table plus the audio it points at.

The geometry is the whole game. A selection table whose boxes sit 3 s off the
call is worse than no export — Raven will happily draw them and the reviewer
will believe them.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from birdbrain.audio.locator import DEFAULT_BAND, SPECIES_FREQ_BANDS
from birdbrain.config import AppConfig
from birdbrain.detector.birdnet import Detection
from birdbrain.raven import COLUMNS, Clip, band_for, selection_table
from birdbrain.storage import Database, DetectionRow
from birdbrain.web.app import create_app

SCI, COMMON = "Otus senegalensis", "African Scops-Owl"


def _app(tmp_path):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
        media_cache_enabled=False,
    )
    (tmp_path / "clips").mkdir(exist_ok=True)
    return create_app(cfg), Database(cfg.db_url), cfg


def _clip_file(cfg, name: str, seconds: float = 6.0) -> Path:
    p = cfg.clips_dir / name
    sf.write(p, np.zeros(int(seconds * 24000), dtype="float32"), 24000, format="OGG",
             subtype="VORBIS")
    return p


def _add(db, cfg, *, name, source="Twin Pan", conf=0.9, seconds=6.0, sci=SCI,
         common=COMMON, label=None, at=None):
    path = _clip_file(cfg, name, seconds)
    db.insert_detections(
        [Detection(source_name=source, started_at=at or datetime.now(UTC), duration_s=3.0,
                   scientific_name=sci, common_name=common, confidence=conf)],
        clip_path=str(path),
    )
    if label:
        with db.session() as s, s.begin():
            row = s.query(DetectionRow).filter(DetectionRow.clip_path == str(path)).one()
            row.label = label
    return path


# --- the table itself ------------------------------------------------------


def _rows(table: str) -> list[dict]:
    """Parse the table without eating the trailing empty Label field — a row
    whose last column is blank legitimately ends in a tab."""
    lines = [ln for ln in table.split("\n") if ln]
    head = lines[0].split("\t")
    return [dict(zip(head, ln.split("\t"), strict=True)) for ln in lines[1:]]


def test_the_header_is_tab_delimited_with_ravens_required_columns():
    """Raven 1.6 refuses a header without real delimiters between the column
    names — the bug that makes BirdNET-Analyzer's own Raven export unreadable."""
    t = selection_table([Clip(1, Path("a.ogg"), 6.0, 3.0, SCI, COMMON, 0.9, "Twin Pan", "x")])
    head = t.split("\n")[0]
    assert "\t" in head
    assert head.split("\t") == COLUMNS
    for required in ("Selection", "View", "Channel", "Begin Time (s)", "End Time (s)",
                     "Low Freq (Hz)", "High Freq (Hz)", "Begin File", "File Offset (s)"):
        assert required in COLUMNS


def test_the_box_covers_the_last_window_of_the_clip():
    """The pipeline prepends 3 s of pre-roll, so the window BirdNET fired on is
    the end of the file, not the start. Deriving it from the clip's filename
    instead lands 3 s out on every clip pushed from an ingest node, because
    central names those after the detection rather than the audio's start.
    """
    r = _rows(selection_table(
        [Clip(1, Path("a.ogg"), 6.0, 3.0, SCI, COMMON, 0.9, "Twin Pan", "x")]))[0]
    assert r["File Offset (s)"] == "3.0000"
    assert r["Begin Time (s)"] == "3.0000"
    assert r["End Time (s)"] == "6.0000"


def test_a_clip_with_no_pre_roll_boxes_the_whole_file():
    """The first chunk after a reconnect has nothing to prepend, so its clip is
    just the window. Measuring from the end still gets it right."""
    r = _rows(selection_table(
        [Clip(1, Path("a.ogg"), 3.0, 3.0, SCI, COMMON, 0.9, "Twin Pan", "x")]))[0]
    assert r["File Offset (s)"] == "0.0000"
    assert (r["Begin Time (s)"], r["End Time (s)"]) == ("0.0000", "3.0000")


def test_begin_time_accumulates_across_the_file_sequence():
    """Raven opens the clips as one sequence, so Begin Time is cumulative while
    File Offset stays relative to each file."""
    clips = [Clip(i, Path(f"{i}.ogg"), 6.0, 3.0, SCI, COMMON, 0.9, "Twin Pan", "x")
             for i in range(1, 4)]
    rows = _rows(selection_table(clips))
    assert [r["Begin Time (s)"] for r in rows] == ["3.0000", "9.0000", "15.0000"]
    assert [r["End Time (s)"] for r in rows] == ["6.0000", "12.0000", "18.0000"]
    assert {r["File Offset (s)"] for r in rows} == {"3.0000"}


def test_mixed_clip_lengths_still_accumulate_correctly():
    clips = [Clip(1, Path("a.ogg"), 3.0, 3.0, SCI, COMMON, 0.9, "S", "x"),
             Clip(2, Path("b.ogg"), 6.0, 3.0, SCI, COMMON, 0.9, "S", "x")]
    rows = _rows(selection_table(clips))
    assert rows[0]["Begin Time (s)"] == "0.0000"      # 3 s file, window is all of it
    assert rows[1]["Begin Time (s)"] == "6.0000"      # 3 + offset 3 into the second
    assert rows[1]["File Offset (s)"] == "3.0000"


def test_the_band_is_the_one_the_review_page_draws():
    known = next(iter(SPECIES_FREQ_BANDS))
    assert band_for(known) == SPECIES_FREQ_BANDS[known]
    assert band_for("Nothing at all") == DEFAULT_BAND
    r = _rows(selection_table(
        [Clip(1, Path("a.ogg"), 6.0, 3.0, known, "x", 0.9, "S", "x")]))[0]
    lo, hi = SPECIES_FREQ_BANDS[known]
    assert (float(r["Low Freq (Hz)"]), float(r["High Freq (Hz)"])) == (lo, hi)


def test_our_verdict_travels_out_and_has_somewhere_to_come_back():
    rows = _rows(selection_table([
        Clip(1, Path("a.ogg"), 6.0, 3.0, SCI, COMMON, 0.9, "Twin Pan", "t", label="good"),
        Clip(2, Path("b.ogg"), 6.0, 3.0, SCI, COMMON, 0.8, "Twin Pan", "t"),
    ]))
    assert rows[0]["Label"] == "good"
    assert rows[1]["Label"] == ""          # empty, for Raven to fill
    assert rows[0]["Detection ID"] == "1"  # the join back


# --- the endpoint ----------------------------------------------------------


def test_the_export_is_a_zip_of_the_table_and_its_audio(tmp_path):
    app, db, cfg = _app(tmp_path)
    for i in range(3):
        _add(db, cfg, name=f"c{i}.ogg", at=datetime.now(UTC) - timedelta(minutes=i))
    r = TestClient(app).get(f"/admin/raven/export?sci={SCI}&source=Twin Pan")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    table = next(n for n in names if n.endswith(".selections.txt"))
    audio = [n for n in names if n.startswith("audio/")]
    assert len(audio) == 3
    rows = _rows(z.read(table).decode())
    assert len(rows) == 3
    # Every row names a file that is actually in the zip.
    for row in rows:
        assert f"audio/{row['Begin File']}" in names


def test_table_only_skips_the_audio(tmp_path):
    app, db, cfg = _app(tmp_path)
    _add(db, cfg, name="c.ogg")
    r = TestClient(app).get(f"/admin/raven/export?sci={SCI}&source=Twin Pan&audio=false")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert not [n for n in z.namelist() if n.startswith("audio/")]
    assert any(n.endswith(".selections.txt") for n in z.namelist())


def test_filters_narrow_the_export(tmp_path):
    app, db, cfg = _app(tmp_path)
    _add(db, cfg, name="hi.ogg", conf=0.95)
    _add(db, cfg, name="lo.ogg", conf=0.30)
    _add(db, cfg, name="elsewhere.ogg", conf=0.95, source="Other Site")
    _add(db, cfg, name="other.ogg", conf=0.95, sci="Other sp", common="Other")

    def n(qs):
        z = zipfile.ZipFile(io.BytesIO(TestClient(app).get(qs).content))
        return len([x for x in z.namelist() if x.startswith("audio/")])

    assert n(f"/admin/raven/export?sci={SCI}&source=Twin Pan") == 2
    assert n(f"/admin/raven/export?sci={SCI}&source=Twin Pan&min_conf=0.5") == 1


def test_a_pruned_clip_is_left_out_rather_than_pointed_at(tmp_path):
    """Retention deletes clip files. A table row naming a missing file opens in
    Raven as an error, so those rows never make it into the export."""
    app, db, cfg = _app(tmp_path)
    keep = _add(db, cfg, name="keep.ogg")
    gone = _add(db, cfg, name="gone.ogg")
    gone.unlink()
    z = zipfile.ZipFile(io.BytesIO(
        TestClient(app).get(f"/admin/raven/export?sci={SCI}&source=Twin Pan").content))
    audio = [n for n in z.namelist() if n.startswith("audio/")]
    assert audio == [f"audio/{keep.name}"]


def test_an_empty_selection_is_a_404_not_an_empty_zip(tmp_path):
    app, db, cfg = _app(tmp_path)
    _add(db, cfg, name="c.ogg", conf=0.2)
    r = TestClient(app).get(f"/admin/raven/export?sci={SCI}&source=Twin Pan&min_conf=0.9")
    assert r.status_code == 404


def test_the_admin_page_offers_only_sites_holding_the_species(tmp_path):
    app, db, cfg = _app(tmp_path)
    _add(db, cfg, name="a.ogg", source="Twin Pan")
    _add(db, cfg, name="b.ogg", source="Elsewhere", sci="Other sp", common="Other")
    html = TestClient(app).get(f"/admin/raven?sci={SCI}").text
    assert "Twin Pan" in html
    assert ">Elsewhere<" not in html


def test_the_admin_page_loads_with_nothing_chosen(tmp_path):
    app, db, cfg = _app(tmp_path)
    _add(db, cfg, name="a.ogg")
    r = TestClient(app).get("/admin/raven")
    assert r.status_code == 200
    assert "pick a species" in r.text
