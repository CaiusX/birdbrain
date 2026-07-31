#!/usr/bin/env bash
# TinyBirdBrain self-update with rollback.
#
# Fast-forwards the unit's branch, reinstalls the tbb dependency profile,
# restarts the services, and verifies /healthz. The unit runs an UNMODIFIED
# checkout (deps live in deploy/tbb/requirements-tbb.txt + a self-managed venv,
# not a local pyproject edit), so:
#   * fast-forward only — never rewrites local history;
#   * on a post-update health failure it ROLLS BACK to the previous commit
#     (`git reset --hard`), reinstalls, and restarts — safe because nothing in
#     the working tree is locally modified.
#
# Run by tbb-update.timer, or manually: `systemctl --user start tbb-update`.
set -uo pipefail

REPO="${TBB_REPO_DIR:-$HOME/birdbrain}"
PORT="${TBB_WEB_PORT:-8080}"
UV="${UV:-$HOME/.local/bin/uv}"
cd "$REPO" || { echo "tbb-update: no repo at $REPO"; exit 1; }

REF="${BIRDBRAIN_TBB_UPDATE_REF:-origin/$(git rev-parse --abbrev-ref HEAD)}"

reinstall() {
  "$UV" pip install -q -r deploy/tbb/requirements-tbb.txt && "$UV" pip install -q --no-deps -e .
}
restart() {
  systemctl --user restart tbb-pipeline tbb-web
}
# Wait for the unit to be genuinely working, not merely answering.
#
# The old version returned success as soon as /healthz gave any 200. But that
# endpoint answers from the WEB process, while the thing that actually records
# birds is the capture worker in tbb-pipeline — and /healthz reports its state
# separately. A unit whose pipeline failed to start therefore sailed through the
# gate, was declared "healthy", and was never rolled back. Require the worker.
#
# The window was also 30s, which is shorter than a Pi Zero 2 W takes to load
# BirdNET (observed 20-60s, longer under swap). That is the more dangerous of
# the two bugs: timing out here does not just mis-report, it ROLLS BACK a
# perfectly good update. Default generously and let it exit early on success.
HEALTH_TIMEOUT="${TBB_HEALTH_TIMEOUT:-180}"

healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) body=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    sleep 3
    body="$(curl -fsS -m 5 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null)" || continue
    # Checked independently so JSON key order can never matter.
    case "$body" in *'"listening":true'*) ;; *) continue ;; esac
    case "$body" in *'"worker_state":"running"'*) return 0 ;; esac
  done
  echo "tbb-update: unhealthy after ${HEALTH_TIMEOUT}s; last /healthz: ${body:-<no response>}"
  return 1
}

before=$(git rev-parse HEAD)
if ! git fetch --quiet origin; then
  echo "tbb-update: fetch failed (offline?) — nothing to do"; exit 0
fi
target=$(git rev-parse "$REF" 2>/dev/null) || { echo "tbb-update: unknown ref $REF"; exit 1; }
if [ "$before" = "$target" ]; then
  echo "tbb-update: already current ($before)"; exit 0
fi

echo "tbb-update: $before -> $target ($REF)"
if ! git merge --ff-only "$REF"; then
  echo "tbb-update: cannot fast-forward (diverged) — skipping, unit untouched"; exit 1
fi

reinstall || echo "tbb-update: WARNING dependency install reported errors"
restart
if healthy; then
  echo "tbb-update: updated to $target and healthy"; exit 0
fi

echo "tbb-update: health check FAILED after update — rolling back to $before"
git reset --hard "$before"
reinstall || echo "tbb-update: WARNING reinstall during rollback reported errors"
restart
if healthy; then
  echo "tbb-update: rolled back to $before and healthy"
else
  echo "tbb-update: WARNING still unhealthy after rollback — power-cycle the unit"
fi
exit 1
