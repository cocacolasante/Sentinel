#!/bin/bash
set -euo pipefail
IMAGE="${1:-ghcr.io/cocacolasante/sentinel:latest}"
COMPOSE_FILE="/sentinel-project/docker-compose.yml"
SERVICES="brain celery-worker celery-beat flower"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a /tmp/deploy.log; }

log "=== Deploy triggered: $IMAGE ==="
docker pull "$IMAGE"    # package is public — no login needed
docker compose -f "$COMPOSE_FILE" up -d --no-deps --pull never $SERVICES
log "=== Deploy complete ==="
