#!/bin/bash
# MLB Stats Ingestion Pipeline
# Runs all three steps sequentially:
#   1. mlb_stats: teams, players, seasons, games, season-level batting/pitching
#   2. boxscore_ingest: per-game batting boxscore stats
#   3. mlb_pitcher_stats: per-game pitching stats
#
# Usage:
#   ./ingest_mlb_stats.sh              # Full pipeline (all years)
#   ./ingest_mlb_stats.sh --year 2026  # Single year
#   ./ingest_mlb_stats.sh --games 100  # Limit games (for testing)

set -e

API_CONTAINER="backend-api-1"
ARGS="${*:---}"

echo "===== MLB STATS PIPELINE ===== $(date) ====="
echo ""

# ── Step 1: mlb_stats (teams, players, seasons, games, season stats) ──
echo "🔄 [1/3] Running mlb_stats (teams, players, seasons, games, season stats)..."
START_1=$(date +%s)
docker exec "$API_CONTAINER" python -m app.ingestion.mlb_stats $ARGS
END_1=$(date +%s)
GAME_COUNT=$(docker exec "$API_CONTAINER" sh -c "python3 -c \"
import asyncio
from app.database import async_session
from sqlalchemy import text
async def c():
    async with async_session() as db:
        r = await db.execute(text('SELECT count(*) FROM mlb.games'))
        return r.scalar()
print(asyncio.run(c()))
\"")
echo "✅ [1/3] Done in $((END_1 - START_1))s — $GAME_COUNT games loaded"
echo ""

# ── Step 2: Boxscore batting stats ──
echo "🔄 [2/3] Running boxscore_ingest (per-game batting stats)..."
START_2=$(date +%s)
docker exec "$API_CONTAINER" python -m app.ingestion.boxscore_ingest $ARGS
END_2=$(date +%s)
BAT_COUNT=$(docker exec "$API_CONTAINER" sh -c "python3 -c \"
import asyncio
from app.database import async_session
from sqlalchemy import text
async def c():
    async with async_session() as db:
        r = await db.execute(text('SELECT count(*) FROM mlb.batting_game_stats'))
        return r.scalar()
print(asyncio.run(c()))
\"")
echo "✅ [2/3] Done in $((END_2 - START_2))s — $BAT_COUNT batting_game_stats rows"
echo ""

# ── Step 3: Pitcher game stats ──
echo "🔄 [3/3] Running mlb_pitcher_stats (per-game pitching stats)..."
START_3=$(date +%s)
docker exec "$API_CONTAINER" python -m app.ingestion.mlb_pitcher_stats $ARGS
END_3=$(date +%s)
PIT_COUNT=$(docker exec "$API_CONTAINER" sh -c "python3 -c \"
import asyncio
from app.database import async_session
from sqlalchemy import text
async def c():
    async with async_session() as db:
        r = await db.execute(text('SELECT count(*) FROM mlb.pitcher_game_stats'))
        return r.scalar()
print(asyncio.run(c()))
\"")
echo "✅ [3/3] Done in $((END_3 - START_3))s — $PIT_COUNT pitcher_game_stats rows"
echo ""

echo "===== MLB STATS PIPELINE COMPLETE ====="
echo "Games:       $GAME_COUNT"
echo "Batting rows: $BAT_COUNT"
echo "Pitching rows: $PIT_COUNT"
