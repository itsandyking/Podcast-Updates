#!/usr/bin/env bash
# scripts/sync.sh — poll for upstream changes and auto-update
#
# Run on a schedule (cron / launchd) on each machine.  If the remote has new
# commits it pulls and re-runs setup.sh so schedules, deps, and config stay
# in sync automatically.
#
# Nothing happens if the repo is already up to date, so running every 5 min
# is cheap.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOGFILE="$REPO_DIR/data/logs/sync.log"
BRANCH="main"

mkdir -p "$(dirname "$LOGFILE")"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOGFILE"; }

cd "$REPO_DIR"

# Fetch quietly — this is the only network call when nothing changed
git fetch origin "$BRANCH" --quiet 2>&1 | tee -a "$LOGFILE"

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"

if [ "$LOCAL" = "$REMOTE" ]; then
    # Nothing to do — exit silently (don't spam the log)
    exit 0
fi

COMMITS="$(git log --oneline "${LOCAL}..${REMOTE}" 2>/dev/null | wc -l | tr -d ' ')"
log "=== $COMMITS new commit(s) — pulling and re-running setup ==="
git log --oneline "${LOCAL}..${REMOTE}" | tee -a "$LOGFILE"

git pull --ff-only origin "$BRANCH" 2>&1 | tee -a "$LOGFILE"

log "Running setup.sh..."
AUTO_YES=1 "$REPO_DIR/setup.sh" 2>&1 | tee -a "$LOGFILE"

log "=== Sync complete ==="
