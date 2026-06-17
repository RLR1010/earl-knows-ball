"""
Public endpoints for team-specific news aggregation.
"""
import html
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
