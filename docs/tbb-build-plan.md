# TBB Build Plan — instructions for a Claude Code session

This is the working brief for building the TinyBirdBrain (TBB) capture unit.
Read it alongside the architecture: [`tbb-architecture.md`](tbb-architecture.md).

You are working **in the `africam` repo**. The TBB is the *same package* with a
new audio source, a lean config profile, a minimal web UI, and a sync agent —
plus a small ingest path on the central server. Do not fork the detector,
schema, or clip writer.

---

## Ground rules (read first)

1. **Branch.** Do all TBB work on a branch: `git checkout -b tbb`. Do not
   commit to `main`. Keep commits small and per-step.
2. **Do not reformat the tree.** The working copy currently shows whole-file
   CRLF churn (Windows checkout). **Only stage files you actually changed** —
   `git add <specific paths>`, never `git add -A`/`git add .`. If you see 60+
   "modified" files in `git status`, that is the line-ending noise; ignore it.
3. **Reuse, don't duplicate.** `MicSource` subclasses the existing
   `africam.audio.source.AudioSource`. The unit reuses `BirdNetDetector`,
   `clips.save_chunk`, and the `DetectionRow` schema verbatim.
4. **Never break central.** The existing YouTube/RTSP pipeline and web app must
   keep working unchanged. TBB behaviour is gated behind a config profile, not
   edits to the default path.
5. **Target hardware is a Raspberry Pi Zero 2 W** (64-bit, ARM, **512 MB RAM**,
   quad A53). Keep deps ARM-friendly and memory-light. The dev sandbox is x86
   and is *not* representative — RAM/timing claims must be validated on a real
   Zero 2 W (or at least a Pi).
6. **Tests & lint.** Run `uv run ruff check` and `uv run pytest` before each
   commit. Add tests for new logic (`MicSource` command construction, sync
   batching/idempotency, ingest auth).
7. **One phase at a time.** Finish a phase's acceptance criteria, commit, and
   stop for review before starting the next. Do not jump ahead.

---

## Phase 0 — bench prototype (validate on metal)

**Goal:** prove the existing pipeline runs on a Zero 2 W from a USB mic, within
RAM and real-time budget. Smallest possible change.

**Do:**
1. Add `MicSource(AudioSource)` in `src/africam/audio/source.py` (or a new
   `src/africam/audio/mic.py`). It only overrides `_ffmpeg_command()` to read
   ALSA: `ffmpeg -f alsa -i <device> -ac 1 -ar <rate> -f s16le -`. See the
   snippet in `tbb-architecture.md` §2.1.
2. Add a CLI smoke command, mirroring the existing one in `cli.py` (~line 249):
   `africam tbb-listen --device plughw:1,0 --seconds 60` that builds a
   `MicSource`, runs `BirdNetDetector.analyze` on each chunk, and prints
   detections + measured per-chunk inference ms.
3. On a real Zero 2 W: install, run the smoke command with a USB mic, and
   record (a) per-chunk inference ms, (b) peak RAM (`systemd-cgtop` / `free -m`
   / `/usr/bin/time -v`).

**Decision point — TensorFlow vs tflite-runtime:** the repo depends on full
`tensorflow` (no cp312 tflite wheels). On 512 MB this may be too heavy. If peak
RAM is uncomfortable (say >~350 MB for the pipeline) or import is very slow,
switch the unit to `tflite-runtime` — `birdnetlib` uses `tf.lite.Interpreter`
and falls back to `tflite_runtime.interpreter` when full TF isn't present.
Capture this as a `tbb`-profile dependency choice; do **not** change central's
deps. Record the outcome in this doc.

**Acceptance:**
- 3 s chunk analyses in well under 3 s on the Zero 2 W (target <1.5 s).
- Pipeline peak RAM leaves comfortable headroom on 512 MB with Lite OS.
- A clear TF-vs-tflite decision is recorded.

**Commit:** `tbb: MicSource + bench smoke command (Phase 0)`

### Phase 0 — decision record (2026-06-21)

**What was built:** `MicSource(AudioSource)` in `src/africam/audio/mic.py` (overrides
only `_ffmpeg_command()` to read ALSA), exported from `africam.audio`; the
`africam tbb-listen` smoke command in `cli.py`; unit tests in
`tests/test_mic_source.py`. `ruff` clean on the new files; the only new `cli.py`
lint hits are the same deferred-import (`PLC0415`) idiom the existing `probe`
command already uses to keep TensorFlow out of CLI startup. `pytest`: 3 passed.

**⚠️ Metal numbers still owed.** This was developed on the x86 Windows dev
sandbox, which has no ALSA and is explicitly *not* representative (ground rule
5). The acceptance numbers below are an **x86 reference only** — the real
Zero 2 W run (`africam tbb-listen --device plughw:1,0 --seconds 60` under
`/usr/bin/time -v`) is still required to close the gate.

| Metric (x86 ref, full TensorFlow) | Value |
| --- | --- |
| Python baseline RSS | ~31 MB |
| After `import tensorflow`/birdnetlib | ~302 MB |
| After BirdNET model load | ~449 MB (peak 474 MB) |
| Per-chunk inference (warm, 3 s chunk) | min 47 / avg 55 / max 67 ms |
| Process peak RSS | **~474 MB** |

**TensorFlow vs tflite-runtime decision → use `tflite-runtime` on the unit.**
The RAM evidence is decisive even allowing for arch differences: full TF's
~450–474 MB working set already exceeds the architecture's ~350 MB switch
threshold *before* adding ffmpeg/`MicSource`, the web app, and SQLite — on a
512 MB Zero 2 W (minus ~60–100 MB for Lite OS) that is an OOM, not "tight". The
import accounts for ~270 MB of it; `tflite-runtime` ships only the interpreter,
and `birdnetlib` already falls back to `tflite_runtime.interpreter` when full TF
is absent, so this is a `tbb`-profile **dependency swap with no code change**.
Central's deps stay on full TensorFlow — unchanged.

**Metal results — Pi Zero 2 W, Raspberry Pi OS Lite 64-bit (Bookworm),
2026-06-22.** Measured on the shipping (tflite) path: `tflite-runtime` 2.14.0
installed cleanly for **cp311 aarch64**, `tensorflow` absent, birdnetlib used the
tflite interpreter (XNNPACK delegate). USB mic = Logitech headset on `plughw:0,0`.

| Metric (Zero 2 W, tflite-runtime, Py 3.11) | Value |
| --- | --- |
| Detector load | 1.5 s |
| Per-chunk inference, **warm steady state** | **~1.05 s (x2.9 real-time)** |
| Per-chunk inference, avg over 16 chunks | 1.40 s (incl. cold start + SD-I/O spikes) |
| Per-chunk inference, max | 4.20 s (cold first chunk, one-off) |
| **Peak RSS** | **190 MB** (`time -v`: 194 MB) — **0 swap** |

Both caveats from the x86 estimate are now resolved: (a) **wheels** — cp311
aarch64 `tflite-runtime` exists, cp312 does **not**, so the `tbb` profile pins
**Python 3.11** (no `ai-edge-litert` shim needed); (b) **timing** — warm inference
~1.05 s/chunk, comfortably inside the 3 s budget. The 4.2 s cold-start chunk and
a couple of 1.6–2.8 s spikes track SD-card I/O (23.9k major page faults on the
first run, cold caches; job was I/O-bound at ~50% CPU), not sustained compute —
worth a warm re-run and a multi-hour soak in Phase 1 to confirm steady-state and
watch thermals.

**Verdict — Phase 0 acceptance met.** tflite-runtime on Python 3.11 gives 190 MB
peak (vs full TF's ~474 MB on x86, which won't fit 512 MB) and ~2.9× real-time
headroom warm. The `tbb` profile must ship **Python 3.11 + tflite-runtime with
`tensorflow` dropped**; central stays on full TF, unchanged. The bench used a
throwaway `pyproject.toml` edit (requires-python `>=3.11`, TF→tflite-runtime) plus
`uv python pin 3.11` — Phase 1 must express this as a proper `tbb` dependency
profile, not an edit to the shared deps.

---

## Phase 1 — standalone unit (offline-useful puck)

**Goal:** a unit that boots, detects from the mic, stores locally, and serves a
minimal LAN UI — with no internet.

**Do:**
1. **Config profile.** Add a `tbb` profile to `africam.config` (env/`tbb.toml`)
   that: selects `MicSource`, sets the single source name to the unit id,
   disables central-only workers (notes, weather, media sweeper, anomalies,
   OCR site-resolver, yt-dlp/cookies), sets local clip retention (prune after N
   days), and carries sync settings (default: sync **off** in Phase 1).
2. **Pipeline service.** A `tbb-pipeline` path that runs the mic → detector →
   SQLite + clips loop using the existing pipeline, single source.
3. **Minimal web app.** A cut-down FastAPI app (reuse `africam.web` building
   blocks) serving exactly two LAN-only pages — **Now** (live feed + spectrogram
   thumbnails, header with unit name / today's species count / mic-level / sync
   dot) and **Today** (species list with counts). Plus a LAN-only `/setup`
   (mic device picker from detected ALSA devices, unit name, sync toggle). Bind
   to LAN; no inbound exposure. See `tbb-architecture.md` §5.
4. **Services + image.** systemd **user** units `tbb-pipeline` and `tbb-web`
   under `scripts/` (mirror the existing `africam-*` unit files). Document the
   Pi Zero 2 W golden-image build (64-bit Lite, ffmpeg, uv, the package) in
   `docs/tbb-provisioning.md`.

**Acceptance:**
- Fresh Zero 2 W with mic, no internet: detections appear on `Now`, persist
  across reboot, clips saved and pruned per retention.
- RAM stable over a multi-hour run; UI responsive on a phone over LAN.
- Central pipeline/app untouched and still green (`pytest`, manual run).

**Commit(s):** `tbb: config profile + pipeline service (Phase 1a)`,
`tbb: minimal local web UI + setup page (Phase 1b)`,
`tbb: systemd units + provisioning notes (Phase 1c)`

---

## Phase 2 — sync (unit → central)

**Goal:** a unit's detections appear on birdbrain.co.za as a normal site.

**Do (unit side):**
1. **Sync agent.** Batches new `DetectionRow`s since `last_synced_id`, POSTs to
   central with `Authorization: Bearer <device_token>`, advances the high-water
   mark on ack. Offline-buffers (never drops local rows), drains backlog on
   reconnect in capped batches, idempotent via a per-row `client_id` +
   `(source_name, started_at, scientific_name)` key. Default cadence ~30–60 s.
   Clips: metadata-only by default (`has_clip` flag, no audio body yet).
   Placement: background task in `tbb-web` for now (revisit in §10 of arch).

**Do (central side):**
2. **Device registry.** `devices` table: `unit_id`, hashed `device_token`,
   owner, display name, lat/lon, `created_at`, `last_seen_at`, `sync_enabled`,
   `public` (default false).
3. **Ingest endpoint.** `POST /ingest/detections` — authenticate token → map to
   the unit's `source_name` → upsert `DetectionRow`s → stamp `last_seen_at`.
   This route is the **one mutating endpoint allowed over the public tunnel**,
   so it must **opt out of the `CF-Connecting-IP` admin gate** in
   `web/app.py` but enforce token auth instead. Add strict rate limits and a
   body-size cap. Keep the scope tiny (detection upserts only).
4. **Auto-register site.** On first ingest, upsert the unit as a source
   (`RuntimeSourceRow` / source-state) with its lat/lon so the existing map,
   site page, species pages, and briefs pick it up with no UI changes.
5. **Liveness.** Reuse `WorkerHeartbeatRow`/`WorkerDowntimeRow` semantics off
   `last_seen_at` so a silent unit shows offline like a stalled YouTube source.

**Acceptance:**
- Unit detections show up on central within a minute; idempotent under retries
  (no dupes); survive a unit reboot and a central restart.
- Unit kept offline for an hour then reconnected drains its backlog with no
  loss and no duplication.
- Public tunnel remains read-only for everything except the token-authed
  ingest route; `/admin` still 404s publicly.

**Commit(s):** `tbb: sync agent with offline buffer (Phase 2a)`,
`central: device registry + token-authed /ingest (Phase 2b)`,
`central: auto-register TBB units as sites + liveness (Phase 2c)`

### Phase 2 — decision record (2026-06-22)

Implemented and unit-tested in the sandbox (34 tests green; central app imports
and its lint counts unchanged from baseline). **Metal acceptance VERIFIED
(2026-06-22):** the bench unit synced real detections (Hadada Ibis, Black-
crowned Night Heron, …) Pi → a local central instance within a minute; the unit
auto-registered as a `tbb-bench` site with a fresh `last_seen`, and resends were
idempotent (no dupes). The mic-test on `/setup` (record-and-play, 409 when the
pipeline holds the device) was also confirmed on metal. Production `birdbrain.
co.za` ingest is untested — same flow, deploy + `tbb-device-add` + point the
unit at the prod URL.

- **Idempotency:** central upserts on the natural key `(source_name,
  started_at, scientific_name)` — no `client_id` *column* was added to central.
  The unit still sends a stable `client_id` per row (logging / forward-compat),
  but the natural key is what dedups. Verified: resending a batch reports
  `accepted=0, duplicate=N`.
- **Auto-register needed a new flag.** A unit is registered as a
  `RuntimeSourceRow` with **`external=True`** (new column, ADD COLUMN migration).
  Without it the pipeline supervisor would try to run a *local* mic worker for
  the unit and thrash; `_desired_sources` now skips external rows. This is a
  deviation from the bare "upsert a RuntimeSourceRow" in §7.3, forced by the
  supervisor's spawn behaviour.
- **Liveness via heartbeat freshness, not downtime accounting.** Each ingest
  stamps `worker_heartbeat(unit_id)`; the dashboard's existing `_hb_status`
  renders a `running` heartbeat older than 60 s as `stale`, so a silent unit
  shows offline with no new code. `WorkerDowntimeRow` intervals are
  backoff-driven (local workers only), so per-unit `down_*_s` stats stay 0 — the
  *status* is correct, the cumulative downtime figure isn't tracked for units.
- **§10 decisions taken:** sync agent placement = in-process background thread
  in `tbb-web` (§10.1, "for now"); clip policy = **metadata-only** (`has_clip`
  flag, no audio body — §10.2 lazy/clip-sync deferred to Phase 4). Tenancy
  (§10.3) untouched: `devices.public` defaults false; no per-owner gating yet.
- **Token lifecycle:** stored as SHA-256 hash; `africam tbb-device-add` mints a
  token on central (shown once) until the Phase 3 `/enroll` flow exists.
- **Public gate:** the ingest route opts out of the `CF-Connecting-IP` block via
  a narrow `/ingest/` allowlist but enforces bearer auth + rate limit + body
  cap. Verified: a public `POST /admin/*` still 404s.

---

## Phase 3 — fleet & enrollment

**Goal:** repeatable bring-up for a unit given/sold to a party.

**Do:**
1. **Enrollment.** `POST /enroll` on central: unit sends a one-time claim code,
   central returns `unit_id` + device token; owner names it and sets lat/lon.
   `/setup` on the unit drives this.
2. **First-boot wifi.** Bake 2.4 GHz creds at flash time, or ship a first-boot
   captive AP. (Zero 2 W is 2.4 GHz only and won't reliably take creds edited
   post-flash — known gotcha.)
3. **Per-unit token lifecycle.** Issue/rotate/revoke on central; store hashed.
4. **Tenancy/privacy flags.** Honour `devices.public`; decide owner-visibility
   model (see arch §10) and gate central views accordingly.
5. **Updates.** Pick and implement one: image re-flash, `apt`
   unattended-upgrades + pinned version, or pull-based self-update.

**Acceptance:** a blank SD card → flashed → booted → claimed → live on central,
following only `docs/tbb-provisioning.md`, with no manual SSH surgery.

**Commit(s):** per item, prefixed `tbb:` / `central:`.

---

## What to hand back after each phase

- A one-paragraph summary of what changed and the acceptance results
  (especially the Phase 0 measured numbers and the TF/tflite decision).
- Any deviations from `tbb-architecture.md`, and any of its §10 open decisions
  that the implementation forced a choice on.
- Updated docs (`tbb-provisioning.md`, and this file's Phase 0 decision record).

Start at **Phase 0**. Stop at each "Acceptance" gate for review.
