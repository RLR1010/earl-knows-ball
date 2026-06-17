"""Embed articles that haven't been embedded yet for a given sport."""
import asyncio
import logging
import sys

import httpx
from sqlalchemy import text
from app.database import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.embed")

OLLAMA_URL = "http://localhost:11434"
MODEL = "snowflake-arctic-embed2"


async def embed_articles(schema: str):
    schema = schema.lower()
    embed_table = f"{schema}.article_embeddings"
    articles_table = f"{schema}.articles"

    async with engine.begin() as conn:
        result = await conn.execute(text(f"""
            SELECT id, title, content
            FROM {articles_table}
            WHERE embedded_at IS NULL
            ORDER BY id
            LIMIT 10
        """))
        articles = result.fetchall()

    if not articles:
        return 0

    async with httpx.AsyncClient(timeout=60.0) as client:
        for article in articles:
            text_to_embed = (article.title or "") + " " + (article.content or "")
            text_to_embed = text_to_embed.strip()[:2000]
            if not text_to_embed:
                continue

            for attempt in range(3):
                try:
                    resp = await client.post(
                        f"{OLLAMA_URL}/api/embeddings",
                        json={"model": MODEL, "prompt": text_to_embed},
                    )
                    resp.raise_for_status()
                    embedding = resp.json()["embedding"]

                    async with engine.begin() as conn:
                        await conn.execute(text(f"""
                            INSERT INTO {embed_table} (article_id, embedding)
                            VALUES (:aid, :emb::vector)
                        """), {"aid": article.id, "emb": str(embedding)})
                        await conn.execute(text(f"""
                            UPDATE {articles_table}
                            SET embedded_at = NOW()
                            WHERE id = :aid
                        """), {"aid": article.id})

                    logger.info(f"[{schema}] Embedded article {article.id}")
                    break
                except Exception as e:
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.error(f"[{schema}] Failed article {article.id}: {e}")

            await asyncio.sleep(0.2)

    return len(articles)


async def run(schema: str):
    n = await embed_articles(schema)
    if n == 0:
        logger.info(f"[{schema}] No unembedded articles found")


if __name__ == "__main__":
    schema = sys.argv[1] if len(sys.argv) > 1 else "nfl"
    asyncio.run(run(schema))
