# Installing BirdBrain

Self-host BirdBrain on a **Raspberry Pi 5** or a **desktop Linux / macOS** machine.
This guide goes from a clean checkout to a running dashboard, then on to an
always-on systemd deployment.

> 🐣 **Not very technical?** Try the gentle, plain-language walkthrough first:
> [`GETTING-STARTED.md`](GETTING-STARTED.md). This page is the detailed reference.

What you're running:

- **`birdbrain run`** — the detection pipeline: one worker per source, pulling audio
  (yt-dlp → ffmpeg) and classifying rolling 3-second chunks with BirdNET.
- **`birdbrain web`** — the FastAPI + Jinja + HTMX dashboard.

Both processes share one local **SQLite** database (`data/birdbrain.sqlite`, created
automatically) and a clips directory (`data/clips`). No cloud service is required for
the core loop — Anthropic / Xeno-Canto keys are optional extras (see below).

---

## 1. Prerequisites

**Runtime**

- **Python ≥ 3.12**
- **[uv](https://docs.astral.sh/uv/)** — the package/venv manager this project uses.

**System tools**

| Tool | Needed for | Required? |
|------|-----------|-----------|
| **ffmpeg** | Decoding/transcoding every audio stream | **Yes — always** |
| **deno** *or* **node** | yt-dlp's YouTube challenge solving | Yes, if you use any `youtube` source |
| **tesseract** | OCR auto-detection of the active camera on rotating multi-site streams | Optional (only `[source.ocr] enabled = true`) |
| **ALSA** / **PipeWire** / **PulseAudio** | Capturing a local microphone (`kind = "device"`) | Optional (only for a local mic) |
| **vcgencmd** | Pi SoC temperature / throttling on the admin health panel | Optional, Raspberry-Pi-only (degrades gracefully) |

> **Heads-up on weight:** the install pulls **full TensorFlow** (BirdNET's backend).
> The first `uv sync` downloads/builds a few hundred MB and is noticeably slow on a
> Raspberry Pi — let it run. Budget a few GB of disk for the venv + model + clips.

### Debian / Raspberry Pi OS (Bookworm)

```bash
sudo apt update
sudo apt install -y ffmpeg git
# JavaScript runtime for yt-dlp (pick one):
sudo apt install -y nodejs            # or install deno: https://docs.deno.com/runtime/
# Optional extras:
sudo apt install -y tesseract-ocr     # only if you enable OCR multi-site
sudo apt install -y pipewire          # only if capturing a local mic (usually already present)
```

### macOS (Homebrew)

```bash
brew install ffmpeg git node          # node (or: brew install deno)
brew install tesseract                # optional
```

### Windows 10 / 11 (winget, in PowerShell)

```powershell
winget install Git.Git
winget install Gyan.FFmpeg
winget install OpenJS.NodeJS.LTS
winget install UB-Mannheim.TesseractOCR   # optional, OCR multi-site only
```

Open a **new** PowerShell window afterwards so the tools land on `PATH`. `uv` will
fetch a matching Python itself, so you don't need to install Python separately.

> **Windows caveats:** the always-on **systemd** steps below are Linux-only (see
> [Windows always-on](#windows-always-on) instead), and **microphone capture**
> (`kind = "device"`) is Linux-only — on Windows use `youtube` or `rtsp` sources.
> Everything else works the same; run the `git` / `uv run …` commands in PowerShell
> (it aliases `cp`/`cd`, so the commands below work as written).

### Install uv

**Linux / macOS:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# then restart your shell, or: source ~/.local/bin/env
```

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

---

## 2. Install

```bash
git clone https://github.com/CaiusX/birdbrain.git
cd birdbrain
uv sync          # creates .venv and installs locked dependencies (slow first run)
```

BirdNET's model is fetched automatically by `birdnetlib` on first use — no manual
download.

---

## 3. Configure your sources

Copy the example and edit it:

```bash
cp sources.example.toml sources.toml
```

Each `[[source]]` is one continuously-monitored stream. Three kinds:

```toml
# A YouTube live stream (needs ffmpeg + a JS runtime)
[[source]]
name = "birdbrain-olifants"
kind = "youtube"
url = "https://www.youtube.com/@birdbrain/live"
lat = -24.0            # biases BirdNET's species filter to the region
lon = 31.5
min_confidence = 0.6   # hide detections below this score

# An RTSP camera/mic on your network
[[source]]
name = "garden-rtsp"
kind = "rtsp"
url = "rtsp://user:pass@192.168.1.42:554/stream1"
lat = -33.92
lon = 18.42
min_confidence = 0.7

# A local microphone (ALSA or PipeWire/Pulse). Find your device with `arecord -L`.
[[source]]
name = "garden-mic"
kind = "device"
url = "plughw:CARD=Device,DEV=0"     # or "pulse:alsa_input.usb-...-mono-fallback"
lat = -26.12
lon = 28.03
min_confidence = 0.5
```

- **`sites.toml`** is only needed for **rotating multi-site** streams (e.g. Wild Africa
  Live). Copy `sites.example.toml` → `sites.toml` and set `multisite = true` on the
  source. Otherwise ignore it.
- **YouTube bot-gating:** if yt-dlp hits *"Sign in to confirm you're not a bot"*, supply
  cookies — either export a `cookies.txt` and point at it, or run
  `uv run birdbrain refresh-cookies` (re-exports from a local Firefox profile).

### Optional `.env` (extra features)

Create a `.env` in the repo root for optional keys (prefix `BIRDBRAIN_`):

```ini
# Inline Xeno-Canto reference recordings in the audition modal (free key):
BIRDBRAIN_XENO_CANTO_KEY=your-key-here
# AI commentary (daily brief, per-species & per-site notes). The pipeline does
# NOT need this; only the note-writer (runs in the web process) uses it:
ANTHROPIC_API_KEY=sk-ant-...
```

Everything works without these — Xeno-Canto falls back to a search link, and the AI
note-writer simply stays dormant.

---

## 4. Smoke test

Confirm ffmpeg + BirdNET work against your config before going live:

```bash
uv run birdbrain probe -s sources.toml
```

This pulls a few seconds from each source and runs one BirdNET pass. If you see
detections (or a clean "no detections" with no errors), you're good.

---

## 5. Run it

In two terminals (same directory):

```bash
# Terminal 1 — detection pipeline
uv run birdbrain run

# Terminal 2 — dashboard
uv run birdbrain web --host 0.0.0.0 --port 8765
```

Open **`http://<host-ip>:8765/`**.

> **Ports:** `birdbrain web` defaults to `127.0.0.1:8000` (localhost only). Pass
> `--host 0.0.0.0 --port 8765` to reach it from other machines on your LAN — `8765`
> is just this project's convention; any free port works.
>
> **Admin:** the read-only gate keys off a Cloudflare header, so over LAN/localhost
> you have **full access** to `/admin` and all controls. The lock-down only applies
> when fronted by a Cloudflare Tunnel (step 7).

Useful commands:

```bash
uv run birdbrain detections -n 25      # recent detections
uv run birdbrain summary -H 24         # per-source rollup, last 24h
uv run birdbrain --help                # all subcommands
```

---

## 6. Run it always-on (systemd, Linux/Pi)

Run the two processes as **user** services so they start on boot and restart on
failure. Example unit templates live in [`scripts/`](scripts/).

```bash
# Let your user's services run without being logged in:
loginctl enable-linger "$USER"

mkdir -p ~/.config/systemd/user
cp scripts/birdbrain-pipeline.service ~/.config/systemd/user/
cp scripts/birdbrain-web.service      ~/.config/systemd/user/
# Edit both: set WorkingDirectory to your checkout and the full path to `uv`
# (run `command -v uv` to find it, e.g. ~/.local/bin/uv):
${EDITOR:-nano} ~/.config/systemd/user/birdbrain-pipeline.service
${EDITOR:-nano} ~/.config/systemd/user/birdbrain-web.service

systemctl --user daemon-reload
systemctl --user enable --now birdbrain-pipeline birdbrain-web
systemctl --user status birdbrain-web
```

Logs and control:

```bash
journalctl --user -u birdbrain-web -f
journalctl --user -u birdbrain-pipeline -f
systemctl --user restart birdbrain-web        # safe anytime
```

> Restarting **`birdbrain-pipeline`** re-resolves *all* sources at once, which can trip
> YouTube's IP bot-block. Prefer toggling a single source from `/admin`; only restart
> the whole pipeline when you must.

### Optional production add-ons

- **Cookies auto-refresh** — ship the included timer so bot-gated YouTube sources
  recover on their own:
  ```bash
  cp scripts/birdbrain-cookies-refresh.{service,timer} ~/.config/systemd/user/
  systemctl --user daemon-reload && systemctl --user enable --now birdbrain-cookies-refresh.timer
  ```
- **Disk hygiene** — clips accumulate; prune old ones (DB rows are kept, audio is
  removed): `uv run birdbrain prune --days 14`. Put it on a daily timer if you like.
- **AI commentary** — set `ANTHROPIC_API_KEY` for the web service via an
  `EnvironmentFile` (e.g. `/etc/birdbrain/secrets.env`), referenced from
  `birdbrain-web.service`.
- **Public access** — front the dashboard with a **Cloudflare Tunnel** (`cloudflared`).
  Over the tunnel, `/admin` and all mutating requests return `404`, so the public side
  is read-only without app-level auth. LAN access stays full.

### Windows always-on

Windows has no systemd. To keep BirdBrain running:

- **Simplest:** leave the two `uv run …` commands (step 5) running in two PowerShell
  windows.
- **On startup / unattended:** wrap each command as a background service with
  [NSSM](https://nssm.cc/), or create two **Task Scheduler** tasks set to *"Run whether
  user is logged on or not"* triggered *At log on*. Point one at `uv run birdbrain run`
  and the other at `uv run birdbrain web --host 0.0.0.0 --port 8765`, each with the repo
  folder as its working directory.

---

## 7. Troubleshooting

- **`uv sync` slow / fails on a Pi** — it's TensorFlow. Be patient; ensure you're on
  64-bit Python 3.12 and have free disk. A swap file helps on low-RAM Pis.
- **`ffmpeg: not found`** — install it (step 1); it's mandatory for every source.
- **YouTube: "Sign in to confirm you're not a bot"** — supply cookies / run
  `birdbrain refresh-cookies`; make sure `deno` or `node` is on `PATH`.
- **Port already in use** — pick another `--port`, or stop whatever owns it.
- **Mic not captured** — find the device string with `arecord -L`; use
  `plughw:CARD=…,DEV=0` for exclusive ALSA, or `pulse:alsa_input.…` for shared
  PipeWire/Pulse (the latter also needs `XDG_RUNTIME_DIR` set in the service env).
- **No AI notes** — expected unless `ANTHROPIC_API_KEY` is set in the **web** process.

---

## License & fair use

The code is **MIT** ([`LICENSE`](LICENSE)). The **BirdNET-Analyzer models** loaded at
runtime are **CC BY-NC-SA 4.0 (non-commercial, share-alike)** — that binds any
deployment that actually runs detection (educational and research use is considered
non-commercial). If you point this at third-party video streams, respect their terms;
this project analyses only the audio and does not redistribute the video. See the
[`README`](README.md) and the in-app `/about` page for full credits.
