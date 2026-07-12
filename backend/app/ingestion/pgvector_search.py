"""Vector + keyword article search for enrichment pipeline."""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select, text

logger = logging.getLogger(__name__)


async def search_articles_chat(
    db,
    message: str,
    matched_team_names: list[str] | None = None,
    prev_teams: list[str] | None = None,
    top_k: int = 5,
    sport: str | None = None,
) -> list[dict]:
    """Search articles for chat context enrichment.

    Builds a query from the user message plus any matched team names,
    then delegates to search_articles for vector (embedding) + keyword search.
    """
    # Build a richer query from message + team context
    query = message
    if matched_team_names:
        team_str = " ".join(matched_team_names)
        query = f"{message} {team_str}"
    if prev_teams:
        prev_str = " ".join(prev_teams)
        query = f"{query} {prev_str}"

    return await search_articles(
        db=db,
        query=query,
        sport=sport,
        top_k=top_k,
    )

SPORT_CONFIGS: dict[str, dict] = {
    "mlb": {"embed_table": "mlb.article_embeddings", "article_table": "mlb.articles"},
    "nfl": {"embed_table": "nfl.article_embeddings", "article_table": "nfl.articles"},
    "nba": {"embed_table": "nba.article_embeddings", "article_table": "nba.articles"},
}


async def search_articles(
    db,
    query: str,
    sport: str = None,
    date_from: str = None,
    date_to: str = None,
    top_k: int = 10,
) -> list[dict]:
    """Search articles by embedding similarity (if possible) or keyword fallback.

    Returns list of dicts with keys: text, title, source_name, distance, published_at.
    """
    cfg = SPORT_CONFIGS.get(sport, {})
    embed_table = cfg.get("embed_table", "article_embeddings")
    article_table = cfg.get("article_table", "articles")

    # Build WHERE clause for date range
    # NOTE: sport column doesn't exist on articles tables (they're schema-specific)
    where_sql = "WHERE 1=1"
    if date_from:
        dttm_from: str = date_from.strftime("%Y-%m-%d") if isinstance(date_from, datetime) else str(date_from)
        where_sql += f" AND a.published_at >= '{dttm_from}'"
    if date_to:
        dttm_to: str = date_to.strftime("%Y-%m-%d") if isinstance(date_to, datetime) else str(date_to)
        where_sql += f" AND a.published_at <= '{dttm_to}'"

    # Try vector search via Ollama embedding
    try:
        import requests as req

        embed_resp = req.post(
            "http://localhost:11434/api/embeddings",
            json={"model": "snowflake-arctic-embed2:latest", "prompt": query},
            timeout=10,
        )
        embed_resp.raise_for_status()
        embedding = embed_resp.json()["embedding"]

        emb_str = "[" + ",".join(str(v) for v in embedding) + "]"

        sql = text(
            f"""
            SELECT a.id, a.title, a.body, a.source_name, a.published_at,
                   ae.embedding <-> '{emb_str}'::vector AS distance
            FROM {embed_table} ae
            JOIN {article_table} a ON a.id = ae.article_id
            {where_sql}
            ORDER BY ae.embedding <-> '{emb_str}'::vector
            LIMIT :top_k
            """
        )

        if hasattr(db, "execute"):
            result = await db.execute(sql, {"top_k": top_k})
            rows = result.mappings().fetchall()
        else:
            mapped = db.compile(sql, compile_kwargs={"literal_binds": True})
            mapped_str = str(mapped)
            mapped_str = mapped_str.replace(":top_k", str(top_k))
            rows = await db.fetch(mapped_str)

        articles = []
        for row in rows:
            if hasattr(row, "_mapping"):
                row = dict(row._mapping)
            title = row.get("title") or ""
            body = row.get("body") or ""
            source = row.get("source_name") or "Unknown"
            text_content = (
                f"# {title}\n\nSource: {source}\n\n{body[:800]}"
                if body
                else f"# {title}"
            )
            pub_date = row.get("published_at")
            pub_date_str = pub_date.strftime("%Y-%m-%d") if pub_date else ""

            articles.append({
                "text": text_content[:1500],
                "title": title,
                "source_name": source,
                "distance": round(float(row.get("distance", 0)), 4),
                "published_at": pub_date_str,
            })

        if articles:
            return articles

    except Exception as exc:
        logger.warning("Vector search failed for query %s: %s", query, exc)
        try:
            await db.rollback()
        except Exception:
            pass

    # Keyword fallback
    try:
        where_sql_keyword = "WHERE 1=1"
        # sport column doesn't exist on articles (tables are schema-specific)
        if date_from:
            dttm_from_2: str = date_from.strftime("%Y-%m-%d") if isinstance(date_from, datetime) else str(date_from)
            where_sql_keyword += f" AND {article_table}.published_at >= '{dttm_from_2}'"
        if date_to:
            dttm_to_2: str = date_to.strftime("%Y-%m-%d") if isinstance(date_to, datetime) else str(date_to)
            where_sql_keyword += f" AND {article_table}.published_at <= '{dttm_to_2}'"

        sql_kw = text(
            f"""
            SELECT id, title, body, source_name, published_at
            FROM {article_table}
            {where_sql_keyword}
            AND (title ILIKE :pattern OR body ILIKE :pattern)
            ORDER BY published_at DESC
            LIMIT :top_k
            """
        )
        pattern = f"%{query}%"

        if hasattr(db, "execute"):
            result = await db.execute(sql_kw, {"pattern": pattern, "top_k": top_k})
            rows = result.mappings().fetchall()
        else:
            mapped = db.compile(sql_kw, compile_kwargs={"literal_binds": True})
            mapped_str = str(mapped)
            mapped_str = mapped_str.replace(":pattern", f"'{pattern}'").replace(":top_k", str(top_k))
            rows = await db.fetch(mapped_str)

        articles = []
        for row in rows:
            if hasattr(row, "_mapping"):
                row = dict(row._mapping)
            title = row.get("title") or ""
            body = row.get("body") or ""
            source = row.get("source_name") or "Unknown"
            text_content = (
                f"# {title}\n\nSource: {source}\n\n{body[:800]}"
                if body
                else f"# {title}"
            )
            pub_date = row.get("published_at")
            date_str = pub_date.strftime("%Y-%m-%d") if pub_date else ""

            articles.append({
                "text": text_content[:1500],
                "title": title,
                "source_name": source,
                "distance": 0,
                "published_at": date_str,
            })

        return articles

    except Exception as exc:
        logger.warning("Keyword search failed for query %s: %s", query, exc)
        return []
