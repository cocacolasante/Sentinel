#!/bin/bash
set -euo pipefail
IMAGE="${1:-ghcr.io/cocacolasante/sentinel:latest}"
COMPOSE_FILE="/sentinel-project/docker-compose.yml"
SERVICES="brain celery-worker celery-beat flower"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a /tmp/deploy.log; }

log "=== Deploy triggered: $IMAGE ==="
PAT="${GHCR_PAT:-${GITHUB_TOKEN:-}}"
if [ -n "$PAT" ]; then
  echo "$PAT" | docker login ghcr.io -u "${GITHUB_USERNAME:-cocacolasante}" --password-stdin
else
  log "WARNING: no GHCR_PAT or GITHUB_TOKEN — pull may fail for private images"
fi
docker pull "$IMAGE"
docker compose -p sentinel -f "$COMPOSE_FILE" up -d --no-deps --pull never $SERVICES
log "=== Deploy complete ==="
