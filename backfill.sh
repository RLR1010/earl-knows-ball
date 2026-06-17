#!/bin/bash
# Standalone SB Nation backfill runner.
# Runs as a standalone Docker container — NOT tied to the API container.
# Docker compose rebuilds won't kill this.
#
# Usage:
#   ./backfill.sh            # start backfill
#   ./backfill.sh status     # check if running
#   ./backfill.sh logs       # see recent output
#   ./backfill.sh stop       # stop gracefully

set -e

CONTAINER_NAME="earl-backfill"
IMAGE="earl-knows-football-api"

case "${1:-start}" in
  start)
    echo "Starting standalone backfill container..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -v "$(dirname "$0")/backend:/app" \
      "$IMAGE" \
      python3 /app/run_backfill.py
    echo "Container $CONTAINER_NAME started."
    echo "Run './backfill.sh logs' to see progress."
    echo "Run './backfill.sh status' to check if alive."
    ;;

  status)
    if docker ps --format '{{.Names}}' | grep -q "^$CONTAINER_NAME$"; then
      echo "Running"
      docker logs "$CONTAINER_NAME" --tail 3
    else
      echo "Not running"
    fi
    ;;

  logs)
    docker logs "$CONTAINER_NAME" --tail 30
    ;;

  stop)
    echo "Stopping $CONTAINER_NAME..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    echo "Stopped."
    ;;

  *)
    echo "Usage: $0 {start|status|logs|stop}"
    exit 1
    ;;
esac
