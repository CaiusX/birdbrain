#!/usr/bin/env bash
# TinyBirdBrain forward-only self-update.
#
# Pulls the unit's current branch, syncs deps, restarts the services, and
# verifies /healthz. Designed to be safe on a remote unit:
#   * fast-forward only — if the pull can't ff (e.g. the unit's local
#     dependency edit conflicts with an incoming pyproject change), it aborts
#     and leaves the running unit untouched;
#   * NO hard rollback — `git reset --hard` would discard the unit's local
#     tflite/3.11 dependency edit, so on a post-update health failure we log
#     loudly and leave it for a power-cycle instead. (Auto-rollback arrives once
#     the tbb dependency profile is committed — see docs/tbb-build-plan.md.)
#
# Run by tbb-update.timer, or manually: `systemctl --user start tbb-update`.
set -uo pipefail

REPO="${TBB_REPO_DIR:-$HOME/birdbrain}"
PORT="${TBB_WEB_PORT:-8080}"
cd "$REPO" || { echo "tbb-update: no repo at $REPO"; exit 1; }

# Track whatever branch the unit is on (override with AFRICAM_TBB_UPDATE_REF).
REF="${AFRICAM_TBB_UPDATE_REF:-origin/$(git rev-parse --abbrev-ref HEAD)}"

before=$(git rev-parse HEAD)
if ! git fetch --quiet origin; then
  echo "tbb-update: fetch failed (offline?) — nothing to do"
  exit 0
fi
target=$(git rev-parse "$REF" 2>/dev/null) || { echo "tbb-update: unknown ref $REF"; exit 1; }
if [ "$before" = "$target" ]; then
  echo "tbb-update: already current ($before)"
  exit 0
fi

echo "tbb-update: $before -> $target ($REF)"
if ! git merge --ff-only "$REF"; then
  echo "tbb-update: cannot fast-forward (local changes / diverged) — skipping, unit untouched"
  exit 1
fi

if ! uv sync --quiet; then
  echo "tbb-update: WARNING uv sync failed; restarting on pulled code anyway"
fi
systemctl --user restart tbb-pipeline tbb-web

for _ in $(seq 1 10); do
  sleep 3
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    echo "tbb-update: updated to $target and healthy"
    exit 0
  fi
done
echo "tbb-update: WARNING health check failed after update to $target — power-cycle if it doesn't recover"
exit 1
