#!/bin/bash
# Standalone FanGraphs archive scraper — MLB articles.
# Runs as a standalone Docker container — NOT tied to the API container.
# Docker compose rebuilds won't kill this.
#
# Usage:
#   ./fangraphs_scraper.sh {start|start deep|status|logs|stop}
#
#   start       — FanGraphs archive (2021–present, newest first)
#   start deep  — FanGraphs full archive (2010–present, newest first)
#   status      — check if running
#   logs        — see recent output
#   stop        — stop gracefully

set -e

CONTAINER_NAME="earl-fangraphs"
IMAGE="earl-knows-football-api"

case "${1:-start}" in
  start)
    START_YEAR="${2:-2021}"
    echo "Starting FanGraphs archive backfill (from ${START_YEAR})..."
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
from app.ingestion.articles_fangraphs import scrape_fangraphs_all

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('earl.fangraphs')

async def run():
    logger.info('Starting FanGraphs archive (from ${START_YEAR})...')
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        result = await scrape_fangraphs_all(
            db=db,
            start_year=${START_YEAR},
            end_year=2010,
            delay=0.75,
            max_articles=None,
        )
        logger.info(f'FanGraphs done: {result}')
    await engine.dispose()
    logger.info('Done!')

asyncio.run(run())
INNERPY
"
    echo "Container $CONTAINER_NAME started."
    echo "Run './fangraphs_scraper.sh logs' to see progress."
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
    echo "Usage: $0 {start|start deep|status|logs|stop}"
    exit 1
    ;;
esac
