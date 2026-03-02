#!/usr/bin/env bash
# =============================================================================
# AI Brain — Deploy / Restart
# Run from ~/ai-brain/ on the Ubuntu server.
#
# Usage:
#   ./scripts/deploy.sh          # build + start all services
#   ./scripts/deploy.sh restart  # restart without rebuild
#   ./scripts/deploy.sh logs     # tail all logs
#   ./scripts/deploy.sh status   # show container status
# =============================================================================

set -euo pipefail

COMMAND="${1:-up}"
COMPOSE="docker compose"

case "$COMMAND" in
  up|deploy)
    echo "Building and starting AI Brain..."
    $COMPOSE build --no-cache brain
    $COMPOSE up -d
    echo ""
    echo "All services started. Checking status..."
    sleep 3
    $COMPOSE ps
    echo ""
    echo "Test the brain:"
    echo "  curl http://localhost:8000/"
    echo "  curl -X POST http://localhost:8000/api/v1/chat \\"
    echo "    -H 'Content-Type: application/json' \\"
    echo "    -d '{\"message\": \"Hello Brain\", \"session_id\": \"test\"}'"
    ;;

  restart)
    echo "Restarting services..."
    $COMPOSE restart
    $COMPOSE ps
    ;;

  logs)
    $COMPOSE logs -f --tail=100
    ;;

  status)
    $COMPOSE ps
    ;;

  stop)
    echo "Stopping all services..."
    $COMPOSE down
    ;;

  *)
    echo "Usage: $0 [up|restart|logs|status|stop]"
    exit 1
    ;;
esac
