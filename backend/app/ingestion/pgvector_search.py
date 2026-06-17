"""
pgvector semantic search for articles.
Replaces the old Cognee-NFL search.
"""
import logging
from typing import Optional

import httpx
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article
from app.models.nba import NBAArticle
from app.models.mlb import MLBArticle

logger = logging.getLogger("earl.pgvector_search")

# Sport → schema / model / embed table mapping
SPORT_CONFIG = {
    "nfl": {"embed_table": "nfl.article_embeddings", "article_table": "nfl.articles", "model": Article},
    "nba": {"embed_table": "nba.article_embeddings", "article_table": "nba.articles", "model": NBAArticle},
    "mlb": {"embed_table": "mlb.article_embeddings", "article_table": "mlb.articles", "model": MLBArticle},
}

OLLAMA_EMBED_URL = "http://localhost:11434/api/embed"


async def embed_query(query: str) -> Optional[list[float]]:
    """Embed a text query using Ollama's snowflake-arctic-embed2."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(OLLAMA_EMBED_URL, json={
                "model": "snowflake-arctic-embed2",
                "input": query,
            })
            if resp.status_code != 200:
                logger.warning(f"Ollama embed returned {resp.status_code}")
                return None
            return resp.json()["embeddings"][0]
    except Exception as e:
        logger.error(f"Ollama embed error: {e}")
        return None


async def search_articles(
    db: AsyncSession,
    query: str,
    top_k: int = 8,
    scope_teams: Optional[list[str]] = None,
    sport: str = "nfl",
) -> list[dict]:
    """
    Search articles via pgvector cosine similarity.

    Args:
        sport: "nfl", "nba", or "mlb"

    Returns list of {text, title, source_name} ordered by relevance.
    """
    sport = sport.lower()
    cfg = SPORT_CONFIG.get(sport, SPORT_CONFIG["nfl"])
    embed_table = cfg["embed_table"]
    article_table = cfg["article_table"]
    model = cfg["model"]

    embedding = await embed_query(query)
    if embedding is None:
        return []

    # Build embedding string for pgvector query
    emb_str = "[" + ",".join(str(round(x, 8)) for x in embedding) + "]"

    # Search with cosine similarity
    sql = text(f"""
        SELECT a.id, a.title, a.body, a.source_name,
               ae.embedding <-> '{emb_str}'::vector AS distance
        FROM {embed_table} ae
        JOIN {article_table} a ON a.id = ae.article_id
        ORDER BY ae.embedding <-> '{emb_str}'::vector
        LIMIT :top_k
    """)

    r = await db.execute(sql, {"top_k": top_k})
    results = r.fetchall()

    articles = []
    for row in results:
        title = row.title or ""
        body = row.body or ""
        source = row.source_name or "Unknown"
        text_content = f"# {title}\n\nSource: {source}\n\n{body[:800]}" if body else f"# {title}"

        articles.append({
            "text": text_content[:1500],
            "title": title or "",
            "source_name": source,
            "distance": round(float(row.distance), 4),
        })

    # If no results from vector search, fall back to keyword search
    if not articles:
        logger.info("Vector search returned no results, falling back to keyword search")
        keyword_r = await db.execute(
            select(model)
            .where(
                model.title.ilike(f"%{query}%")
            )
            .limit(top_k)
        )
        for article in keyword_r.scalars().all():
            text_content = f"# {article.title}\n\nSource: {article.source_name or 'Unknown'}"
            if article.body:
                text_content += f"\n\n{article.body[:800]}"
            articles.append({
                "text": text_content[:1500],
                "title": article.title or "",
                "source_name": article.source_name or "Unknown",
                "distance": 0,
            })

    return articles


async def search_articles_chat(
    db: AsyncSession,
    message: str,
    matched_team_names: Optional[list[str]] = None,
    prev_teams: Optional[list[str]] = None,
    top_k: int = 8,
    sport: str = "nfl",
) -> list[dict]:
    """
    Search articles for chat context.
    Scopes the search by scoping team names from the current and previous turn.
    """
    search_query = message
    scope_names = list(set((matched_team_names or []) + (prev_teams or [])))
    if scope_names:
        search_query = f"{' '.join(scope_names)} {message}"

    articles = await search_articles(db, search_query, top_k=top_k * 2, sport=sport)  # fetch more for filtering

    # Filter by team scope
    if scope_names and articles:
        filtered = []
        for a in articles:
            text = a.get("text", "")
            if any(name.lower() in text.lower() for name in scope_names):
                filtered.append(a)
        if not filtered:
            filtered = articles[:1]
        articles = filtered[:top_k]

    return articles[:top_k]
