"""Remote /admin access and the browser terminal.

The public-tunnel gate used to be absolute: /admin was 404 over the tunnel for
everyone, so nothing in the app had to reason about privilege. Now that an
admin-role account can reach it remotely, these tests pin the three conditions
that keep the shell shut — role, opt-in config, and platform support — and, in
particular, that the WebSocket enforces them *itself*. Starlette HTTP middleware
does not run for WebSocket connections, so the tunnel gate cannot cover the
socket; if that check regresses, the shell is reachable while /admin looks shut.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from birdbrain.config import AppConfig
from birdbrain.storage import Database
from birdbrain.web import auth as auth_mod
from birdbrain.web import terminal
from birdbrain.web.app import create_app

# Cloudflare stamps this on every proxied request; its presence is what the app
# uses to mean "this arrived over the public tunnel".
PUBLIC = {"cf-connecting-ip": "203.0.113.7"}


def _app(tmp_path, **over):
    cfg = AppConfig(
        db_url=f"sqlite:///{tmp_path / 'c.sqlite'}",
        clips_dir=tmp_path / "clips",
        sources_file=tmp_path / "none.toml",
        sites_file=tmp_path / "none.toml",
        media_cache_enabled=False,
        secret_key="test-key-not-secret",
        **over,
    )
    return create_app(cfg), Database(cfg.db_url)


def _user(db, name, role):
    db.create_user(name, auth_mod.hash_password("correct horse battery"), role=role)
    return db.get_user_by_username(name)


def _login(client, name):
    return client.post(
        "/auth/login",
        data={"username": name, "password": "correct horse battery"},
        follow_redirects=False,
    )


def test_is_admin_is_total_on_none():
    """The gate calls this with request.state.user, which is None when nobody is
    logged in — it must not raise there."""
    assert auth_mod.is_admin(None) is False


def test_admin_over_tunnel_needs_an_admin_role(tmp_path):
    app, db = _app(tmp_path)
    _user(db, "caiusx", "operator")
    _user(db, "alice", "tester")
    client = TestClient(app)

    # Anonymous over the tunnel: still 404, as before.
    assert client.get("/admin", headers=PUBLIC).status_code == 404

    # A logged-in non-admin gets no more than an anonymous visitor.
    _login(client, "alice")
    assert client.get("/admin", headers=PUBLIC).status_code == 404
    client.post("/auth/logout", follow_redirects=False)

    # The admin account gets through.
    _login(client, "caiusx")
    assert client.get("/admin", headers=PUBLIC).status_code == 200


def test_admin_on_lan_is_unchanged_for_everyone(tmp_path):
    """No cf-connecting-ip means LAN/localhost, which was never gated and must
    not become gated by this change."""
    app, db = _app(tmp_path)
    _user(db, "alice", "tester")
    client = TestClient(app)
    assert client.get("/admin").status_code == 200  # anonymous, on the LAN
    _login(client, "alice")
    assert client.get("/admin").status_code == 200


def test_terminal_is_off_by_default(tmp_path):
    """Opt-in: an admin on the LAN still gets nothing until it's switched on."""
    app, db = _app(tmp_path)
    _user(db, "caiusx", "operator")
    client = TestClient(app)
    _login(client, "caiusx")
    assert client.get("/admin/terminal").status_code == 404


@pytest.mark.skipif(not terminal.available(), reason="no PTY on this platform")
def test_terminal_page_requires_admin_even_when_enabled(tmp_path):
    app, db = _app(tmp_path, terminal_enabled=True)
    _user(db, "caiusx", "operator")
    _user(db, "alice", "tester")
    client = TestClient(app)

    assert client.get("/admin/terminal").status_code == 404  # anonymous
    _login(client, "alice")
    assert client.get("/admin/terminal").status_code == 404  # non-admin
    client.post("/auth/logout", follow_redirects=False)
    _login(client, "caiusx")
    assert client.get("/admin/terminal").status_code == 200


def test_terminal_websocket_refuses_without_an_admin_session(tmp_path):
    """The load-bearing one. HTTP middleware never sees a WebSocket handshake,
    so this check lives in the route; if it goes, the socket is wide open."""
    app, db = _app(tmp_path, terminal_enabled=True)
    _user(db, "alice", "tester")
    client = TestClient(app)

    # Anonymous, then logged in as a non-admin. Neither may open the socket.
    for who in (None, "alice"):
        if who:
            _login(client, who)
        with pytest.raises(WebSocketDisconnect), \
                client.websocket_connect("/admin/terminal/ws"):
            pass


def test_terminal_websocket_refuses_when_disabled_even_for_admin(tmp_path):
    """Admin role is necessary but not sufficient — the opt-in still governs."""
    app, db = _app(tmp_path)  # terminal_enabled defaults False
    _user(db, "caiusx", "operator")
    client = TestClient(app)
    _login(client, "caiusx")
    with pytest.raises(WebSocketDisconnect), \
            client.websocket_connect("/admin/terminal/ws"):
        pass


@pytest.mark.skipif(not terminal.available(), reason="no PTY on this platform")
def test_pty_session_roundtrips_a_command():
    """The PTY itself works: spawn a shell, run something, see the output.

    Driven through asyncio.run rather than an async test — the project has no
    pytest-asyncio, and adding a plugin for two tests isn't worth it.
    """
    async def go() -> str:
        session = terminal.TerminalSession(shell="/bin/sh")
        await session.start()
        try:
            session.write(b"echo hello-from-pty\n")
            seen = ""
            while "hello-from-pty" in seen or len(seen) < 4096:
                chunk = await asyncio.wait_for(session.read(), timeout=10)
                if chunk is None:
                    break
                seen += chunk.decode("utf-8", errors="replace")
                if "hello-from-pty" in seen:
                    break
            return seen
        finally:
            await session.close()

    assert "hello-from-pty" in asyncio.run(go())


@pytest.mark.skipif(not terminal.available(), reason="no PTY on this platform")
def test_pty_session_close_reaps_the_shell():
    """close() must not leave an orphaned shell behind — the session is the only
    thing holding the process, so a leak here is a leak for the life of the web
    process."""
    async def go() -> int | None:
        session = terminal.TerminalSession(shell="/bin/sh")
        await session.start()
        proc = session._proc
        await session.close()
        return proc.returncode

    assert asyncio.run(go()) is not None
