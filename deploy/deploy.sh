#!/bin/bash
set -euo pipefail
IMAGE="${1:-ghcr.io/cocacolasante/sentinel:latest}"
COMPOSE_FILE="/sentinel-project/docker-compose.yml"
SERVICES="brain celery-worker celery-beat flower"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a /tmp/deploy.log; }

log "=== Deploy triggered: $IMAGE ==="
if [ -n "${GHCR_PAT:-}" ]; then
  echo "$GHCR_PAT" | docker login ghcr.io -u "${GITHUB_USERNAME:-cocacolasante}" --password-stdin
fi
docker pull "$IMAGE"
docker compose -p sentinel -f "$COMPOSE_FILE" up -d --no-deps --pull never $SERVICES
log "=== Deploy complete ==="
