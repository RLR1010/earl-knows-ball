#!/usr/bin/env bash
# NBA stats runner - uses host because nba_api works best on host
# Usage: ./nba_loader.sh {start|status|logs|stop}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
PID_FILE="$LOG_DIR/nba_loader.pid"
LOG_FILE="$LOG_DIR/nba_loader.log"

case "${1:-start}" in
    start)
        echo "Starting NBA stats ingestion..."
        if [ -f "$PID_FILE" ]; then
            kill $(cat "$PID_FILE") 2>/dev/null || true
            rm -f "$PID_FILE"
        fi

        # Use docker with --network=host override for the API container (has SQLAlchemy)
        docker exec -d earl-knows-football-api-1 bash -c "
            pip install -q nba_api 2>/dev/null
            cd /app
            nohup python3 -m app.ingestion.nba_stats > /tmp/nba_loader.log 2>&1 &
            echo \$! > /tmp/nba_loader.pid
            echo 'NBA stats loader started'
        "
        echo "Started! Check: docker exec earl-knows-football-api-1 tail -f /tmp/nba_loader.log"
        ;;

    status)
        PID=$(docker exec earl-knows-football-api-1 cat /tmp/nba_loader.pid 2>/dev/null)
        if [ -n "$PID" ]; then
            if docker exec earl-knows-football-api-1 kill -0 $PID 2>/dev/null; then
                echo "Running (PID: $PID)"
                docker exec earl-knows-football-api-1 tail -5 /tmp/nba_loader.log 2>/dev/null
            else
                echo "Stopped"
            fi
        else
            echo "Not running"
        fi
        ;;

    logs)
        docker exec earl-knows-football-api-1 tail -f /tmp/nba_loader.log 2>/dev/null
        ;;

    stop)
        PID=$(docker exec earl-knows-football-api-1 cat /tmp/nba_loader.pid 2>/dev/null)
        if [ -n "$PID" ]; then
            docker exec earl-knows-football-api-1 kill $PID 2>/dev/null || true
            echo "Stopped PID $PID"
        else
            echo "Not running"
        fi
        ;;

    *)
        echo "Usage: $0 {start|status|logs|stop}"
        exit 1
        ;;
esac
