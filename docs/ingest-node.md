# Stream-ingest nodes

A second Pi that runs its own pipeline over cams central does not analyse, and
reports the detections upstream. Each cam arrives on central as its own site, at
its own coordinates, through the ingest endpoint that already exists for TBB.

## Why

Central's own dashboard puts a ceiling on it: `21 / ~30 cams max` at roughly
100 ms per 3-second chunk, sharing four cores with the web service, the SQLite
database and the notes worker. Africam alone streams ~50 live cams. Past the
ceiling the answer is another machine, not a lower threshold.

A node moves *inference* off central and leaves everything else where it is.
Audio never reaches central — a detection row is ~200 bytes, an audio stream is
not — so the link carries kilobytes a day per cam.

## How it differs from a TBB unit

Both push to `POST /ingest/detections` and central cannot tell them apart. The
difference is on the sending side:

| | TBB unit (Pi Zero 2 W) | Ingest node (Pi 5) |
|---|---|---|
| Captures | one USB mic | many YouTube/RTSP cams |
| Locations | one | one per cam |
| Devices on central | one | **one per cam** |
| Agent | `tbb_sync` | `node_sync` |
| Runs in | a thread in `tbb-web` | its own `birdbrain node-sync` service |
| Sends `audio_quality` | yes, it measures its own mic | no |

One device per cam is not a workaround. `ingest.ingest_batch` writes
`source_name=device.unit_id` at `device.lat`/`device.lon`, so **a token
authorises exactly one source at exactly one place** — which is precisely what
puts each cam on the map correctly. Pointing `tbb_sync` at a multi-cam node
instead files every cam under one unit at one coordinate.

## What central needs

Nothing. The wire format is unchanged (`wire.SCHEMA_VERSION` 1), so
auto-registration, the `external` flag that stops central's supervisor running
the cam locally, heartbeat liveness, species floors and suppressions all apply
as they already do.

Two things are worth knowing:

- A pushed source registers as `kind="mic"`, `url="tbb://<unit>"`
  (`db.register_tbb_source` hardcodes the TBB shape). Cosmetic, and the
  registration is create-only, so an admin edit sticks.
- Central applies its own species floors and suppressions to ingested rows, so a
  node's detections are held to the same standard as a locally-analysed cam.

## Setting one up

### 1. Rosters

A node keeps its cams in **`sources.node.toml`**, never `sources.toml` — the
latter is force-added to git so central can `git pull` its own roster, and
writing a node's cams there would push them onto central. Point the node at its
own file once, in `.env`:

    BIRDBRAIN_SOURCES_FILE=sources.node.toml

Nothing in the node's roster may duplicate a cam central runs itself. Central
skips `external` sources, but it will happily run a cam that is also in its own
`sources.toml`, and the two would race.

### 2. Enroll one device per cam, on central

    birdbrain tbb-device-add --unit-id "Nkorho Bush Lodge" \
        --lat -24.734 --lon 31.5974 --public

`--unit-id` becomes the source name shown on the dashboard, so give it the human
name you want to read there — unlike `/enroll`, which slugifies it. The token
prints once; central stores only a SHA-256 of it. Re-running rotates it.

### 3. `node.toml` on the node

Copy `node.example.toml`, set `central_url`, and add one `[[link]]` per cam
pairing the local `source` name with its `unit` and token. It is gitignored: it
holds bearer tokens.

On a LAN node prefer central's private address over the public hostname — same
endpoint, but batches stay off the tunnel and skip TLS on a link already
trusted.

### 4. YouTube cookies

Mandatory, not optional. `audio/youtube.py` pins `player_client=mweb`, and mweb
without cookies is bot-gated ("Sign in to confirm you're not a bot"). A node
needs its own logged-in Firefox profile and its own `refresh-cookies` timer.

Use a **different Google account from central's**. Re-exporting cookies makes
YouTube rotate the session, so two hosts sharing one account invalidate each
other's cookies and both end up bot-gated.

A node also needs a JS runtime on `PATH` (deno or node) for YouTube's n-sig
challenge — `_detect_js_runtime` finds it, and without one yt-dlp warns and some
formats go missing.

### 5. Services

    cp scripts/birdbrain-pipeline.service  ~/.config/systemd/user/
    cp scripts/birdbrain-node-sync.service ~/.config/systemd/user/
    # substitute <REPO> and <UV> in both
    systemctl --user daemon-reload
    systemctl --user enable --now birdbrain-pipeline birdbrain-node-sync

A node runs **no web service**. `birdbrain web` is central's; a node has no
dashboard to serve and no reason to open a port.

Smoke-test the link before enabling the timer:

    birdbrain node-sync --once

## Operating it

- **Backlog is never lost.** A per-source mark advances only on a 2xx, so an
  unreachable central means the marks stay put and drain on reconnect. Retries
  are idempotent: central upserts on `(source_name, started_at,
  scientific_name)` and every row carries a stable `client_id`.
- **A stalled cam is isolated.** Marks are per-source, so one cam bot-gated for
  an hour does not hold back the rest of the roster behind it.
- **A wedged worker shows as offline.** Keep-alives are suppressed while a cam's
  local worker heartbeat is stale, so central's ordinary stale-heartbeat logic
  can mark it down. Real backlog still flushes — data captured before a wedge is
  still good.
- **Replaying is safe but rate-limited.** Deleting the state file replays from
  zero and produces no duplicates. Note `write_json_atomic` keeps a `.bak` that
  `read_json_state` falls back to, so a true reset means deleting both.

## Sizing

An identical Pi 5 running only the pipeline — no dashboard, no database growth,
no notes worker — has more headroom than central's ~30. Watch `CPU load` and the
per-chunk inference estimate rather than a cam count, and remember every cam
also costs a yt-dlp resolve on restart: the supervisor staggers those, because
resolving a full roster at once raises the odds of tripping YouTube's IP block.
