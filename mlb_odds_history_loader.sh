#!/usr/bin/env bash
# Runner script for MLB historical odds API ingestion
# Grabs lines/odds for every MLB game from 2021 through today
# Uses The Odds API paid tier (odds-history endpoint)
# Usage: ./mlb_odds_history_loader.sh {start|status|logs|stop}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

CONTAINER_NAME="earl-mlb-odds-history"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

build_loader_image() {
    local dockerfile_content='FROM python:3.12-slim
WORKDIR /app
# Only install essential packages (no torch/xgboost/etc)
COPY backend/requirements.txt .
RUN grep -v "sentence-transformers\|xgboost\|scikit-learn\|torch\|pyarrow\|feedparser" requirements.txt > /tmp/requirements-minimal.txt \
    && echo "httpx" >> /tmp/requirements-minimal.txt \
    && pip install --no-cache-dir -r /tmp/requirements-minimal.txt
COPY backend/ .
ENV PYTHONPATH=/app'

    echo "$dockerfile_content" | docker build -t earl-mlb-odds-history -f- .
}

case "${1:-start}" in
    start)
        echo "Building MLB odds history loader image..."
        build_loader_image

        echo "Starting MLB odds history ingestion..."
        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

        # Run curl against local API for odds-api-history endpoint
        # Starts from 2021-01-01 to today - the endpoint handles game-by-game
        docker run -d \
            --name "$CONTAINER_NAME" \
            --network host \
            -v "$LOG_DIR:/app/logs" \
            --restart no \
            --env-file backend/.env \
            earl-mlb-odds-history \
            python -c "
import asyncio, os, sys
sys.path.insert(0, '/app')

from app.ingestion.mlb_betting_lines import ingest_historical_odds_api_mlb_lines
from app.database import get_db_session
from datetime import date, datetime

async def main():
    async for db in get_db_session():
        result = await ingest_historical_odds_api_mlb_lines(
            db=db,
            api_key='',
            start_date=date(2021, 1, 1),
            end_date=None,  # today
            source_name='the_odds_api_historical',
            markets='totals,spreads,h2h',
            pause_between_games=False,  # let the function control delays
        )
        print(f'Result: {result}')
        break

asyncio.run(main())
" 2>&1

        echo "Started container: $CONTAINER_NAME"
        echo "Monitor with: $0 logs"
        echo "Check status: $0 status"
        ;;

    status)
        docker ps --filter "name=$CONTAINER_NAME" --format "{{.Status}}" 2>/dev/null || echo "Not running"
        docker logs --tail 5 "$CONTAINER_NAME" 2>/dev/null || echo "Container not found"
        ;;

    logs)
        shift
        docker logs "$CONTAINER_NAME" ${@:---tail 100 -f} 2>&1
        ;;

    stop)
        echo "Stopping $CONTAINER_NAME..."
        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
        ;;
esac
