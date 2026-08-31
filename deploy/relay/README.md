# Local RTSP relay

Some cameras serve **one client at a time**. Whoever connects first locks
everyone else out — and since the detection worker connects and stays
connected, that is always the detector. `/admin`'s live audition could never
get in; it saw `400 Bad Request` every time.

This relay holds the single upstream connection itself and re-serves the stream
on loopback, so the detector and the audition endpoint both read a local copy.
Djuma (added 2026-08-30) is the source this exists for.

```
  camera ──one connection──▶ birdbrain-relay ──▶ detection worker
   (single-client)            127.0.0.1:8554  └─▶ /admin audition
```

## What it costs

The relay becomes a **single point of failure** for anything routed through it:
if it stops, that source stops. Mitigated by `Restart=always` (verified: a
SIGKILL is recovered in ~12s, the relay reclaims the upstream and the worker's
own backoff reconnects it), but worth knowing before routing more sources here.

Sources that are *not* single-client should keep talking to their camera
directly — this is a workaround, not an improvement.

## Install

`mediamtx` is a single static binary and is deliberately **not** vendored into
this repo (~27 MB, and it wants its own update cadence). Fetch it and check the
signature — do not skip the checksum:

```sh
V=v1.20.1
cd "$(mktemp -d)"
curl -fsSLO "https://github.com/bluenviron/mediamtx/releases/download/$V/mediamtx_${V}_linux_arm64.tar.gz"
curl -fsSLO "https://github.com/bluenviron/mediamtx/releases/download/$V/checksums.sha256"
grep linux_arm64.tar.gz checksums.sha256 | sha256sum -c -   # must print OK
tar -xzf mediamtx_${V}_linux_arm64.tar.gz
install -m 0755 mediamtx ~/.local/bin/mediamtx
```

Then the service:

```sh
cp deploy/relay/birdbrain-relay.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now birdbrain-relay
loginctl enable-linger "$USER"      # so it survives logout
```

## Adding another single-client source

1. Add a path under `paths:` in `mediamtx.yml` pointing at the camera.
2. Point the source's `url` in `sources.toml` at `rtsp://127.0.0.1:8554/<path>`.
3. **Stop the pipeline first**, then restart the relay, then start the pipeline.
   The upstream can only be held by one process, so if the worker wins the race
   the relay sits retrying and the worker never reaches it. The worker's backoff
   does recover on its own, but it takes a minute or two of dead air.

## Checking it

```sh
systemctl --user status birdbrain-relay
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/djuma      # should show the audio track
ss -tnp | grep 8554                                          # readers + one upstream
```

Two local readers and exactly one connection to the camera is the healthy shape.

## Config notes

`mediamtx.yml` is deliberately minimal. Two settings are load-bearing:

- `rtspAddress: 127.0.0.1:8554` — loopback only. This re-serves someone else's
  stream; it is not ours to rebroadcast to the LAN.
- `moq: false` — MoQ defaults **on** and binds `:8892`/`:8893` on *all*
  interfaces. Every other protocol is off for the same reason.
