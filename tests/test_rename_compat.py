"""Guards for the africam -> birdbrain transition shims.

These exist because both failure modes of the rename are SILENT. A missed
env var falls back to a field default, so a unit quietly stops syncing rather
than crashing; and a db_url pointing at a filename that does not exist makes
SQLite create a fresh empty database next to the real one, so the site comes up
looking merely quiet.

Delete this file only together with the shims themselves, once every unit
reports on BIRDBRAIN_* and the databases have been renamed.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from birdbrain.config import DEFAULT_DB_URL, LEGACY_DB_PATH, AppConfig


@pytest.fixture
def cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path


def _cfg(**kw):
    return AppConfig(sources_file="none.toml", sites_file="none.toml", **kw)


def test_fresh_install_uses_the_new_name(cwd):
    assert _cfg().db_url == DEFAULT_DB_URL


def test_falls_back_to_the_pre_rename_database(cwd):
    """The important one: never silently create an empty DB beside a real one."""
    (cwd / LEGACY_DB_PATH).write_text("")
    assert _cfg().db_url == f"sqlite:///{LEGACY_DB_PATH}"


def test_prefers_the_new_database_once_it_exists(cwd):
    (cwd / LEGACY_DB_PATH).write_text("")
    (cwd / "data/birdbrain.sqlite").write_text("")
    assert _cfg().db_url == DEFAULT_DB_URL


def test_an_explicit_db_url_is_never_rewritten(cwd):
    """Tests and real deployments pass explicit paths; a stray africam.sqlite in
    the working directory must not hijack them."""
    (cwd / LEGACY_DB_PATH).write_text("")
    explicit = f"sqlite:///{cwd/'elsewhere.sqlite'}"
    assert _cfg(db_url=explicit).db_url == explicit


def test_legacy_env_prefix_still_works(cwd, monkeypatch):
    monkeypatch.setenv("AFRICAM_TBB_UNIT_ID", "legacy-unit")
    assert _cfg().tbb_unit_id == "legacy-unit"


def test_new_env_prefix_wins_over_legacy(cwd, monkeypatch):
    monkeypatch.setenv("AFRICAM_TBB_UNIT_ID", "legacy-unit")
    monkeypatch.setenv("BIRDBRAIN_TBB_UNIT_ID", "new-unit")
    assert _cfg().tbb_unit_id == "new-unit"


def test_legacy_env_works_for_non_string_fields(cwd, monkeypatch):
    """A unit's .env carries bools and floats too — sync_enabled reverting to
    its default is exactly the silent failure this guards against."""
    monkeypatch.setenv("AFRICAM_TBB_SYNC_ENABLED", "true")
    monkeypatch.setenv("AFRICAM_TBB_LAT", "-26.124")
    cfg = _cfg()
    assert cfg.tbb_sync_enabled is True
    assert cfg.tbb_lat == pytest.approx(-26.124)


def test_console_script_alias_is_declared():
    """The fleet's systemd units still exec `.venv/bin/africam`; dropping this
    alias strands every unit on the next auto-update."""
    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    assert 'birdbrain = "birdbrain.cli:app"' in pyproject
    assert 'africam = "birdbrain.cli:app"' in pyproject


def test_package_is_importable_under_the_new_name():
    import birdbrain
    import birdbrain.cli  # noqa: F401

    assert birdbrain.__name__ == "birdbrain"

# NB: "is the old `africam` distribution gone from the venv?" is deliberately
# NOT asserted here. It is a property of the installed environment, not of this
# source tree, so it fails spuriously when the suite runs from a git worktree
# against the host venv. It is verified during the cutover instead:
#     uv pip uninstall africam && python -c "import africam"   # must fail
