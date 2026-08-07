# Remote admin access (and the browser terminal)

How to reach `/admin` — and optionally a shell — on a publicly-exposed BirdBrain,
without handing the internet a way in.

Until 2026-08-07, `/admin` was simply unreachable over the public tunnel: the
gate in `web/app.py::restrict_public` returned 404 for `/admin*` whenever
Cloudflare's `CF-Connecting-IP` header was present. That was safe and required no
thought about privilege. Making it reachable means privilege now has to be real,
so read this before turning anything on.

## The threat model in one paragraph

The terminal is an interactive shell running as the web service's own user
(`dennis` on the reference Pi). It is not sandboxed, and it is not meant to be —
a sandboxed shell would not be useful for the job it exists to do. Anyone who
reaches it owns the machine: the detection database, the Cloudflare tunnel token,
the unit device tokens, the lot. Every control below exists to keep that
reachable by exactly one person.

## Layers

Three independent things must all hold before a shell exists. Two are in the
app; the third is the one that actually matters and lives outside it.

1. **Opt-in.** `BIRDBRAIN_TERMINAL_ENABLED=1`. Off by default. BirdBrain is
   self-hosted by other people, and a shell that shipped on by default would be
   an RCE none of those deployments asked for.
2. **Admin role.** The account's `users.role` must be in
   `web.auth.ADMIN_ROLES` (currently just `operator`). Set it with
   `birdbrain set-role <user> --role operator`.
3. **An edge authenticator.** Cloudflare Access in front of `/admin`, below.

Layers 1 and 2 are a single password. That is not enough on its own for a
remotely-reachable admin console — there is no 2FA, no lockout, and the session
cookie is set `https_only=False` so it also works over plain HTTP on the LAN.
Layer 3 is what makes this defensible; 1 and 2 are defence in depth behind it.

## Setting up Cloudflare Access

The tunnel here is a token-managed one (`cloudflared.service` runs
`tunnel run --token …`), so the policy is configured in the dashboard, not in a
local file. This has to be done by hand — it needs Cloudflare credentials.

1. Cloudflare dashboard → **Zero Trust** → **Access** → **Applications** →
   *Add an application* → **Self-hosted**.
2. **Application domain:** `birdbrain.co.za`, path `admin`. Add a second
   application for path `admin/terminal` if you want a stricter policy on the
   shell than on the rest of admin (recommended: require a fresh login).
3. **Session duration:** short. 24h for `/admin`, 1h or less for the terminal.
4. **Policy:** action *Allow*, rule `Emails` → your address. Do **not** use
   "Everyone" plus a login method — that authenticates anyone with any Google
   account, which is not the same as authenticating you.
5. Add a second **Block** policy for `Everyone` beneath it, so the default is
   deny if the allow rule ever stops matching.
6. Enable a second factor on the identity provider you pick. Access is only as
   strong as the IdP behind it.

Verify: open `https://birdbrain.co.za/admin` in a private window. You should get
Cloudflare's login screen *before* anything BirdBrain-shaped appears. If you see
the dashboard's own login form first, the Access policy is not matching the path.

## Turning the terminal on

```sh
birdbrain set-role caiusx --role operator          # layer 2
systemctl --user edit birdbrain-web.service        # layer 1
#   [Service]
#   Environment=BIRDBRAIN_TERMINAL_ENABLED=1
systemctl --user restart birdbrain-web.service
```

Then `https://birdbrain.co.za/admin/terminal`.

## What the app enforces, and one trap

`/admin` over the tunnel now requires an admin-role session; everyone else still
gets 404 (not 403, so the tunnel doesn't confirm the route exists). `/admin` on
the LAN is unchanged and ungated, as before.

The trap, recorded because it is not obvious and is easy to reintroduce:
**Starlette's `@app.middleware("http")` does not run for WebSocket
connections.** The public-tunnel gate is exactly such a middleware. The terminal
WebSocket therefore cannot rely on it and authenticates itself in the route
handler — if that check is ever removed, `/admin` would look shut while the
shell stayed wide open. `SessionMiddleware` *does* cover the websocket scope,
which is why the signed session cookie is readable there.
`tests/test_remote_admin.py::test_terminal_websocket_refuses_without_an_admin_session`
pins this.

## Turning it off

```sh
systemctl --user revert birdbrain-web.service && systemctl --user restart birdbrain-web.service
birdbrain set-role caiusx --role tester
```

Removing the Cloudflare Access application does *not* re-close `/admin` — the
in-app role gate still lets an admin session through. Demote the account too.
