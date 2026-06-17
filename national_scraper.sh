#!/bin/bash
# Standalone national news scraper runner.
# Runs outside the API container — survives Docker compose rebuilds.
#
# Usage:
#   ./national_scraper.sh          # start Last Word on Sports scrape
#   ./national_scraper.sh status   # check if running
#   ./national_scraper.sh logs     # see progress
#   ./national_scraper.sh stop     # stop gracefully

set -e

CONTAINER_NAME="earl-national"
IMAGE="earl-knows-football-api"

case "${1:-start}" in
  start)
    echo "Starting national news scraper..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -v "$(dirname "$0")/backend:/app" \
      "$IMAGE" \
      sh -c "
python3 << 'EOF'
import asyncio, sys, logging
sys.path.insert(0, '/app')
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.national_archives import scrape_all_sources, NATIONAL_SOURCES

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('earl.national')

async def run():
    logger.info(f'Scraping {len(NATIONAL_SOURCES)} sources...')
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        result = await scrape_all_sources(db=db, max_per_source=None, delay=0.5, embed=False)
    for s, st in result.get('sources', {}).items():
        if isinstance(st, dict):
            logger.info(f'  {s}: {st.get(\"articles_scraped\",0)} articles')
    logger.info(f'Total: {result.get(\"total_scraped\",0)} articles')
    await engine.dispose()

asyncio.run(run())
EOF
"
    echo "National scraper started."
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
    echo "Usage: $0 {start|status|logs|stop}"
    exit 1
    ;;
esac
