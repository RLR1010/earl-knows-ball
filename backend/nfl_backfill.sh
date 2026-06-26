#!/bin/bash
# NFL betting lines backfill — runs per_game_backfill.py on the host
# Usage:
#   ./nfl_backfill.sh start        # Backfill 2021-2025 (opening + closing)
#   ./nfl_backfill.sh opening      # Backfill opening only
#   ./nfl_backfill.sh closing      # Backfill closing only
#   ./nfl_backfill.sh status       # Check running process
#   ./nfl_backfill.sh logs         # Tail logs
#   ./nfl_backfill.sh stop         # Kill running process

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="/tmp/nfl_backfill.log"
PID_FILE="/tmp/nfl_backfill.pid"
PYTHONPATH="$BASE_DIR"

case "${1:-start}" in
    start)
        echo "Starting NFL betting lines backfill (2021-2025)..."
        cd "$BASE_DIR"
        export PYTHONPATH
        export ODDS_API_KEY=965e3dd1bf2f0813fb208335a18f4ee3
        nohup python3 -m app.ingestion.per_game_backfill \
            --sport nfl \
            --start 2021 \
            --end 2025 \
            > "$LOG_FILE" 2>&1 &
        PID=$!
        echo $PID > "$PID_FILE"
        echo "Started PID $PID — log: $LOG_FILE"
        ;;
    opening)
        echo "Starting NFL opening lines backfill (2021-2025)..."
        cd "$BASE_DIR"
        export PYTHONPATH
        export ODDS_API_KEY=965e3dd1bf2f0813fb208335a18f4ee3
        nohup python3 -m app.ingestion.per_game_backfill \
            --sport nfl \
            --start 2021 \
            --end 2025 \
            --opening-only \
            > "$LOG_FILE" 2>&1 &
        PID=$!
        echo $PID > "$PID_FILE"
        echo "Started PID $PID (opening only) — log: $LOG_FILE"
        ;;
    closing)
        echo "Starting NFL closing lines backfill (2021-2025)..."
        cd "$BASE_DIR"
        export PYTHONPATH
        export ODDS_API_KEY=965e3dd1bf2f0813fb208335a18f4ee3
        nohup python3 -m app.ingestion.per_game_backfill \
            --sport nfl \
            --start 2021 \
            --end 2025 \
            --closing-only \
            > "$LOG_FILE" 2>&1 &
        PID=$!
        echo $PID > "$PID_FILE"
        echo "Started PID $PID (closing only) — log: $LOG_FILE"
        ;;
    status)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            if kill -0 "$PID" 2>/dev/null; then
                echo "NFL backfill is RUNNING (PID $PID)"
                echo "Log: $LOG_FILE"
                tail -5 "$LOG_FILE" 2>/dev/null || echo "(log empty)"
            else
                echo "NFL backfill NOT RUNNING (stale PID $PID)"
                rm -f "$PID_FILE"
            fi
        else
            echo "NFL backfill NOT RUNNING"
        fi
        ;;
    logs)
        tail -f "$LOG_FILE" 2>/dev/null || echo "Log file not found"
        ;;
    stop)
        if [ -f "$PID_FILE" ]; then
            PID=$(cat "$PID_FILE")
            kill "$PID" 2>/dev/null && echo "Stopped PID $PID" || echo "Process $PID not found"
            rm -f "$PID_FILE"
        else
            pkill -f "per_game_backfill.*--sport nfl" 2>/dev/null && echo "Stopped via pkill" || echo "No NFL backfill process found"
        fi
        ;;
    *)
        echo "Usage: $0 {start|opening|closing|status|logs|stop}"
        exit 1
        ;;
esac
