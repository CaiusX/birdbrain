"""The web app must not import TensorFlow.

The dashboard shares a package with the detector, so it is one stray
module-level import away from dragging birdnetlib -> TensorFlow into every
uvicorn worker. It did, via ``storage/db.py``, and the cost was not subtle: two
workers at 1.1-1.2GB on an 8GB Pi that was sitting in full swap, for a process
that runs inference on exactly one lazily-loaded admin path.

The mistake is easy to remake and invisible when you do — everything still
works, the box just runs out of memory later. Hence a test rather than a
comment. It mirrors ``test_windows_compat.py``: a cheap static/import guard for
a class of regression that has already happened once.
"""

from __future__ import annotations

import subprocess
import sys


def _modules_after_importing(target: str) -> set[str]:
    """Import ``target`` in a clean interpreter and report what came with it.

    A subprocess, because by the time this test runs pytest has very likely
    imported the detector for some other test — asking about *this* process
    would measure the test session, not the web app.
    """
    code = (
        "import sys, importlib;"
        f"importlib.import_module({target!r});"
        "print('\\n'.join(sorted(sys.modules)))"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert r.returncode == 0, f"importing {target} failed:\n{r.stderr[-2000:]}"
    return set(r.stdout.split())


def test_web_app_does_not_import_tensorflow():
    mods = _modules_after_importing("birdbrain.web.app")
    heavy = sorted(
        m for m in mods
        if m == "tensorflow" or m.startswith("tensorflow.")
        or m == "birdnetlib" or m.startswith("birdnetlib.")
    )
    assert not heavy, (
        "the web app is importing the detector stack again — that is ~1.3GB "
        "across the uvicorn workers for code the dashboard does not run. "
        f"Pulled in: {heavy[:5]}. Find the module-level import (it was "
        "storage/db.py) and put it behind TYPE_CHECKING or inside the function."
    )


def test_storage_alone_does_not_import_tensorflow():
    """storage is the layer that regressed, and the one most likely to again —
    it is imported by everything."""
    mods = _modules_after_importing("birdbrain.storage")
    assert not any(m.startswith(("tensorflow", "birdnetlib")) for m in mods)


def test_the_cli_entry_point_does_not_import_tensorflow():
    """The one that actually mattered, and the one the first version of this
    test missed. The service does not run ``birdbrain.web.app`` directly — it
    runs ``birdbrain web``, which goes through the Typer CLI. That module
    imported the pipeline at top level, so every subcommand loaded the detector
    and the dashboard was still paying ~250MB of resident TensorFlow after
    ``storage`` had been cleaned up.

    Check the real entry point, not the module you hope is the entry point.
    """
    mods = _modules_after_importing("birdbrain.cli")
    heavy = sorted(m for m in mods if m.startswith(("tensorflow", "birdnetlib")))
    assert not heavy, (
        f"the CLI imports the detector stack at module scope: {heavy[:5]}. "
        "Every subcommand pays for it, including `birdbrain web`."
    )


def test_the_pipeline_still_does_import_it():
    """The other half of the contract. Making storage lazy is only correct if
    the code that actually runs inference still gets the detector eagerly — a
    'fix' that broke detection would pass the tests above."""
    mods = _modules_after_importing("birdbrain.pipeline")
    assert "tensorflow" in mods, "the pipeline must still load the detector"
