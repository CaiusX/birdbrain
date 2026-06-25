# TBB → birdbrain (pi) integration — review brief for the Pi 5

**To:** the agent/operator on the production **birdbrain.co.za** host (Pi 5),
which owns the `pi` branch and the live deploy.
**From:** the TBB build session (dev box + bench Zero 2 W).
**Ask:** review the proposed integration of TinyBirdBrain (TBB) onto `pi` and
decide the best way to land it on production. You know the live environment and
the `pi` branch's auth/scoring internals better than I do — especially confirm
the **public-gate merge** and the **privacy model** fit your account system.

---

## 1. What TBB is (1 paragraph)

TBB = cheap Raspberry Pi Zero 2 W "capture units" with a USB/Codec-Zero mic that
run the **same `africam` package** on-device (BirdNET via **tflite-runtime**,
not full TF — 190 MB vs 474 MB, fits 512 MB), show a minimal LAN web UI, and
**optionally sync detections up to central** where they appear as just another
source/site. Built across Phases 0–3 and accepted on a real Zero 2 W (detect →
local UI → reboot-persist → sync to a *local* central all proven on metal).

## 2. Branch state

- `origin/tbb` — all TBB work (≈ branched from `main`, +TBB commits).
- `origin/pi` — production (tester accounts + per-user scoring).
- `origin/tbb-pi-integration` — **a completed merge of `tbb` into `pi`** (this is
  what I'm asking you to review). `tbb` and `pi` diverged ~32/56 commits; 8 files
  conflicted, all resolved. **49 tests pass** on the merged branch; no leftover
  markers, everything parses, no merge-induced lint (the 3 pre-existing app.py
  F401s are unchanged from `pi`).

Review the diff:
```sh
git fetch origin
git diff origin/pi...origin/tbb-pi-integration          # full integration delta
git log --oneline origin/pi..origin/tbb-pi-integration  # commits being added
uv run pytest -q                                        # on a checkout of the branch
```

## 3. What the integration adds to central (the `pi`-side surface)

- **Tables:** `devices` (unit_id, sha256 token_hash, owner, lat/lon,
  last_seen_at, sync_enabled, public) and `claim_codes`; **column**
  `runtime_sources.external`. All via `Base.metadata.create_all` + the existing
  `_migrate_in_place` ADD COLUMN list — **additive, existing rows untouched.**
- **Routes:** `POST /ingest/detections` (per-unit **bearer token**, idempotent
  upsert on the natural key, rate-limited, body-capped) and `POST /enroll`
  (one-time **claim code** → issues unit_id + token). Both are the only mutating
  routes allowed over the public tunnel.
- **Public gate:** added `_PUBLIC_ALLOWED_PREFIXES = ("/ingest/", "/enroll")` so
  anonymous units reach those token/code-gated routes; **everything else public
  stays read-only and `/admin` still 404s.**
- **Privacy:** units whose `devices.public` is false are hidden from public
  (tunnel) views — feed, map, `/site/<unit>`; LAN/admin sees all.
- **Auto-register + liveness:** first ingest upserts the unit as an *external*
  `RuntimeSourceRow` (so map/site/species/briefs pick it up) and stamps a worker
  heartbeat; the pipeline supervisor **skips external sources** (never runs a
  local worker for a unit). A silent unit goes "stale" via the existing
  `_hb_status`.
- **CLI:** `tbb-claim-new` (mint claim code), `tbb-device-add` (mint token
  directly), `tbb-device-revoke`.
- **Packaging:** `requires-python` lowered to `>=3.11`; new unit-only
  `deploy/tbb/requirements-tbb.txt` (central deps − tensorflow + tflite-runtime).
  Central's `pyproject`/`uv.lock` stay **full-TF**; units install their own venv.

## 4. Conflict reconciliations I made — please sanity-check against `pi`

1. **Two ALSA sources kept.** `pi` has `AlsaSource` (kind `"device"`); TBB has
   `MicSource` (kind `"mic"`). I kept **both** (build_source handles both; both
   exported). They overlap conceptually — **you may prefer to unify** TBB onto
   your `AlsaSource` and drop `MicSource`. Flagged as a follow-up.
2. **Public gate (most important to review).** I combined your session-auth gate
   (login/signup over the tunnel; logged-in users may mutate) with TBB's
   anonymous `/ingest`+`/enroll` allowlist. Please confirm the merged
   `restrict_public` preserves your auth semantics exactly.
3. **Dashboard/feed** now applies **both** your replay filter and TBB's
   private-unit hiding.
4. **Pipeline** keeps both source kinds, combines your `effective_min` with
   TBB's `drop_non_bird`, and keeps your supervisor staggering + TBB's
   external-source skip.
5. **`itsdangerous`** added to the unit dep profile (a drift test caught that
   `pi` added it for `SessionMiddleware`).

## 5. Decisions for you to make

- **Privacy vs your accounts.** TBB hides non-public units from *anonymous*
  public traffic (via `cf-connecting-ip` + `devices.public`). Should unit
  visibility instead (or also) key off your **per-user accounts** (e.g. an owner
  sees their unit when logged in)? TBB units are **not** currently tied to a
  `UserRow`. (Per-owner login was scoped as TBB "Phase 4".)
- **Land strategy:** (a) fast-forward `pi` to `origin/tbb-pi-integration`;
  (b) cherry-pick only the TBB commits onto `pi` yourself; or (c) re-do the merge
  locally if you'd rather own the conflict calls. I recommend (a) after you've
  reviewed the gate merge.
- **Migration safety on the live DB:** the adds are non-destructive, but please
  **back up the production SQLite** before first restart on the new code.
- **`uv.lock`:** the merge auto-merged it — regenerate (`uv lock`) on your side
  and confirm it resolves on the Pi 5 (full TF, your Python).
- **Dual ALSA source:** unify now or leave for later?

## 6. Risks / must-not-break

- Public side stays **read-only** except the two token/code-gated routes;
  `/admin` and all other mutating verbs remain 404 to anonymous public.
- The pipeline supervisor must **never** start a local worker for a TBB unit
  (handled by the `external` flag + `_desired_sources` skip — verify).
- Central's full-TF pipeline is unchanged; only `requires-python` moved to
  `>=3.11` (your Pi 5 Python satisfies that).
- TBB unit code (`tbb.py`, `web/tbb_app.py`, `tbb_sync.py`) and
  `deploy/tbb/*` are unit-only and don't run on central.

## 7. After you land it — bring a unit onboard

```sh
# on central (Pi 5), once deployed + restarted:
africam tbb-claim-new --note "first unit"        # prints a claim code
# then on the unit's /setup "Connect to central" form: central URL =
# https://birdbrain.co.za, paste the code, name it, drop a lat/lon → Enroll.
# The unit appears as a normal source; mark devices.public=true to show it on
# the public map.
```

## 8. Verification already done

- 49 tests pass on `origin/tbb-pi-integration` (your tests + TBB's).
- Bench Zero 2 W: Phases 0–2 accepted on metal; full enroll→token→sync proven
  against a *local* central instance (not production).
- See `docs/tbb-build-plan.md` (decision records per phase) and
  `docs/tbb-provisioning.md` (unit build + Codec Zero notes).
