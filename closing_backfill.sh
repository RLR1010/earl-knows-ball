#!/bin/bash
# Standalone closing-lines backfill runner.
# Pulls historical closing odds from The Odds API and inserts into mlb.betting_lines.
# Runs as a standalone Docker container — not tied to the API container.
# Image is a slim python:3.12-slim build (~300MB), not the full 7GB API image.
#
# Usage:
#   ./closing_backfill.sh                # start full MLB 2021-2026 closing lines
#   ./closing_backfill.sh nfl            # start full NFL 2020-2026 closing lines
#   ./closing_backfill.sh nba            # start full NBA 2020-2026 closing lines
#   ./closing_backfill.sh status         # check if running
#   ./closing_backfill.sh logs           # see recent output
#   ./closing_backfill.sh stop           # stop gracefully

set -e

CONTAINER_NAME="earl-closing-backfill"
IMAGE="earl-mlb-backfill"

# Load .env for ODDS_API_KEY
if [ -f "$(dirname "$0")/backend/.env" ]; then
  set -a
  source "$(dirname "$0")/backend/.env"
  set +a
fi

SPORT="${1:-mlb}"

case "$SPORT" in
  start|mlb)
    SPORT="mlb"
    ARGS="--sport mlb --start 2021 --end 2026 --closing-only"
    ;;
  nfl)
    ARGS="--sport nfl --start 2020 --end 2026 --closing-only"
    ;;
  nba)
    ARGS="--sport nba --start 2020 --end 2026 --closing-only"
    ;;
  status|logs|stop)
    # pass through
    ;;
  *)
    echo "Usage: $0 {start|nfl|nba|status|logs|stop}"
    exit 1
    ;;
esac

case "${1:-start}" in
  start|mlb|nfl|nba)
    echo "Starting closing backfill container for $SPORT..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -e ODDS_API_KEY="$ODDS_API_KEY" \
      -e SYNC_DATABASE_URL="postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football" \
      "$IMAGE" \
      python -u -m app.ingestion.per_game_backfill $ARGS
    echo "Container $CONTAINER_NAME started (sport=$SPORT)."
    echo "Run './closing_backfill.sh logs' to see progress."
    echo "Run './closing_backfill.sh status' to check if alive."
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
    echo "Usage: $0 {start|nfl|nba|status|logs|stop}"
    exit 1
    ;;
esac
