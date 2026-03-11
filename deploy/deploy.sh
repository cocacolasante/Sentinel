#!/bin/bash
set -euo pipefail
IMAGE="${1:-ghcr.io/cocacolasante/sentinel:latest}"
SHA="${2:-}"
REPO_DIR="/root/sentinel"
COMPOSE_FILE="$REPO_DIR/docker-compose.yml"
SERVICES="brain celery-worker celery-worker-workspace celery-beat flower"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a /tmp/deploy.log; }

log "=== Deploy triggered: $IMAGE sha=${SHA:-latest} ==="

# ── 1. Sync codebase to the exact commit that was built ───────────────────────
log "Syncing repo at $REPO_DIR"
cd "$REPO_DIR"
git fetch origin main 2>&1 | tee -a /tmp/deploy.log
if [ -n "$SHA" ]; then
  git reset --hard "$SHA" 2>&1 | tee -a /tmp/deploy.log
  log "Repo pinned to sha=$SHA"
else
  git reset --hard origin/main 2>&1 | tee -a /tmp/deploy.log
  log "Repo reset to origin/main"
fi

# ── 2. Pull new image from GHCR ───────────────────────────────────────────────
PAT="${GHCR_PAT:-${GITHUB_TOKEN:-}}"
if [ -n "$PAT" ]; then
  echo "$PAT" | docker login ghcr.io -u "${GITHUB_USERNAME:-cocacolasante}" --password-stdin
else
  log "WARNING: no GHCR_PAT or GITHUB_TOKEN — pull may fail for private images"
fi
docker pull "$IMAGE"

# ── 3. Restart services with the new image ────────────────────────────────────
docker compose -p sentinel -f "$COMPOSE_FILE" up -d --no-deps --pull never $SERVICES
log "=== Deploy complete ==="
