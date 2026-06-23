#!/bin/bash
# Standalone PFT archive scraper — ProFootballTalk NBC Sports sitemaps.
# Runs as a standalone Docker container — NOT tied to the API container.
# Docker compose rebuilds won't kill this.
#
# Usage:
#   ./pft_scraper.sh {start|start deep|rss|status|logs|stop}
#
#   start       — PFT archive backfill (50 most recent sitemaps ~ 87,500 articles)
#   start deep  — PFT deep backfill (all sitemaps — likely 200+ ~ 350,000 articles)
#   rss         — Fresh PFT RSS pull (most recent 50 articles only)
#   status      — check if running
#   logs        — see recent output
#   stop        — stop gracefully

set -e

CONTAINER_NAME="earl-pft"
IMAGE="earl-knows-football-api"

case "${1:-start}" in
  start)
    MAX_SITEMAPS="${2:-50}"
    DELAY="${3:-0.5}"
    echo "Starting PFT archive backfill (max ${MAX_SITEMAPS} sitemaps, ${DELAY}s delay)..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -v "$(dirname "$0")/backend:/app" \
      "$IMAGE" \
      sh -c "
python3 << INNERPY
import asyncio, sys, logging
sys.path.insert(0, '/app')
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.pft_archives import scrape_from_sitemaps

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('earl.pft')

async def run():
    logger.info('Starting PFT archive backfill (max_sitemaps=${MAX_SITEMAPS})...')
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        result = await scrape_from_sitemaps(
            db=db,
            max_sitemaps=${MAX_SITEMAPS},
            max_per_sitemap=None,
            delay=${DELAY},
        )
        logger.info(f'PFT archive done: {result}')
    await engine.dispose()
    logger.info('Done!')

asyncio.run(run())
INNERPY
"
    echo "Container $CONTAINER_NAME started."
    echo "Run './pft_scraper.sh logs' to see progress."
    ;;

  rss)
    MAX_ARTICLES="${2:-50}"
    echo "Starting PFT RSS pull (max ${MAX_ARTICLES} articles)..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -v "$(dirname "$0")/backend:/app" \
      "$IMAGE" \
      sh -c "
python3 << INNERPY
import asyncio, sys, logging
sys.path.insert(0, '/app')
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.pft_archives import scrape_latest_rss

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

async def run():
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        result = await scrape_latest_rss(db=db, max_articles=${MAX_ARTICLES})
        print(f'PFT RSS: {result}')
    await engine.dispose()

asyncio.run(run())
INNERPY
"
    echo "PFT RSS pull started."
    ;;

  status)
    if docker ps --format '{{.Names}}' | grep -q "^$CONTAINER_NAME$"; then
      echo "Running ✓"
      docker logs "$CONTAINER_NAME" --tail 3
    else
      echo "Not running ✗"
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
    echo "Usage: $0 {start|start deep|rss|status|logs|stop}"
    exit 1
    ;;
esac
