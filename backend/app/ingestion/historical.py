"""
Historical NFL knowledge generator.
Creates season recaps from nflverse stats + Wikipedia summaries for 2005-2025.
"""
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article
from app.ingestion.articles import _slugify, _try_insert_article

logger = logging.getLogger("earl.historical")

# Map season_id → calendar year (populated from nflverse + ESPN data)
YEAR_TO_SEASON_ID = {
    2005: 10, 2006: 11, 2007: 12, 2008: 13, 2009: 14,
    2010: 15, 2011: 16, 2012: 17, 2013: 18, 2014: 19,
    2015: 20, 2016: 21, 2017: 22, 2018: 23, 2019: 24,
    2020: 25, 2021: 26, 2022: 28, 2023: 29, 2024: 9, 2025: 3,
}


async def _get_stat_leaders(db: AsyncSession, season_id: int, stat_col: str, position_filter: str = "",
                            limit: int = 5) -> list[dict]:
    """Generic stat leader query. Returns list of {name, value} dicts."""
    pos_where = f"AND p.position IN ({position_filter})" if position_filter else ""
    rows = await db.execute(text(f"""
        SELECT p.name, SUM(ps.{stat_col}) as val
        FROM player_weekly_stats ps
        JOIN players p ON ps.player_id = p.id
        WHERE ps.season_id = :sid {pos_where}
        GROUP BY p.id, p.name
        ORDER BY val DESC
        LIMIT :lim
    """), {"sid": season_id, "lim": limit})
    return [{"name": r.name, "value": int(r.val)} for r in rows]


async def _scrape_wikipedia_season(year: int) -> Optional[str]:
    """Fetch Wikipedia season summary for a given NFL season year."""
    url = f"https://en.wikipedia.org/wiki/{year}_NFL_season"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={
                "User-Agent": "EarlKnowsBall/1.0 (research; rich@ljart.com)"
            })
            if resp.status_code != 200:
                return None

            import html as html_mod
            paragraphs = []
            for m in re.finditer(r'<p>(.*?)</p>', resp.text, re.DOTALL):
                text = re.sub(r'<[^>]+>', '', m.group(1))
                text = html_mod.unescape(text).strip()
                if len(text) > 80 and not text.startswith('<'):
                    paragraphs.append(text)
                    if len(paragraphs) >= 4:
                        break
            return "\n\n".join(paragraphs) if paragraphs else None
    except Exception as e:
        logger.warning(f"Wikipedia scrape failed for {year}: {e}")
        return None


async def generate_season_recap(
    db: AsyncSession,
    season_id: int,
    year: int,
) -> Optional[int]:
    """Generate and store a season recap article from stats + Wikipedia."""
    wiki = await _scrape_wikipedia_season(year)

    passers = await _get_stat_leaders(db, season_id, "pass_yards", "'QB'")
    pass_tds = await _get_stat_leaders(db, season_id, "pass_tds", "'QB'")
    rushers = await _get_stat_leaders(db, season_id, "rush_yards", "'RB','FB','QB'")
    rush_tds = await _get_stat_leaders(db, season_id, "rush_tds", "'RB','FB'")
    receivers = await _get_stat_leaders(db, season_id, "receiving_yards", "'WR','TE'")
    recv_tds = await _get_stat_leaders(db, season_id, "receiving_tds", "'WR','TE'")

    parts = [f"# {year} NFL Season Recap\n"]

    if wiki:
        parts.append(wiki)
        parts.append("")

    parts.append("## Statistical Leaders\n")

    if passers:
        parts.append("### Passing Yards")
        for p in passers:
            parts.append(f"- {p['name']}: {p['value']:,} yards")
    if pass_tds:
        parts.append("\n### Passing Touchdowns")
        for p in pass_tds:
            parts.append(f"- {p['name']}: {p['value']} TDs")

    if rushers:
        parts.append("\n### Rushing Yards")
        for r in rushers:
            parts.append(f"- {r['name']}: {r['value']:,} yards")
    if rush_tds:
        parts.append("\n### Rushing Touchdowns")
        for r in rush_tds:
            parts.append(f"- {r['name']}: {r['value']} TDs")

    if receivers:
        parts.append("\n### Receiving Yards")
        for r in receivers:
            parts.append(f"- {r['name']}: {r['value']:,} yards")
    if recv_tds:
        parts.append("\n### Receiving Touchdowns")
        for r in recv_tds:
            parts.append(f"- {r['name']}: {r['value']} TDs")

    body = "\n".join(parts)

    title = f"{year} NFL Season Recap"
    title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
    slug = f"{_slugify(title)}-{title_hash}"

    article = Article(
        title=title,
        slug=slug,
        body=body,
        excerpt=f"Comprehensive recap of the {year} NFL season including statistical leaders and key storylines.",
        category="general",
        tier="free",
        published=True,
        published_at=datetime.now(timezone.utc),
        author="Earl Knows Ball",
        source_name="Earl Knows Ball",
        source_type="generated",
    )

    inserted = await _try_insert_article(db, article)
    if not inserted:
        logger.info(f"Season {year} already exists, skipping")
        return None

    await db.commit()
    logger.info(f"Generated {year} season recap (article #{article.id})")
    return article.id


async def generate_all_seasons(
    db: AsyncSession,
    start_year: int = 2005,
    end_year: int = 2025,
) -> dict:
    """Generate recaps for all seasons in range."""
    results = {"generated": 0, "skipped": 0, "errors": 0, "years": []}

    for year in range(start_year, end_year + 1):
        season_id = YEAR_TO_SEASON_ID.get(year)
        if not season_id:
            logger.warning(f"No season_id mapping for {year}")
            results["skipped"] += 1
            continue

        try:
            aid = await generate_season_recap(db, season_id, year)
            if aid:
                results["generated"] += 1
                results["years"].append(year)
            else:
                results["skipped"] += 1
        except Exception as e:
            logger.error(f"Error generating {year}: {e}")
            results["errors"] += 1
            await db.rollback()

    return results
