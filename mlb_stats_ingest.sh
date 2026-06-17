#!/bin/bash
# MLB Stats Ingestion Runner
# Loads teams, players, games, batting stats, and pitching stats from 2021-present
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="backend-api"

ACTION="${1:-start}"

case "$ACTION" in
  start)
    echo "Starting MLB stats ingestion (2021-present)..."
    docker run -d \
      --name mlb-stats-ingest \
      --restart on-failure \
      --network host \
      -v "$PROJECT_DIR/backend:/app" \
      "$IMAGE" \
      python3 /app/run_mlb_stats_ingest.py
    echo "Container mlb-stats-ingest started."
    echo "Watch: docker logs -f mlb-stats-ingest"
    ;;
  stop)
    echo "Stopping mlb-stats-ingest..."
    docker stop mlb-stats-ingest 2>/dev/null && docker rm mlb-stats-ingest 2>/dev/null
    echo "Stopped."
    ;;
  logs)
    exec docker logs -f mlb-stats-ingest
    ;;
  status)
    docker ps --filter name=mlb-stats-ingest --format "{{.Names}} {{.Status}}"
    ;;
  *)
    echo "Usage: $0 {start|stop|logs|status}"
    exit 1
    ;;
esac
