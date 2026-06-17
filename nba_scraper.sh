#!/bin/bash
# Standalone NBA scraper — SB Nation team blog archives + HoopsRumors.
# Runs as a standalone Docker container — NOT tied to the API container.
# Docker compose rebuilds won't kill this.
#
# Usage:
#   ./nba_scraper.sh {start|start deep|hoopsrumors|rss|status|logs|stop}
#
#   start          — SB Nation NBA blog archive backfill (2025+ by default)
#   start deep     — SB Nation NBA blog deep backfill (2024+)
#   hoopsrumors    — HoopsRumors historical archive (~75k articles, 2011-present)
#   rss            — Fresh NBA RSS feed pull (recent articles only)
#   status         — check if running
#   logs           — see recent output
#   stop           — stop gracefully

set -e

CONTAINER_NAME="earl-nba"
IMAGE="earl-knows-football-api"

case "${1:-start}" in
  start)
    START_YEAR="${2:-2025}"
    echo "Starting NBA SB Nation archive backfill (from ${START_YEAR})..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -v "$(dirname "$0")/backend:/app" \
      "$IMAGE" \
      sh -c "
python3 << 'INNERPY'
import asyncio, sys, logging
sys.path.insert(0, '/app')
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.sbnation_archives import scrape_all_blogs, NBA_BLOGS

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('earl.nba')

async def run():
    logger.info('Starting NBA SB Nation backfill...')
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        result = await scrape_all_blogs(
            db=db,
            start_year=${START_YEAR},
            delay=0.75,
            max_per_blog=None,
            blogs=NBA_BLOGS,
        )
        logger.info(f'NBA backfill done: {result.get(\"total_scraped\",0)} scraped, {result.get(\"total_skipped\",0)} skipped')
    await engine.dispose()
    logger.info('Done!')

asyncio.run(run())
INNERPY
"
    echo "Container $CONTAINER_NAME started."
    echo "Run './nba_scraper.sh logs' to see progress."
    ;;

  hoopsrumors)
    echo "Starting HoopsRumors historical archive crawl..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -v "$(dirname "$0")/backend:/app" \
      "$IMAGE" \
      sh -c '
python3 << INNERPY
import asyncio, logging
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.articles_hoopsrumors import scrape_hoopsrumors_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def run():
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        result = await scrape_hoopsrumors_all(db=db, delay=0.75)
        print(f"HoopsRumors: {result}")
    await engine.dispose()

asyncio.run(run())
INNERPY
'
    echo "HoopsRumors crawl started."
    echo "Run './nba_scraper.sh logs' to see progress."
    ;;

  rss)
    echo "Starting NBA RSS feed pull..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -v "$(dirname "$0")/backend:/app" \
      "$IMAGE" \
      sh -c "
python3 << 'INNERPY'
import asyncio, logging
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.articles_nba import scrape_rss_feeds_nba

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

async def run():
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        result = await scrape_rss_feeds_nba(db=db, max_per_feed=20, skip_older_than_days=30)
        print(f'NBA RSS: {result}')
    await engine.dispose()

asyncio.run(run())
INNERPY
"
    echo "NBA RSS pull started."
    ;;

  status)
    if docker ps --format '{{.Names}}' | grep -q "^$CONTAINER_NAME$"; then
      echo "Running"
      docker logs "$CONTAINER_NAME" --tail 3
    else
      echo "Not running"
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
