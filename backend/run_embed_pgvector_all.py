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

import httpx
from sqlalchemy import create_engine, text
import os

# Allow DB host override via env var (for standalone container deployment)
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_USER = os.environ.get("DB_USER", "earl")
DB_PASS = os.environ.get("DB_PASS", "earl_dev_pass")
DB_NAME = os.environ.get("DB_NAME", "earl_knows_football")

SYNC_DB_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:5432/{DB_NAME}"
engine = create_engine(SYNC_DB_URL, pool_pre_ping=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("earl.embed_pgvector")

OLLAMA_URL = "http://localhost:11434/api/embed"
BATCH_SIZE = 20
SLEEP_BETWEEN = 60  # seconds between full cycles

SPORTS = [
    {"name": "NFL", "schema": "nfl"},
    {"name": "NBA", "schema": "nba"},
    {"name": "MLB", "schema": "mlb"},
]


def embed_text(text_to_embed: str) -> list[float] | None:
    """Embed a single text string via Ollama."""
    text_to_embed = text_to_embed.strip()[:2500]
    if len(text_to_embed) < 10:
        return None

    for attempt in range(3):
        try:
            resp = httpx.post(
                OLLAMA_URL,
                json={"model": "snowflake-arctic-embed2", "input": text_to_embed},
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            # /api/embed returns {"embeddings": [[...]]} for arctic-embed2
            embs = data.get("embeddings", data.get("embedding", []))
            if isinstance(embs, list) and len(embs) > 0 and isinstance(embs[0], list):
                return embs[0]  # [[floats]] → [floats]
            return embs  # [floats] as-is
        except Exception as e:
            logger.warning(f"Ollama attempt {attempt + 1} failed: {e}")
            time.sleep(1)

    return None


def embed_sport(schema: str) -> int:
    """Embed one batch of articles for a given schema. Returns count embedded."""
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                f"SELECT id, title, body FROM {schema}.articles "
                f"WHERE embedded_at IS NULL ORDER BY id LIMIT {BATCH_SIZE}"
            )
        ).fetchall()

    if not rows:
        return 0

    count = 0
    for row in rows:
        article_id = row[0]
        title = row[1] or ""
        body = row[2] or ""
        content = f"{title}\n\n{body}"[:2500]

        if len(content.strip()) < 10:
            # Mark as embedded with epoch (effectively skipped)
            with engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE {schema}.articles SET embedded_at = 'epoch' WHERE id = :id"),
                    {"id": article_id},
                )
            continue

        embedding = embed_text(content)
        if embedding is None:
            logger.error(f"[{schema}] Failed to embed article {article_id}, skipping")
            continue

        # Insert / upsert into article_embeddings
        with engine.begin() as conn:
            # Check if embedding exists
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
        if count % 5 == 0:
            logger.info(f"[{schema}] Embedded {count}/{len(rows)} this batch")

        time.sleep(0.2)  # Rate limiting

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
