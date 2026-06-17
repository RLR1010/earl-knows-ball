"""
Extract opening betting lines from SB Nation article bodies.

Patterns we handle:
  "the [Team] [are/is] [±X.X]-point [favorite/underdog]"
  "the opening betting line at [Team] [±X.X] points"
  "as a [±X.X]-point [favorite/underdog]"
  "total is set at [X.X] points"
  "[Team] [±X.X] (-/+)"
  "Spread: [Team] [±X.X]"
  "Moneyline: [Team] [-/+]XXX | [Team] [-/+]XXX"
  "Over/Under: [X.X]"
"""
import re
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, Game, Team, Season, BettingLine

logger = logging.getLogger("earl.article_opening_lines")

# Team name → abbreviation mapping (SB Nation uses full names/cities)
TEAM_MAP = {
    "cardinals": "ARI", "arizona cardinals": "ARI",
    "falcons": "ATL", "atlanta falcons": "ATL",
    "ravens": "BAL", "baltimore ravens": "BAL",
    "bills": "BUF", "buffalo bills": "BUF",
    "panthers": "CAR", "carolina panthers": "CAR",
    "bears": "CHI", "chicago bears": "CHI",
    "bengals": "CIN", "cincinnati bengals": "CIN",
    "browns": "CLE", "cleveland browns": "CLE",
    "cowboys": "DAL", "dallas cowboys": "DAL",
    "broncos": "DEN", "denver broncos": "DEN",
    "lions": "DET", "detroit lions": "DET",
    "packers": "GB", "green bay packers": "GB",
    "texans": "HOU", "houston texans": "HOU",
    "colts": "IND", "indianapolis colts": "IND",
    "jaguars": "JAX", "jacksonville jaguars": "JAX",
    "chiefs": "KC", "kansas city chiefs": "KC",
    "chargers": "LAC", "los angeles chargers": "LAC",
    "rams": "LAR", "los angeles rams": "LAR",
    "raiders": "LV", "las vegas raiders": "LV",
    "dolphins": "MIA", "miami dolphins": "MIA",
    "vikings": "MIN", "minnesota vikings": "MIN",
    "patriots": "NE", "new england patriots": "NE",
    "saints": "NO", "new orleans saints": "NO",
    "giants": "NYG", "new york giants": "NYG",
    "jets": "NYJ", "new york jets": "NYJ",
    "eagles": "PHI", "philadelphia eagles": "PHI",
    "steelers": "PIT", "pittsburgh steelers": "PIT",
    "49ers": "SF", "san francisco 49ers": "SF",
    "seahawks": "SEA", "seattle seahawks": "SEA",
    "buccaneers": "TB", "tampa bay buccaneers": "TB",
    "titans": "TEN", "tennessee titans": "TEN",
    "commanders": "WAS", "washington commanders": "WAS",
}

# Common article patterns
PAT_SPREAD_X_POINT = re.compile(
    r'(the\s+)?([A-Z][a-z]+(\s[A-Z][a-z]+)?)\s+(?:are|is)\s+(a\s+)?([+-]?\d+\.?\d*)-point\s+(favorite|underdog|favorites|underdogs)',
    re.IGNORECASE
)
PAT_SPREAD_LINE_AT = re.compile(
    r'opening betting line\s+(?:at\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+([+-]?\d+\.?\d*)\s+points?',
    re.IGNORECASE
)
PAT_SPREAD_AS_A = re.compile(
    r'as\s+(?:a\s+)?([+-]?\d+\.?\d*)-point\s+(favorite|underdog|favorites|underdogs)\s+(?:over|against|to)\s+(?:the\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)',
    re.IGNORECASE
)
PAT_SPREAD_COLON = re.compile(
    r'Spread:\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s*([+-]?\d+\.?\d*)',
    re.IGNORECASE
)
PAT_TOTAL = re.compile(
    r'total\s+(?:is set at|is|sits at)\s+(\d+\.?\d*)\s*points?',
    re.IGNORECASE
)
PAT_TOTAL_COLON = re.compile(
    r'Over/Under:\s*(\d+\.?\d*)',
    re.IGNORECASE
)
PAT_MONEYLINE = re.compile(
    r'Moneyline:\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+([+-]\d+)\s*[;|]\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+([+-]\d+)',
    re.IGNORECASE
)
# "Jaguars are 3.5-point favorites ... the total is set at 46.5"
PAT_BOTH = re.compile(
    r'(?:the\s+)?([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+(?:are|is)\s+(?:a\s+)?([+-]?\d+\.?\d*)-point\s+(favorite|underdog).*?total\s+(?:is set at|is)\s+(\d+\.?\d*)',
    re.IGNORECASE | re.DOTALL
)


def _team_to_abbr(name: str) -> Optional[str]:
    """Convert a team name/city to abbreviation."""
    n = name.strip().lower()
    # Direct lookup
    if n in TEAM_MAP:
        return TEAM_MAP[n]
    # Try splitting compound names
    parts = n.split()
    for p in parts:
        if p in TEAM_MAP:
            return TEAM_MAP[p]
    return None


def extract_lines_from_article(body: str, title: str) -> Optional[dict]:
    """Extract opening lines from article body. Returns dict or None."""
    if not body or len(body) < 50:
        return None

    result = {"spread": None, "spread_team": None, "over_under": None, "home_ml": None, "away_ml": None}

    # Pattern 1: "Bengals are 3.5-point favorites and the total is set at 49.5"
    m = PAT_BOTH.search(body)
    if m:
        team = _team_to_abbr(m.group(1))
        spread_str = m.group(2)
        is_fav = m.group(3).startswith("fav")
        total_str = m.group(4)
        if team and spread_str:
            spread = float(spread_str)
            # Favorite = negative spread (from favorite's perspective)
            if is_fav:
                result["spread"] = -abs(spread)
            else:
                result["spread"] = abs(spread)
            result["spread_team"] = team
        if total_str:
            result["over_under"] = float(total_str)
        return result if any(v is not None for v in [result["spread"], result["over_under"]]) else None

    # Pattern 2: "Spread:" colon format with team
    m = PAT_SPREAD_COLON.search(body)
    if m:
        team = _team_to_abbr(m.group(1))
        spread_str = m.group(2)
        if team and spread_str:
            result["spread"] = float(spread_str)
            result["spread_team"] = team

    # Pattern 3: "the [Team] are [X]-point favorite/underdog"
    if result["spread"] is None:
        m = PAT_SPREAD_X_POINT.search(body)
        if m:
            team = _team_to_abbr(m.group(2))
            spread_str = m.group(5)
            is_fav = m.group(6).startswith("fav")
            if team and spread_str:
                spread = float(spread_str)
                if is_fav:
                    result["spread"] = -abs(spread)
                else:
                    result["spread"] = abs(spread)
                result["spread_team"] = team

    # Pattern 4: "opening betting line at Team +X.X points"
    if result["spread"] is None:
        m = PAT_SPREAD_LINE_AT.search(body)
        if m:
            team = _team_to_abbr(m.group(1))
            spread_str = m.group(2)
            if team and spread_str:
                result["spread"] = float(spread_str)
                result["spread_team"] = team

    # Total patterns
    m = PAT_TOTAL.search(body)
    if m:
        result["over_under"] = float(m.group(1))
    else:
        m = PAT_TOTAL_COLON.search(body)
        if m:
            result["over_under"] = float(m.group(1))

    # Moneyline
    m = PAT_MONEYLINE.search(body)
    if m:
        home = _team_to_abbr(m.group(1))
        home_odds = int(m.group(2))
        away = _team_to_abbr(m.group(3))
        away_odds = int(m.group(4))
        if home and away:
            result["home_ml"] = home_odds
            result["away_ml"] = away_odds

    return result if any(v is not None for v in [result["spread"], result["over_under"], result["home_ml"]]) else None


async def extract_lines_from_articles(
    db: AsyncSession,
    year: int = 2025,
    dry_run: bool = True,
) -> dict:
    """Scan articles for opening lines and optionally save to betting_lines."""
    from datetime import datetime
    
    r = await db.execute(select(Season).where(Season.year == year))
    season = r.scalar_one_or_none()
    if not season:
        return {"error": f"Season {year} not found"}

    r = await db.execute(
        select(Article)
        .where(
            Article.published_at >= f"{year}-01-01",
            Article.published_at < f"{year+1}-01-01",
            (Article.title.ilike("%opening odds%") | Article.title.ilike("%opening line%")),
        )
        .order_by(Article.published_at)
    )
    articles = r.scalars().all()

    stats = {"total": len(articles), "parsed": 0, "with_spread": 0, "with_total": 0, "with_ml": 0, "saved": 0}
    results = []

    for article in articles:
        data = extract_lines_from_article(article.body, article.title)
        if data:
            stats["parsed"] += 1
            if data["spread"] is not None:
                stats["with_spread"] += 1
            if data["over_under"] is not None:
                stats["with_total"] += 1
            if data["home_ml"] is not None:
                stats["with_ml"] += 1

            results.append({
                "article_id": article.id,
                "title": article.title[:80],
                "published": str(article.published_at)[:10],
                "data": data,
            })

    stats["results"] = results
    return stats


async def run_extraction(db: AsyncSession, year: int = 2025):
    """Run extraction and print results."""
    stats = await extract_lines_from_articles(db, year=year, dry_run=True)
    print(f"\n{'='*60}")
    print(f"Article Opening Lines — {year} Season")
    print(f"{'='*60}")
    print(f"Total articles: {stats['total']}")
    print(f"Parsed: {stats['parsed']}")
    print(f"  With spread: {stats['with_spread']}")
    print(f"  With total:  {stats['with_total']}")
    print(f"  With ML:     {stats['with_ml']}")
    print()
    for r in stats.get("results", []):
        d = r["data"]
        parts = []
        if d.get("spread") and d.get("spread_team"):
            parts.append(f"spread: {d['spread_team']} {d['spread']:+.1f}")
        if d.get("over_under"):
            parts.append(f"O/U {d['over_under']}")
        if d.get("home_ml"):
            parts.append(f"ML +{d['home_ml']}/{d['away_ml']}")
        print(f"  {r['published']} | {r['title'][:60]:60s} | {', '.join(parts)}")

    return stats
