#!/bin/bash
# nba_loader.sh - Load NBA data from 2016 onwards
# Usage: ./nba_loader.sh {start|status|logs|stop}
# Runs scripts directly (avoids API endpoint bugs)

SCRIPT_NAME="nba_loader"
PID_FILE="/tmp/${SCRIPT_NAME}.pid"
LOG_FILE="/tmp/${SCRIPT_NAME}.log"
BACKEND_DIR="/home/rich/.openclaw/workspace/earl-knows-football/backend"

start_loader() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "NBA loader already running (PID $(cat "$PID_FILE"))"
        exit 1
    fi

    echo "Starting NBA data loader (2016-2025)..."

    nohup bash -c '
        LOG="'"$LOG_FILE"'"
        BACKEND="'"$BACKEND_DIR"'"

        log() { echo "[$(date "+%H:%M:%S")] $*" | tee -a "$LOG"; }

        cd "$BACKEND"
        export PYTHONPATH="$BACKEND"

        # ─── 1. NBA Players ───
        log "=== STEP 1: NBA Players (from ESPN rosters) ==="
        python3 -m app.ingestion.nba_players 2>&1 | tee -a "$LOG"

        # ─── 2. NBA Games & Schedule per season ───
        log "=== STEP 2: NBA Games / Schedule ==="
        for year in $(seq 2024 -1 2016); do
            log "--- Loading ${year}-${year+1} schedule ---"
            python3 -c "
import asyncio
from app.database import AsyncSessionLocal
from app.ingestion.espn_nba import ingest_nba_schedule

async def run():
    async with AsyncSessionLocal() as session:
        result = await ingest_nba_schedule(session, season_year='${year}')
        await session.commit()
        print(f'Result: {result}')

asyncio.run(run())
" 2>&1 | tee -a "$LOG"
            sleep 2
        done

        # ─── 3. NBA Player Season Stats (from Basketball-Reference) ───
        # Runs per-season. The module loads 2 seasons at a time.
        log "=== STEP 3: NBA Player Season Stats ==="
        for year in $(seq 2016 1 2024); do
            log "--- Fetching stats for ${year}-${year+1} season ---"
            python3 -c "
import asyncio
from app.ingestion.nba_stats import load_all
asyncio.run(load_all($year, $year+1))
" 2>&1 | tee -a "$LOG"
            sleep 3
        done

        log "=== ALL DONE ==="
    ' > "$LOG_FILE" 2>&1 &

    PID=$!
    echo $PID > "$PID_FILE"
    echo "NBA loader started (PID $PID). Log: $LOG_FILE"
}

status_loader() {
    if [ -f "$PID_FILE" ]; then
        log "NBA loader is RUNNING (PID $(cat "$PID_FILE"))"
        tail -5 "$LOG_FILE" 2>/dev/null || true
    else
        echo "NBA loader is NOT running"
    fi
}

logs_loader() {
    tail -f "$LOG_FILE" 2>/dev/null || echo "No log"
}

stop_loader() {
    [ -f "$PID_FILE" ] && kill "$(cat "$PID_FILE")" 2>/dev/null && rm -f "$PID_FILE" && echo "Stopped" || echo "Not running"
}

case "${1:-start}" in
    start)  start_loader ;;
    status) status_loader ;;
    logs)   logs_loader ;;
    stop)   stop_loader ;;
    *)      echo "Usage: $0 {start|status|logs|stop}" ;;
esac
