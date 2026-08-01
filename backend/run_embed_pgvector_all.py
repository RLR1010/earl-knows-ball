"""
Multi-sport pgvector embedding runner.
Embeds NFL, NBA, and MLB articles via Ollama snowflake-arctic-embed2.
Stores vectors in each sport's article_embeddings table.
Runs in an infinite loop, processing one sport at a time.

Uses synchronous httpx to avoid asyncio issues in some container setups.
"""
import sys
import time
import logging
import traceback
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
import os

# DB URL from single source of truth; DB_HOST override for container deployments
from app.db_urls import PSYCOPG2_DATABASE_URL
from app.ollama_embed import embed_batch_sync, embed_sync

DB_HOST = os.environ.get("DB_HOST", "localhost")
SYNC_DB_URL = PSYCOPG2_DATABASE_URL.replace("@localhost:", f"@{DB_HOST}:")
engine = create_engine(SYNC_DB_URL, pool_pre_ping=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("earl.embed_pgvector")

BATCH_SIZE = 20
SLEEP_BETWEEN = 60  # seconds between full cycles

SPORTS = [
    {"name": "NFL", "schema": "nfl"},
    {"name": "NBA", "schema": "nba"},
    {"name": "MLB", "schema": "mlb"},
]


def embed_text(text_to_embed: str) -> list[float] | None:
    """Embed a single text string via the load-balanced dual-GPU client."""
    text_to_embed = text_to_embed.strip()[:2500]
    if len(text_to_embed) < 10:
        return None

    for attempt in range(3):
        try:
            return embed_sync(text_to_embed, timeout=120.0)
        except Exception as e:
            logger.warning(f"Embedding attempt {attempt + 1} failed: {e}")
            time.sleep(1)

    return None


def embed_sport(schema: str) -> int:
    """Embed one batch of articles for a given schema. Returns count embedded.

    Embeds the whole batch in one call split across both GPU instances
    concurrently (~2x throughput), then upserts vectors in a single transaction.
    """
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                f"SELECT id, title, body FROM {schema}.articles "
                f"WHERE embedded_at IS NULL ORDER BY id LIMIT {BATCH_SIZE}"
            )
        ).fetchall()

    if not rows:
        return 0

    # Build the content list; mark empty/too-short articles as skipped (epoch)
    pending: list[tuple[int, str]] = []  # (article_id, content)
    skip_ids: list[int] = []
    for row in rows:
        article_id = row[0]
        title = row[1] or ""
        body = row[2] or ""
        content = f"{title}\n\n{body}"[:2500].strip()
        if len(content) < 10:
            skip_ids.append(article_id)
            continue
        pending.append((article_id, content))

    if skip_ids:
        with engine.begin() as conn:
            for aid in skip_ids:
                conn.execute(
                    text(f"UPDATE {schema}.articles SET embedded_at = 'epoch' WHERE id = :id"),
                    {"id": aid},
                )

    if not pending:
        return 0

    # Batch embed with retries; module handles GPU split + failover
    embeddings = None
    for attempt in range(3):
        try:
            embeddings = embed_batch_sync([c for _, c in pending], timeout=180.0)
            break
        except Exception as e:
            logger.warning(f"Batch embed attempt {attempt + 1} failed: {e}")
            time.sleep(1)
    if embeddings is None:
        logger.error(f"[{schema}] Failed to embed batch of {len(pending)} articles")
        return 0

    count = 0
    with engine.begin() as conn:
        for (article_id, _), embedding in zip(pending, embeddings):
            existing = conn.execute(
                text(f"SELECT id FROM {schema}.article_embeddings WHERE article_id = :id LIMIT 1"),
                {"id": article_id},
            ).fetchone()

            if existing:
                conn.execute(
                    text(f"UPDATE {schema}.article_embeddings SET embedding = :vector WHERE article_id = :id"),
                    {"vector": embedding, "id": article_id},
                )
            else:
                conn.execute(
                    text(f"INSERT INTO {schema}.article_embeddings (article_id, embedding) VALUES (:id, :vector)"),
                    {"id": article_id, "vector": embedding},
                )

            conn.execute(
                text(f"UPDATE {schema}.articles SET embedded_at = NOW() WHERE id = :id"),
                {"id": article_id},
            )
            count += 1

    logger.info(f"[{schema}] Embedded {count}/{len(rows)} this batch")
    return count


def run():
    logger.info("Embedding runner started (sync httpx)")
    t0 = time.time()

    while True:
        any_pending = False
        totals = {}

        for sport in SPORTS:
            try:
                n = embed_sport(sport["schema"])
                totals[sport["name"]] = n
                if n:
                    any_pending = True
                    logger.info(f"[{sport['name']}] Embedded {n} articles")
                else:
                    logger.info(f"[{sport['name']}] No unembedded articles")
            except Exception as e:
                logger.error(f"[{sport['name']}] Error: {e}")
                traceback.print_exc()

        if not any_pending:
            elapsed = time.time() - t0
            total_all = sum(totals.values())
            logger.info(
                f"All sports embedded! Total this cycle: {total_all} | "
                f"Elapsed: {elapsed:.0f}s | Sleeping {SLEEP_BETWEEN}s..."
            )
            t0 = time.time()
            time.sleep(SLEEP_BETWEEN)
        else:
            time.sleep(1)


if __name__ == "__main__":
    run()
