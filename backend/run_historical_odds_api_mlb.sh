#!/bin/bash
# Run historical MLB odds from The Odds API (paid tier)
# Queries odds-history endpoint per game date for closing-adjacent lines
#
# Usage:
#   ./run_historical_odds_api_mlb.sh            # Full backfill 2020-06-30 to today
#   ./run_historical_odds_api_mlb.sh 2025-01-01 # From a specific start date
#   ./run_historical_odds_api_mlb.sh 2025-01-01 2025-06-01  # Date range

CONTAINER="earl-knows-football-api-1"
START_DATE="${1:-2020-06-30}"
END_DATE="${2:-$(date +%Y-%m-%d)}"

echo "=== Historical MLB Odds API - The Odds API (paid tier) ==="
echo "Container: $CONTAINER"
echo "Date range: $START_DATE to $END_DATE"
echo ""

# Build curl command
CMD="curl -s -X POST \"http://localhost:8001/api/ingest/mlb/betting-lines/historical/odds-api?start_date=$START_DATE&end_date=$END_DATE&markets=totals\""

echo "Running in container: docker exec $CONTAINER $CMD"
echo ""

docker exec "$CONTAINER" bash -c "$CMD" | python3 -m json.tool 2>/dev/null || \
docker exec "$CONTAINER" bash -c "$CMD"
