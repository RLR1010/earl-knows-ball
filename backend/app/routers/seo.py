"""SEO support endpoints — robots.txt guidance & sitemap data source.

Pure read endpoints that arm the frontend's /robots.txt and /sitemap.xml
(Next.js App Router routes) with the crawlable URL set. Served on the API
box (user-facing reads) since it shares Postgres with compute.
"""
import re
from datetime import date as _date

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.database import get_db

router = APIRouter(prefix="/seo", tags=["seo"])

# The static, always-crawlable page slugs. Ordered by importance. These are
# the fixed routes (marketing + sport hubs). Dynamic ones come from the DB.
STATIC_PAGES = [
    {"path": "",                         "priority": 1.0, "changefreq": "daily"},
    {"path": "faq",                      "priority": 0.6, "changefreq": "monthly"},
    {"path": "pricing",                  "priority": 0.7, "changefreq": "monthly"},
    {"path": "privacy",                  "priority": 0.3, "changefreq": "yearly"},
    {"path": "terms",                    "priority": 0.3, "changefreq": "yearly"},
    {"path": "support",                  "priority": 0.4, "changefreq": "monthly"},
]

# Sport hubs (per sport). Note: /players is intentionally NOT listed —
# player-stat pages are thin, client-rendered and blocked at robots.txt.
SPORT_STATIC_ROUTES = ["", "schedule", "stats", "teams", "props", "results", "analysis", "articles"]

SPORTS = ["nfl", "nba", "mlb"]

# How many current/upcoming games to include per sport in the sitemap.
# We deliberately include scheduled/final games within the current season
# (the "X vs Y prediction" pages that draw search traffic), not the full
# historical archive (tens of thousands of URLs — low value, blows the
# 50k sitemap cap).
GAMES_LIMIT = 500


VALID_SPORTS = set(SPORTS)


@router.get("/team-meta/{sport}/{abbr}")
async def team_meta(sport: str, abbr: str, db: AsyncSession = Depends(get_db)):
    """Full human-readable team name for a sport+abbreviation.

    Powers server-rendered `generateMetadata` on the team pages so the raw
    HTML carries a real title (e.g. "Chicago Bears — NFL Team") instead of a
    generic app title. Returns empty name if the abbreviation isn't found.
    """
    if sport not in VALID_SPORTS:
        return {"sport": sport, "abbreviation": abbr.upper(), "name": None}
    row = await db.execute(text(f"""
        SELECT name FROM {sport}.teams
        WHERE upper(abbreviation) = upper(:abbr)
        LIMIT 1
    """), {"abbr": abbr})
    name = row.scalar()
    return {"sport": sport, "abbreviation": abbr.upper(), "name": name}


def _slugify(name: str) -> str:
    """Turn a full team name into a URL-safe slug token.

    "Chicago Cubs" -> "chicago-cubs", "St. Louis Cardinals" -> "st-louis-
    cardinals", "New England Patriots" -> "new-england-patriots". Removes
    punctuation (periods, apostrophes), lowercases, collapses whitespace.
    """
    s = re.sub(r"[^\w\s-]", "", name)          # drop punctuation incl. dots
    s = s.strip().lower()
    s = re.sub(r"[\s-]+", "-", s)              # spaces/dashes -> single dash
    return s.strip("-")


def _game_slug(sport: str, home: str, away: str, game_date, game_id) -> str:
    """Canonical SEO slug: {home-full}-vs-{away-full}-{YYYY-MM-DD}-{id}.

    Example (MLB): chicago-cubs-vs-st-louis-cardinals-2026-08-26-49070. The
    trailing game id keeps the numeric identifier authoritative, so the URL is
    both readable and unambiguous. The DATE here is the actual game date.
    """
    date_str = str(game_date or "")[:10]   # YYYY-MM-DD
    return f"{_slugify(home)}-vs-{_slugify(away)}-{date_str}-{game_id}".lower()


@router.get("/game-meta/{sport}/{game_id}")
async def game_meta(sport: str, game_id: int, db: AsyncSession = Depends(get_db)):
    """Resolve home/away team names + game date for a pick-card / analysis URL.

    All sports keep `id`/`home_team_id`/`away_team_id`/`date` on {sport}.games
    and `id`/`name`/`abbreviation` on {sport}.teams, so one template works for
    all three. Returns the matchup with human-readable names (built from the
    full team names), the canonical SEO slug ({home}-vs-{away}-{date}-{id}),
    and a rich, human-quality description for the page <meta> tag.
    """
    if sport not in VALID_SPORTS:
        return {"sport": sport, "home": None, "away": None}
    row = await db.execute(text(f"""
        SELECT ht.name  AS home_name, ht.abbreviation AS home_abbr,
               at.name  AS away_name, at.abbreviation AS away_abbr,
               g.date   AS game_date,
               g.status AS status
        FROM {sport}.games g
        JOIN {sport}.teams ht ON ht.id = g.home_team_id
        JOIN {sport}.teams at ON at.id = g.away_team_id
        WHERE g.id = :gid
        LIMIT 1
    """), {"gid": game_id})
    r = row.mappings().first()
    if not r:
        return {"sport": sport, "home": None, "away": None}

    home_name, away_name = r["home_name"], r["away_name"]
    game_date = r["game_date"]
    slug = _game_slug(sport, home_name, away_name, game_date, game_id)

    # Human-readable date for the description (e.g. "August 26, 2026").
    description = None
    if game_date is not None:
        try:
            d = game_date if isinstance(game_date, _date) else game_date.date()
            date_label = d.strftime("%B %-d, %Y").replace(" 0", " ")
        except Exception:
            date_label = str(game_date)[:10]
        description = (
            f"{home_name} and {away_name} meet on {date_label}"
            f" in this {sport.upper()} matchup. Get Earl Knows Ball's"
            f" AI-powered prediction, projected odds, moneyline, ATS and"
            f" over/under analysis, plus the stats and trends that matter"
            f" before you bet."
        )
    else:
        description = (
            f"{home_name} vs {away_name} {sport.upper()} prediction from "
            f"Earl Knows Ball: AI-powered picks, projected odds, moneyline, "
            f"ATS and over/under analysis backed by stats and trends."
        )

    return {
        "sport": sport,
        "home": {"name": home_name, "abbr": r["home_abbr"]},
        "away": {"name": away_name, "abbr": r["away_abbr"]},
        "date": str(game_date) if game_date else None,
        "status": r["status"],
        "slug": slug,
        "description": description,
    }


@router.get("/writeup-meta/{sport}/{identifier}")
async def writeup_meta(sport: str, identifier: str, db: AsyncSession = Depends(get_db)):
    """Writeup title for the analysis page's server-rendered metadata.

    The analysis URL identifier can be the writeup's slug, numeric id, or
    game_id (the frontend links to `preview.slug || preview.writeup_id`).
    Resolve to the row's title so generateMetadata can emit a real
    \"<title>\" in raw HTML. Returns null title on miss (caller falls back).
    """
    if sport not in VALID_SPORTS:
        return {"sport": sport, "identifier": identifier, "title": None}
    ident = identifier.strip()
    is_digit = ident.isdigit()
    # Try slug first (stable SEO URL), then numeric id, then game_id.
    cols = ("slug", "id", "game_id") if is_digit else ("slug",)
    for col in cols:
        key: object = int(ident) if col != "slug" else ident
        row = await db.execute(text(f"""
            SELECT title FROM {sport}.game_writeups
            WHERE {col} = :ident
            ORDER BY id DESC LIMIT 1
        """), {"ident": key})
        r = row.mappings().first()
        if r and r["title"]:
            return {"sport": sport, "identifier": identifier, "title": r["title"]}
    return {"sport": sport, "identifier": identifier, "title": None}


@router.get("/sitemap-data")
async def sitemap_data(db: AsyncSession = Depends(get_db)):
    """Return the crawlable URL set as a flat JSON structure.

    The frontend sitemap.ts turns this into application/xml. Keeping the
    data-sourcing on the backend (over the shared DB) avoids putting DB
    credentials / SQLAlchemy into the Next.js server.
    """
    result = {
        "static": STATIC_PAGES,
        "sports": {},
    }

    for sport in SPORTS:
        # Team pages (one per franchise / abbreviation).
        teams = await db.execute(text(f"""
            SELECT abbreviation FROM {sport}.teams
            WHERE abbreviation IS NOT NULL AND abbreviation <> ''
            ORDER BY abbreviation
        """))
        team_abbrs = [r[0] for r in teams.all()]

        # Current / upcoming games worth indexing. Emit the canonical readable
        # slug URL ({home}-vs-{away}-{date}-{id}) so the sitemap points at the
        # exact canonical page for every game. Restrict to the 2022 season and
        # newer (year in {sport}.seasons = the season's start year: 2022 for
        # MLB, 2022-23 for NFL/NBA) so customers never see pre-2022 games.
        games = await db.execute(text(f"""
            SELECT g.id,
                   ht.name AS home_name,
                   at.name AS away_name,
                   (g.date AT TIME ZONE 'America/New_York')::date AS game_date,
                   g.date AS game_datetime
            FROM {sport}.games g
            JOIN {sport}.teams ht ON ht.id = g.home_team_id
            JOIN {sport}.teams at ON at.id = g.away_team_id
            JOIN {sport}.seasons s ON s.id = g.season_id
            WHERE s.year >= 2022
            ORDER BY g.date DESC
            LIMIT {GAMES_LIMIT}
        """))
        game_slugs = [
            _game_slug(sport, r[1], r[2], r[4], r[0])
            for r in games.all()
        ]

        # Published writeups → /{sport}/analysis/{slug}.
        # The frontend links to analysis pages via `preview.slug || preview.writeup_id`
        # (slug preferred), and the backend /{sport}/{identifier} resolves by slug,
        # so emitting slugs yields stable SEO URLs. Fall back to the writeup id
        # only when no slug exists.
        writeup_slugs = []
        try:
            rows = await db.execute(text(f"""
                SELECT COALESCE(NULLIF(slug, ''), CAST(id AS text)) AS ident
                FROM {sport}.game_writeups
                WHERE status = 'published'
                ORDER BY id DESC
                LIMIT {GAMES_LIMIT}
            """))
            writeup_slugs = [r[0] for r in rows.all()]
        except Exception:
            # Some schemas may not have game_writeups; gracefully skip.
            writeup_slugs = []

        # Published original articles → /{sport}/articles/{slug}.
        articles = await db.execute(text("""
            SELECT slug FROM public.original_articles
            WHERE status = 'published' AND visibility = 'public'
              AND sport = :sport AND slug IS NOT NULL AND slug <> ''
            ORDER BY id DESC
        """), {"sport": sport})
        article_slugs = [r[0] for r in articles.all()]

        result["sports"][sport] = {
            "static_routes": SPORT_STATIC_ROUTES,
            "teams": team_abbrs,
            "game_slugs": game_slugs,
            "writeup_slugs": writeup_slugs,
            "article_slugs": article_slugs,
        }

    return result
