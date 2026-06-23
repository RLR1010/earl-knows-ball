#!/bin/bash
# Standalone HoopsRumors archive scraper — all NBA articles via paginated RSS.
# Runs as a standalone Docker container — NOT tied to the API container.
# Docker compose rebuilds won't kill this.
#
# Usage:
#   ./hoopsrumors_scraper.sh {start|start deep|status|logs|stop}
#
#   start       — HoopsRumors archive (max 500 pages, newest first)
#   start deep  — HoopsRumors full archive (all pages, ~5000+, ~75k articles)
#   status      — check if running
#   logs        — see recent output
#   stop        — stop gracefully

set -e

CONTAINER_NAME="earl-hoopsrumors"
IMAGE="earl-knows-football-api"

case "${1:-start}" in
  start)
    MAX_PAGES="${2:-500}"
    DELAY="${3:-0.5}"
    echo "Starting HoopsRumors archive backfill (max ${MAX_PAGES} pages, ${DELAY}s delay)..."
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
from app.ingestion.articles_hoopsrumors import scrape_hoopsrumors_all

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('earl.hoopsrumors')

async def run():
    logger.info('Starting HoopsRumors archive (max_pages=${MAX_PAGES})...')
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        result = await scrape_hoopsrumors_all(
            db=db,
            max_pages=${MAX_PAGES},
            max_articles=None,
            delay=${DELAY},
        )
        logger.info(f'HoopsRumors done: {result}')
    await engine.dispose()
    logger.info('Done!')

asyncio.run(run())
INNERPY
"
    echo "Container $CONTAINER_NAME started."
    echo "Run './hoopsrumors_scraper.sh logs' to see progress."
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
