"""
Public endpoints for team-specific news aggregation.
"""
import html
import json
import re
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, desc, or_, text
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone, timedelta

from app.database import get_db
from app.models import Article, Team
from app.models.nba import NBAArticle, NBATeam
from app.models.mlb import MLBArticle, MLBTeam

router = APIRouter(prefix="/api/articles", tags=["articles"])


# ── Model lookups ────────────────────────────────────────────────

def _article_model(sport: str):
    if sport == "nfl":
        return Article
    elif sport == "nba":
        return NBAArticle
    elif sport == "mlb":
        return MLBArticle
    raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")


def _team_model(sport: str):
    if sport == "nfl":
        return Team
    elif sport == "nba":
        return NBATeam
    elif sport == "mlb":
        return MLBTeam
    raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")


async def _get_team_source_names(sport: str, abbreviation: str) -> set[str]:
    """Get RSS feed source names specific to a team."""
    from app.ingestion.rss_feeds import get_all_feeds
    feeds = get_all_feeds(sport)
    return {
        f["name"]
        for f in feeds
        if f.get("team") and f["team"].upper() == abbreviation.upper()
    }


async def _get_team_search_names(sport: str, abbreviation: str, db: AsyncSession) -> list[str]:
    """
    Get team names (city + nickname) to use in article title search.
    Returns terms that should appear in article titles about this team.
    """
    TeamModel = _team_model(sport)
    abbr = abbreviation.upper()

    result = await db.execute(
        select(TeamModel.name).where(TeamModel.abbreviation == abbr)
    )
    team = result.scalar_one_or_none()
    if not team:
        return [abbr]

    # Parse: "Chicago Bears" → ["CHI", "Chicago", "Bears"]
    parts = team.split(" ", 1)
    terms = [abbr, team]  # abbreviation + full name
    if len(parts) == 2:
        city = parts[0]
        nickname = parts[1]
        # Skip generic city names that would cause false matches
        generic_cities = {"New", "Los", "Las", "San", "St.", "Saint"}
        if city not in generic_cities:
            terms.append(city)
        terms.append(nickname)
    else:
        terms.append(parts[0])

    return terms


# Common English words that collide with team abbreviations — skip these
def _build_team_title_regex(terms: list[str]) -> str:
    """
    Build a strict PostgreSQL regex for matching team in article titles.
    Uses word boundaries (\\m = start, \\M = end) to avoid substring matches.
    Escapes special regex chars in terms.
    """
    common_words = {"was"}  # abbreviations that are common English words
    escaped = []
    for term in terms:
        lower = term.lower()
        if len(term) <= 4 and lower in common_words:
            # Skip common words that would match too broadly
            continue
        if len(term) <= 3:
            # For short terms (abbreviations), require word boundary both sides
            escaped.append(r"\m" + term + r"\M")
        elif len(term) <= 6:
            # For medium terms (city names, short nicknames), require word start
            escaped.append(r"\m" + term)
        else:
            # For long terms (full team name), simple case-insensitive match
            escaped.append(re.escape(term))

    return "|".join(escaped)


# ── Team News Endpoint ────────────────────────────────────────────

@router.get("/team/{sport}/{abbreviation}")
async def get_team_news(
    sport: str,
    abbreviation: str,
    limit: int = Query(25, ge=1, le=100, description="Max articles to return"),
    days_back: int = Query(30, ge=1, le=365, description="How far back to look"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get recent news articles about a specific team.

    Strategy:
      1. Pull all articles from team-specific sources (SB Nation, FanSided, etc.)
      2. Pull articles from general sources where title strictly matches
         the team abbreviation (word-boundary), city name, or nickname.
      3. Deduplicate by slug, sort by date descending.
    """
    abbr = abbreviation.upper()
    Model = _article_model(sport)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Get team-specific source names
    team_sources = await _get_team_source_names(sport, abbr)

    from app.ingestion.rss_feeds import get_teams_for_sport
    valid_teams = get_teams_for_sport(sport)
    if abbr not in valid_teams:
        raise HTTPException(status_code=404, detail=f"Team '{abbr}' not found for sport '{sport}'")

    # Query 1: Articles from team-specific sources
    stmt = select(Model).where(
        Model.published_at >= cutoff,
        Model.source_name.in_(team_sources),
    ).order_by(desc(Model.published_at)).limit(limit * 2)

    result = await db.execute(stmt)
    team_articles = result.scalars().all()
    seen_slugs = {a.slug for a in team_articles}

    # Query 2: Articles from general sources that mention the team
    # Uses strict word-boundary regex to avoid false matches
    search_terms = await _get_team_search_names(sport, abbr, db)
    regex_pattern = _build_team_title_regex(search_terms)

    stmt_general = select(Model).where(
        Model.published_at >= cutoff,
        ~Model.source_name.in_(team_sources),
        Model.title.op("~*")(regex_pattern),
    ).order_by(desc(Model.published_at)).limit(limit)

    result = await db.execute(stmt_general)
    for a in result.scalars().all():
        if a.slug not in seen_slugs:
            # Clean any remaining HTML entities from title/excerpt
            team_articles.append(a)
            seen_slugs.add(a.slug)

    # Sort by published_at desc, limit
    team_articles.sort(key=lambda a: a.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    team_articles = team_articles[:limit]

    return {
        "sport": sport,
        "team": abbr,
        "total": len(team_articles),
        "articles": [
            {
                "id": a.id,
                "title": html.unescape(a.title) if a.title else a.title,
                "excerpt": html.unescape(a.excerpt) if a.excerpt else a.excerpt,
                "category": a.category,
                "source_name": a.source_name,
                "source_url": a.source_url,
                "author": a.author,
                "published_at": a.published_at.isoformat() if a.published_at else None,
            }
            for a in team_articles
        ],
    }


# ── Team Content Endpoint (writeups + original articles) ─────────

_SUPPORTED_CONTENT_SPORTS = {"mlb", "nfl", "nba"}


# Reused by team-content to produce the "beginning of the writeup" excerpt.
def _make_excerpt(content: str, length: int = 240) -> str:
    if not content:
        return ""
    text_ = re.sub(r"<[^>]+>", " ", str(content))
    text_ = html.unescape(text_)
    text_ = re.sub(r"\s+", " ", text_).strip()
    if len(text_) <= length:
        return text_
    return text_[:length].rstrip() + "…"


@router.get("/team-content/{sport}/{abbreviation}")
async def get_team_content(
    sport: str,
    abbreviation: str,
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    per_page: int = Query(20, ge=1, le=100, description="Items per page"),
    limit: int = Query(100, ge=1, le=500, description="Max writeups+articles to return (backward-compat overall cap)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get all public writeups for games a team is in, plus any original
    articles (`public.original_articles`) that list the team in their
    `teams` JSONB column. Returns a page-able combined list sorted by
    date desc (newest first).
    """
    if sport not in _SUPPORTED_CONTENT_SPORTS:
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")

    abbr = abbreviation.upper()

    # 1) Public writeups for games this team is home or away in
    writeups_sql = text(f"""
        SELECT
            w.game_id AS game_id,
            w.title AS title,
            w.slug AS slug,
            w.published_at AS published_at,
            w.public_content AS content,
            g.date AS game_date,
            ht.abbreviation AS home_abbr,
            at.abbreviation AS away_abbr
        FROM {sport}.game_writeups w
        JOIN {sport}.games g ON g.id = w.game_id
        JOIN {sport}.teams ht ON ht.id = g.home_team_id
        JOIN {sport}.teams at ON at.id = g.away_team_id
        WHERE (ht.abbreviation = :abbr OR at.abbreviation = :abbr)
          AND w.status = 'published'
          AND w.published_at IS NOT NULL
        ORDER BY g.date DESC
    """)
    result = await db.execute(writeups_sql, {"abbr": abbr})
    writeup_rows = result.fetchall()

    writeups = []
    for r in writeup_rows:
        writeups.append({
            "type": "writeup",
            "game_id": r.game_id,
            "title": html.unescape(r.title) if r.title else r.title,
            "slug": r.slug,
            "summary": _make_excerpt(r.content),
            "link": f"/{sport}/articles/previews/{r.slug or r.game_id}",
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "game_date": r.game_date.isoformat() if r.game_date else None,
            "home_abbr": r.home_abbr,
            "away_abbr": r.away_abbr,
            "matchup": f"{r.away_abbr} @ {r.home_abbr}" if r.away_abbr and r.home_abbr else None,
            "author": "Earl",
        })

    # 2) Original articles in public.original_articles that list this team
    articles_sql = text("""
        SELECT
            id, title, summary, slug, author, teams, published_at
        FROM public.original_articles
        WHERE sport = :sport
          AND status = 'published'
          AND published_at IS NOT NULL
          AND teams @> CAST(:abbr_json AS jsonb)
        ORDER BY published_at DESC
    """)
    result = await db.execute(articles_sql, {
        "sport": sport,
        "abbr_json": json.dumps([abbr]),
    })
    article_rows = result.fetchall()

    articles = []
    for r in article_rows:
        articles.append({
            "type": "article",
            "id": r.id,
            "title": html.unescape(r.title) if r.title else r.title,
            "summary": html.unescape(r.summary) if r.summary else r.summary,
            "slug": r.slug,
            "link": f"/{sport}/articles/{r.slug or r.id}",
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "author": r.author or "Earl",
            "teams": r.teams if isinstance(r.teams, list) else [],
        })

    combined = writeups + articles
    combined.sort(
        key=lambda x: x.get("game_date") or x.get("published_at") or "",
        reverse=True,
    )
    combined.sort(
        key=lambda x: x.get("game_date") or x.get("published_at") or "",
        reverse=True,
    )

    total = len(combined)
    combined = combined[:limit]
    total_capped = len(combined)

    pages = max(1, (total_capped + per_page - 1) // per_page)
    page = min(page, pages)
    offset = (page - 1) * per_page
    page_items = combined[offset:offset + per_page]

    return {
        "sport": sport,
        "team": abbr,
        "total": total_capped,
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "writeups": writeups,
        "articles": articles,
        "items": page_items,
    }
