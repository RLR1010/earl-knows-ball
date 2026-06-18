#!/bin/bash
# nfl_loader.sh - Load NFL data from 2016 onwards
# Usage: ./nfl_loader.sh {start|status|logs|stop}
# Runs against the host Granian API on localhost:8001

SCRIPT_NAME="nfl_loader"
API_BASE="http://localhost:8001"
PID_FILE="/tmp/${SCRIPT_NAME}.pid"
LOG_FILE="/tmp/${SCRIPT_NAME}.log"
BACKEND_DIR="/home/rich/.openclaw/workspace/earl-knows-football/backend"

start_loader() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "NFL loader already running (PID $(cat "$PID_FILE"))"
        exit 1
    fi

    echo "Starting NFL data loader (2016-2025)..."

    nohup bash -c '
        set -e
        API="http://localhost:8001"
        LOG="'"$LOG_FILE"'"
        BACKEND="'"$BACKEND_DIR"'"

        log()   { echo "[$(date "+%H:%M:%S")] $*" | tee -a "$LOG"; }
        call()  { log ">>> POST $1"; curl -s -X POST "$API$1" 2>&1 | tee -a "$LOG"; echo; }

        # ─── 1. Generate seasons (creates nfl.seasons rows) ───
        log "=== STEP 1: Generate Seasons ==="
        call "/ingest/historical"

        # ─── 2. ESPN Schedule for each year (2016-2025) ───
        log "=== STEP 2: ESPN Schedule ==="
        for year in $(seq 2016 2025); do
            log "--- Loading ${year} schedule ---"
            call "/ingest/espn-schedule?season=${year}&season_type=2"
            sleep 1
        done

        # ─── 3. NFLverse Historical Player Stats (2016-2025) ───
        log "=== STEP 3: NFLverse Player Stats ==="
        call "/ingest/nflverse-historical?start=2016&end=2025"

        # ─── 4. Team Pace Stats ───
        log "=== STEP 4: Team Pace Stats ==="
        call "/ingest/nfl/pace"

        # ─── 5. NFLverse Trades ───
        log "=== STEP 5: NFLverse Trades ==="
        call "/ingest/nflverse/trades"

        # ─── 6. NFLverse Injuries (2016-2025) ───
        log "=== STEP 6: NFLverse Injuries ==="
        call "/ingest/nflverse/injuries?start_year=2016&end_year=2025"

        # ─── 7. NFLverse Draft Info ───
        log "=== STEP 7: Draft Info ==="
        call "/ingest/nflverse/draft"

        # ─── 8. Match Player IDs ───
        log "=== STEP 8: Match Players ==="
        call "/ingest/match-players"

        # ─── 9. Ourlads Historical Depth Charts (2016-2025) ───
        log "=== STEP 9: Ourlads Depth Chart Archive ==="
        cd "$BACKEND"
        export PYTHONPATH="$BACKEND"
        for year in $(seq 2016 2025); do
            log "--- Scraping Ourlads ${year} ---"
            python3 -m app.ingestion.ourlads_archive --year "$year" 2>&1 | tee -a "$LOG"
            sleep 2
        done

        log "=== ALL DONE ==="
    ' > "$LOG_FILE" 2>&1 &

    PID=$!
    echo $PID > "$PID_FILE"
    echo "NFL loader started (PID $PID). Log: $LOG_FILE"
}

status_loader() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "NFL loader is RUNNING (PID $PID)"
            echo "Last log lines:"
            tail -5 "$LOG_FILE" 2>/dev/null || echo "(no log yet)"
        else
            echo "NFL loader is NOT running (stale PID $PID)"
            echo "--- Last 20 log lines ---"
            tail -20 "$LOG_FILE" 2>/dev/null || echo "(no log)"
        fi
    else
        echo "NFL loader is NOT running"
    fi
}

logs_loader() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "No log file found"
    fi
}

stop_loader() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Stopping NFL loader (PID $PID)..."
            kill "$PID"
            rm -f "$PID_FILE"
            echo "Stopped."
        else
            echo "Process not running. Cleaning up PID file."
            rm -f "$PID_FILE"
        fi
    else
        echo "No NFL loader running"
    fi
}

case "${1:-start}" in
    start)      start_loader ;;
    status)     status_loader ;;
    logs)       logs_loader ;;
    stop)       stop_loader ;;
    *)
        echo "Usage: $0 {start|status|logs|stop}"
        exit 1
        ;;
esac
