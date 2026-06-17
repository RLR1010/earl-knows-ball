#!/bin/bash
# Standalone batch embedding for existing articles.
# Pushes un-embedded articles to Cognee-NFL for semantic search.
# NOT tied to the API container — survives Docker compose rebuilds.
#
# Usage:
#   ./embed.sh              # embed the next batch of articles
#   ./embed.sh status       # check if running
#   ./embed.sh logs         # see progress
#   ./embed.sh stop         # stop gracefully

set -e

CONTAINER_NAME="earl-embed"
IMAGE="earl-knows-football-api"

case "${1:-start}" in
  start)
    echo "Starting standalone embed container..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -v "$(dirname "$0")/backend:/app" \
      "$IMAGE" \
      python3 -c "
import asyncio, sys, logging
sys.path.insert(0, '/app')
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.articles import batch_embed_articles
logging.basicConfig(level=logging.INFO)
async def run():
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession)
    async with SessionLocal() as db:
        result = await batch_embed_articles(db, limit=50)
        print(result)
    await engine.dispose()
asyncio.run(run())
"
    echo "Embed container started. Embeds 50 articles per run."
    echo "Run again to embed the next batch."
    ;;

  loop)
    echo "Starting continuous embedding (50 articles per batch, looping)..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    docker run -d --name "$CONTAINER_NAME" \
      --network host \
      -v "$(dirname "$0")/backend:/app" \
      "$IMAGE" \
      sh -c "
while true; do
  python3 -c \"
import asyncio, sys, logging
sys.path.insert(0, '/app')
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.articles import batch_embed_articles
logging.basicConfig(level=logging.INFO)
async def run():
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession)
    async with SessionLocal() as db:
        result = await batch_embed_articles(db, limit=50)
        print(f'Embedded: {result}')
    await engine.dispose()
asyncio.run(run())
\" 2>&1
  echo 'Waiting 30s before next batch...'
  sleep 30
done
"
    echo "Continuous embed started."
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
    docker logs "$CONTAINER_NAME" --tail 20
    ;;

  stop)
    echo "Stopping $CONTAINER_NAME..."
    docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
    echo "Stopped."
    ;;

  *)
    echo "Usage: $0 {start|loop|status|logs|stop}"
    exit 1
    ;;
esac
