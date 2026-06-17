#!/bin/bash
# Launch SB Nation scrape for all three sports (NFL, NBA, MLB) from 2021-present
# Keeps container running with restart always

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="backend-api"

ACTION="${1:-start}"

case "$ACTION" in
  start)
    echo "Starting SB Nation multi-sport scraper (2021-present)..."
    docker run -d \
      --name sbnation-all \
      --restart always \
      --network host \
      -v "$PROJECT_DIR/backend:/app" \
      "$IMAGE" \
      python3 /app/run_sbnation_all.py
    echo "Container sbnation-all started."
    echo "Watch logs:  docker logs -f sbnation-all"
    ;;
  stop)
    echo "Stopping sbnation-all..."
    docker stop sbnation-all 2>/dev/null && docker rm sbnation-all 2>/dev/null
    echo "Stopped."
    ;;
  logs)
    exec docker logs -f sbnation-all
    ;;
  status)
    docker ps --filter name=sbnation-all --format "{{.Names}} {{.Status}}"
    ;;
  *)
    echo "Usage: $0 {start|stop|logs|status}"
    exit 1
    ;;
esac
