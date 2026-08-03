"""Static guards against Unix-isms that silently break the Windows build.

BirdBrain is developed and deployed on Raspberry Pi (Linux), so it's easy to
reach for Linux-only APIs that fail to import or 500 on Windows -- where the
project is also supported (see INSTALL.md's Windows section). These checks scan
the source statically, so they run on *any* platform and catch regressions even
when the suite runs on the Pi.

If one fires, don't weaken the check -- fix the code:

  * Unix-only module -> guard the import behind ``try/except ImportError`` and
    provide a cross-platform path. See ``web/app.py``'s ``_acquire_singleton_lock``
    (fcntl on Unix, msvcrt on Windows) for the pattern.
  * strftime ``%-d`` -> the ``-`` ("strip leading zero") code is glibc-only and
    raises ``ValueError: Invalid format string`` on the Windows C runtime, which
    spells it ``%#d``. Route the format through ``web.app._portable_strftime`` or
    the ``strftime`` Jinja filter (both rewrite ``%-`` -> ``%#`` on Windows)
    instead of calling ``.strftime()`` on the format literal directly.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src" / "birdbrain"

# stdlib modules with no Windows implementation; importing one at module scope
# makes the whole module unimportable there (ModuleNotFoundError at import time).
_UNIX_ONLY_MODULES = (
    "fcntl",
    "termios",
    "pwd",
    "grp",
    "resource",
    "syslog",
    "posix",
    "tty",
    "crypt",
    "spwd",
    "nis",
    "readline",
)

# Match only *top-level* imports (column 0). A guarded import inside
# ``try: import fcntl`` or a lazy import inside a function is indented, so it
# won't match -- that's the allowed cross-platform pattern.
_TOP_LEVEL_UNIX_IMPORT = re.compile(
    r"^(?:import|from)\s+(?:" + "|".join(_UNIX_ONLY_MODULES) + r")\b",
    re.MULTILINE,
)

# A raw ``.strftime(...)`` whose format literal carries a glibc ``%-`` code.
# Deliberately does NOT match ``_portable_strftime(x, "%-d")`` or the Jinja
# ``| strftime('%-d')`` filter -- those are the sanctioned, Windows-safe routes.
_RAW_DASH_STRFTIME = re.compile(r"\.strftime\(\s*[\"'][^\"']*%-")


def _py_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _all_source_files() -> list[Path]:
    return sorted([*_SRC.rglob("*.py"), *_SRC.rglob("*.html")])


def test_no_top_level_unix_only_imports() -> None:
    offenders = []
    for f in _py_files():
        for m in _TOP_LEVEL_UNIX_IMPORT.finditer(f.read_text(encoding="utf-8")):
            offenders.append(f"{f.relative_to(_REPO)}: {m.group(0)}")
    assert not offenders, (
        "Unix-only stdlib module imported at module scope -- this breaks "
        "`import` on Windows. Guard it behind try/except ImportError with a "
        "cross-platform fallback (see web/app.py _acquire_singleton_lock):\n  "
        + "\n  ".join(offenders)
    )


def test_no_raw_dash_strftime() -> None:
    offenders = []
    for f in _all_source_files():
        text = f.read_text(encoding="utf-8")
        for m in _RAW_DASH_STRFTIME.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{f.relative_to(_REPO)}:{line}")
    assert not offenders, (
        'Raw .strftime("...%-...") raises "Invalid format string" on Windows. '
        "Use web.app._portable_strftime or the `strftime` Jinja filter "
        "(they rewrite %- -> %# on Windows):\n  " + "\n  ".join(offenders)
    )
