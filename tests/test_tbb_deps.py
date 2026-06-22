from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _name(spec: str) -> str:
    """Distribution name from a requirement spec (strip version/extras)."""
    return re.split(r"[<>=!~\[ ]", spec.strip(), maxsplit=1)[0].lower()


def _req_names(path: Path) -> set[str]:
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(_name(line))
    return out


def test_tbb_requirements_track_pyproject_minus_tensorflow():
    """The unit profile must be the central deps with tensorflow swapped for
    tflite-runtime — guards against a new central dep silently missing on units."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    base = {_name(d) for d in pyproject["project"]["dependencies"]}
    unit = _req_names(ROOT / "deploy" / "tbb" / "requirements-tbb.txt")

    assert "tensorflow" in base               # central uses full TF
    assert "tensorflow" not in unit           # the unit does not
    assert "tflite-runtime" in unit           # ...it uses tflite-runtime instead
    # Every other central dependency is also present on the unit (no drift).
    missing = (base - {"tensorflow"}) - unit
    assert not missing, f"unit profile missing central deps: {sorted(missing)}"


def test_requires_python_allows_311():
    """The unit runs Python 3.11 (tflite-runtime has no cp312 wheel), so the
    package must declare >=3.11 or the editable install refuses to install."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=3.11"
