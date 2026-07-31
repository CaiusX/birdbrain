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
healthy() {
  for _ in $(seq 1 10); do
    sleep 3
    curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && return 0
  done
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
