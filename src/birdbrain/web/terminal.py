"""PTY-backed shell sessions for the admin console.

A real terminal needs a pseudo-terminal, and PTYs are a Unix concept — ``pty``,
``termios`` and ``fcntl`` do not exist on Windows, where this project is also
supported (see CLAUDE.md). So every one of those imports is guarded and this
module degrades to :data:`SUPPORTED` = False rather than making the whole web
app unimportable. Callers must check :func:`available` before starting a
session; the route does, and returns 404 when it is False.

Security posture — read before changing anything here:

* A session is an interactive shell running as the web service's own user. It
  is exactly as privileged as that account. There is no sandbox, and adding one
  is not what this is for.
* The endpoint is therefore **opt-in** (``BIRDBRAIN_TERMINAL_ENABLED``) and off
  by default. BirdBrain is self-hosted by other people; a shell that ships on by
  default would hand every one of those deployments an RCE they never asked for.
* Starlette's ``@app.middleware("http")`` does **not** run for WebSocket
  connections. The public-tunnel gate is such a middleware, so it cannot protect
  the socket — the WebSocket route authenticates the session itself. Do not
  assume middleware coverage here.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import struct

from birdbrain.logging import get_logger

try:  # Unix only — see the module docstring.
    import fcntl
    import pty
    import termios

    SUPPORTED = True
except ImportError:  # pragma: no cover - exercised on Windows, not on the Pi
    fcntl = pty = termios = None  # type: ignore[assignment]
    SUPPORTED = False

log = get_logger(__name__)

# Read chunk size off the master fd. Big enough that `cat`-ing a file doesn't
# thrash the event loop, small enough to stay interactive.
_READ_BYTES = 65_536

# Hard ceiling on concurrent sessions. Each holds a shell and a PTY; this is an
# admin console, not a shell host, so the limit is deliberately small.
MAX_SESSIONS = 4


def available() -> bool:
    """True if this platform can host a PTY session at all."""
    return SUPPORTED


def default_shell() -> str:
    """The shell to spawn: $SHELL if it looks usable, else bash, else sh."""
    env_shell = os.environ.get("SHELL")
    if env_shell and os.path.isfile(env_shell) and os.access(env_shell, os.X_OK):
        return env_shell
    return shutil.which("bash") or shutil.which("sh") or "/bin/sh"


class TerminalSession:
    """One interactive shell attached to a PTY.

    Output is pumped off the master fd by the event loop (``add_reader``) into a
    queue, rather than by a thread per session — a blocking ``os.read`` in an
    executor would pin a worker thread for the whole life of the session.
    """

    def __init__(self, *, shell: str | None = None, cwd: str | None = None) -> None:
        if not SUPPORTED:  # pragma: no cover - guarded by the caller
            raise RuntimeError("PTY sessions are not supported on this platform")
        self.shell = shell or default_shell()
        self.cwd = cwd or os.path.expanduser("~")
        self._master: int | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        self._closed = False

    async def start(self) -> None:
        master, slave = pty.openpty()
        # The child gets its own session + controlling terminal, so job control
        # and Ctrl-C behave. Without start_new_session the shell shares our
        # process group and a signal aimed at the child hits the web server.
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env.pop("BIRDBRAIN_TERMINAL_ENABLED", None)  # don't leak the toggle in
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.shell, "-i",
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, cwd=self.cwd, env=env,
            )
        finally:
            # The child holds its own dup of the slave; ours would otherwise keep
            # the PTY open forever and the reader would never see EOF on exit.
            os.close(slave)
        self._master = master
        os.set_blocking(master, False)
        asyncio.get_running_loop().add_reader(master, self._on_readable)
        log.info("terminal.started", shell=self.shell, pid=self._proc.pid)

    def _on_readable(self) -> None:
        """Event-loop callback: drain what's ready, queue it, notice EOF."""
        assert self._master is not None
        try:
            data = os.read(self._master, _READ_BYTES)
        except BlockingIOError:
            return
        except OSError:
            data = b""  # PTY torn down — treat as EOF
        if not data:
            self._signal_eof()
            return
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            # A firehose (`yes`, a huge cat) outrunning the socket. Dropping is
            # the right failure: the alternative is unbounded memory growth on
            # the Pi. The user sees a gap, not a dead session.
            log.warning("terminal.output_dropped", bytes=len(data))

    def _signal_eof(self) -> None:
        if self._master is not None:
            # OSError: fd already gone. RuntimeError: no running loop (shutdown).
            with contextlib.suppress(OSError, RuntimeError):
                asyncio.get_running_loop().remove_reader(self._master)
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    async def read(self) -> bytes | None:
        """Next chunk of shell output, or None once the shell has exited."""
        return await self._queue.get()

    def write(self, data: bytes) -> None:
        if self._master is None or self._closed:
            return
        try:
            os.write(self._master, data)
        except OSError:
            self._signal_eof()

    def resize(self, rows: int, cols: int) -> None:
        """Push the browser's window size into the PTY so curses apps (htop,
        less, vim) lay out correctly. Silently ignored if the PTY has gone."""
        if self._master is None or self._closed:
            return
        rows = max(1, min(int(rows), 500))
        cols = max(1, min(int(cols), 500))
        with contextlib.suppress(OSError):  # racing a closing PTY
            fcntl.ioctl(
                self._master, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )

    async def close(self) -> None:
        """Tear down the shell and the PTY. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if self._master is not None:
            with contextlib.suppress(OSError, RuntimeError):
                asyncio.get_running_loop().remove_reader(self._master)
        proc = self._proc
        if proc is not None and proc.returncode is None:
            # SIGHUP to the whole process group, as a real terminal hangup would
            # — otherwise a backgrounded child outlives the session.
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:  # pragma: no cover - shell ignoring SIGHUP
                proc.kill()
                await proc.wait()
        if self._master is not None:
            with contextlib.suppress(OSError):
                os.close(self._master)
            self._master = None
        log.info("terminal.closed")
