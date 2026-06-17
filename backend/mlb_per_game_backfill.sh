#!/bin/bash
# Run MLB per-game backfill inside the API container
# Starts from 2021 onward

set -e

CONTAINER="backend-api-1"
LOGFILE="/home/rich/.openclaw/workspace/earl-knows-football/backend/logs/mlb_per_game_backfill_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$(dirname "$LOGFILE")"

echo "Starting MLB per-game backfill (2021-2026) at $(date)" | tee -a $LOGFILE

docker exec $CONTAINER python -m app.ingestion.per_game_backfill \
    --sport mlb \
    --start 2021 \
    --end 2026 \
    2>&1 | tee -a $LOGFILE

echo "Backfill complete at $(date)" | tee -a $LOGFILE
