#!/usr/bin/env bash
# Runner for MLB historical odds API endpoint
# Calls the existing endpoint at localhost:8001
# Runs in a Docker container with the ODDS_API_KEY
# Usage: ./mlb_odds_api_history.sh {start|logs|status|stop|build}

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
CONTAINER_NAME="earl-mlb-odds-api-history"

build_image() {
    docker build -t "${CONTAINER_NAME}" -f- . <<'DOCKERFILE'
FROM alpine/curl:latest
RUN apk add --no-cache bash
COPY --chmod=755 .devcontainer/scripts/runner.sh /runner.sh
ENTRYPOINT ["/bin/bash", "/runner.sh"]
DOCKERFILE
}

case "${1:-start}" in
    start)
        echo "Starting MLB odds API historical ingestion..."
        docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

        # Need the API key from .env
        source backend/.env 2>/dev/null || true
        if [ -z "$ODDS_API_KEY" ]; then
            echo "ERROR: ODDS_API_KEY not found in backend/.env"
            exit 1
        fi

        docker run -d \
            --name "$CONTAINER_NAME" \
            --network host \
            --restart no \
            --entrypoint sh \
            alpine/curl:latest \
            -c "
                echo 'Starting MLB odds API historical ingestion from 2021-01-01 to today...'
                echo 'This will take a while — querying per game date from The Odds API (paid tier)'
                start=\$(date +%s)

                curl -s -X POST 'http://localhost:8001/ingest/mlb/betting-lines/historical/odds-api?start_date=2021-01-01&api_key=$ODDS_API_KEY' \
                    -H 'accept: application/json' \
                    --max-time 7200

                echo ''
                elapsed=\$(( \$(date +%s) - start ))
                echo \"Finished in \$((elapsed / 60))m \$((elapsed % 60))s\"
            " 2>&1

        echo "Started: $CONTAINER_NAME"
        echo "Monitor: ./mlb_odds_api_history.sh logs"
        ;;

    logs)
        docker logs "$CONTAINER_NAME" ${@:---tail 50 -f}
        ;;

    status)
        docker ps --filter "name=$CONTAINER_NAME" --format "{{.Names}}: {{.Status}}"
        docker logs "$CONTAINER_NAME" --tail 5 2>/dev/null || echo "No logs"
        ;;

    stop)
        docker rm -f "$CONTAINER_NAME" 2>/dev/null
        echo "Stopped"
        ;;
esac
