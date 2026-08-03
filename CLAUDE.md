# BirdBrain — contributor & agent notes

Real-time BirdNET detection from live wildlife streams. Primary deploy target is
a **Raspberry Pi 5** (dashboard + pipeline) plus an optional **Pi Zero 2 W field
bridge** (`birdnetcloud`/`tbb`). The dashboard, CLI, and pipeline are **also run
and developed on Windows and macOS** — see the Windows section of `INSTALL.md`.

## Commands

- Sync deps: `uv sync`
- Tests: `uv run pytest -q`
- Lint: `uv run ruff check src tests`
- Smoke-test sources: `uv run birdbrain probe -s sources.toml`
- Run locally: `uv run birdbrain run` (pipeline) and, in a second terminal,
  `uv run birdbrain web --host 0.0.0.0 --port 8765` (dashboard → http://localhost:8765/).
  Note: `0.0.0.0` is a *bind* address — browse to `localhost`/`127.0.0.1`, never `0.0.0.0`.

## Keep the dashboard, CLI, and pipeline Windows-compatible

Because the everyday deploy target is Linux, it's easy to reach for Linux-only
APIs that silently break the Windows setup we also support. `tests/test_windows_compat.py`
statically guards the two classes that have already bitten us — it runs on **any**
OS (including the Pi), so regressions are caught even when the suite runs on Linux.
Before using a platform API:

- **No Unix-only stdlib at module scope.** `fcntl`, `termios`, `pwd`, `grp`,
  `resource`, `syslog`, … don't exist on Windows and make the whole module
  unimportable (`ModuleNotFoundError` at import time). Guard the import behind
  `try/except ImportError` and provide a cross-platform path. For advisory file
  locking use `web/app.py::_acquire_singleton_lock` (fcntl on Unix,
  `msvcrt.locking` on Windows, sole-process fallback otherwise).
- **No glibc `%-d` strftime codes through a raw `.strftime()`.** The `-`
  (strip-leading-zero) code raises `ValueError: Invalid format string` on the
  Windows C runtime, which spells the same thing `%#d`. Format dates through
  `web/app.py::_portable_strftime` or the `strftime` Jinja filter — both rewrite
  `%-` → `%#` on Windows. (`%-d` *inside* those calls is fine; a raw
  `dt.strftime("%-d")` is not.)
- More broadly: use `pathlib`/`os.path` rather than hardcoded `/` paths, and
  avoid `os.fork`, `signal.SIGHUP`, and POSIX-only `subprocess` assumptions in
  any code the dashboard or pipeline reaches.

**Exempt — Pi-only by design:** the `birdnetcloud`/`tbb` **Pi Zero field bridge**
and its host telemetry (`birdnetcloud_sync.py::host_info` reads `/proc`, `/sys`,
`/etc/os-release`). That hardware runs only on the Pi, so its Linux-specific
probes — and the tests asserting them (`test_birdnetcloud_sync.py`) — are not
expected to pass off-Pi. Don't Windows-proof the bridge; do keep the dashboard,
CLI, and pipeline green.
