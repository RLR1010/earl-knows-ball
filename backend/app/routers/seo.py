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
SPORT_STATIC_ROUTES = ["", "schedule", "stats", "teams", "props", "results", "analysis", "articles", "power-rankings"]

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
        return {"sport": sport, "identifier": identifier, "title": None, "canonical_slug": None}
    ident = identifier.strip()
    is_digit = ident.isdigit()
    # Try slug first (stable SEO URL), then numeric id, then game_id. When a
    # non-digit (SEO slug) misses as a canonical slug, check for an old published
    # slug alias pointing at a canonical row so the caller can 301 to the live URL.
    cols = ("slug", "id", "game_id") if is_digit else ("slug",)
    # Redirects (canonical_slug) are ONLY reported when the requested identifier is
    # an OLD alias slug. Canonical slugs and numeric id/game_id lookups return
    # canonical_slug: null so existing deep links behave exactly as before.
    for col in cols:
        key: object = int(ident) if col != "slug" else ident
        row = await db.execute(text(f"""
            SELECT title, preview_image, premium_social_card
            FROM {sport}.game_writeups
            WHERE {col} = :ident
            ORDER BY id DESC LIMIT 1
        """), {"ident": key})
        r = row.mappings().first()
        if r and r["title"]:
            return {
                "sport": sport,
                "identifier": identifier,
                "title": r["title"],
                "canonical_slug": None,
                "preview_image": r["preview_image"],
                "premium_social_card": r["premium_social_card"],
            }
    # Alias fallback: the requested slug is an OLD published slug.
    if not is_digit:
        row = await db.execute(text(f"""
            SELECT gw.id, gw.title, gw.slug AS canonical_slug,
                   gw.preview_image, gw.premium_social_card
            FROM {sport}.game_writeup_slug_aliases a
            JOIN {sport}.game_writeups gw ON gw.id = a.game_writeup_id
            WHERE a.old_slug = :ident
            ORDER BY a.created_at DESC, gw.id DESC LIMIT 1
        """), {"ident": ident})
        r = row.mappings().first()
        if r and r["title"]:
            canonical_slug = r["canonical_slug"] or None
            return {
                "sport": sport,
                "identifier": identifier,
                "title": r["title"],
                "canonical_slug": canonical_slug,
                "preview_image": r["preview_image"],
                "premium_social_card": r["premium_social_card"],
            }
    return {
        "sport": sport,
        "identifier": identifier,
        "title": None,
        "canonical_slug": None,
        "preview_image": None,
        "premium_social_card": None,
    }


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
        #
        # IMPORTANT: only writeups an anonymous crawler can READ are emitted.
        # The analysis page fetches with `?tier=premium`; the backend returns 403
        # for paywalled writeups and the page renders a <PremiumGate> with no
        # content. Submitting those (a) wastes crawl budget, (b) produces
        # soft-404 / "crawled - currently not indexed" signals. As of 2026-09-14
        # a writeup becomes public once its game is historical (MLB/NBA: yesterday
        # or earlier; NFL: previous schedule week or earlier), so those are now
        # crawlable too — emit free-feature OR historical writeups.
        if sport == "nfl":
            # Historical = previous schedule week or earlier, compared by date against
            # the start of the current week (preseason weeks 30+ don't break ordering).
            # A season with no upcoming games is entirely historical.
            _hist = (
                "((SELECT MIN((g2.date AT TIME ZONE 'America/Chicago')::date) FROM nfl.games g2 "
                "WHERE g2.season_id = g.season_id "
                "AND (g2.date AT TIME ZONE 'America/Chicago')::date >= (now() AT TIME ZONE 'America/Chicago')::date) IS NULL "
                "OR (g.date AT TIME ZONE 'America/Chicago')::date < "
                "(SELECT MIN((g3.date AT TIME ZONE 'America/Chicago')::date) FROM nfl.games g3 "
                "WHERE g3.season_id = g.season_id "
                "AND (g3.date AT TIME ZONE 'America/Chicago')::date >= (now() AT TIME ZONE 'America/Chicago')::date))"
            )
        else:
            _hist = ("(g.date AT TIME ZONE 'America/Chicago')::date "
                     "< (now() AT TIME ZONE 'America/Chicago')::date")
        writeup_slugs = []
        try:
            rows = await db.execute(text(f"""
                SELECT COALESCE(NULLIF(w.slug, ''), CAST(w.id AS text)) AS ident
                FROM {sport}.game_writeups w
                JOIN {sport}.games g ON g.id = w.game_id
                WHERE w.status = 'published'
                  AND (w.is_free_feature = true OR {_hist})
                ORDER BY w.id DESC
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

        # Power rankings: weekly archives + per-team pages (only if published).
        # A missing table (sport not built yet) aborts the tx, so probe in a
        # try/except and roll back to keep the session usable for the next sport.
        pr_weeks = []
        try:
            pr_rows = await db.execute(text(f"""
                SELECT DISTINCT season, week FROM {sport}.power_ratings
                WHERE season >= 2022
                ORDER BY season DESC, week DESC
            """))
            pr_weeks = [{"season": r[0], "week": r[1]} for r in pr_rows.all()]
        except Exception:
            await db.rollback()
            pr_weeks = []

        result["sports"][sport] = {
            "static_routes": SPORT_STATIC_ROUTES,
            "teams": team_abbrs,
            "game_slugs": game_slugs,
            "writeup_slugs": writeup_slugs,
            "article_slugs": article_slugs,
            "power_ranking_weeks": pr_weeks,
            "power_ranking_teams": team_abbrs if pr_weeks else [],
        }

    return result


def _strip_repeat_of_title(title: str, text: str) -> str:
    """Trim a leading repetition of ``title`` from ``text`` (editorial summaries
    are frequently stored with the headline embedded in front, which would show
    the title twice in a feed item). Port of the same helper in original_articles."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    t = re.sub(r"\s+", " ", (title or "").strip()).rstrip(".!? ,;")
    if not t:
        return text
    head = text[: len(t) + 4]
    tw = [w.lower().strip(".!?,;:—\"'") for w in t.split() if w]
    if not tw:
        return text
    text_words = head.split()
    keep = len(tw)
    if len(text_words) >= keep and [
        w.lower().strip(".!?,;:—\"'") for w in text_words[:keep]
    ] == tw:
        return text[len(" ".join(text_words[:keep])) :].strip().lstrip(".!?,;:— ")
    return text


def _excerpt(md: str | None, limit: int = 300) -> str | None:
    """Plain-text excerpt from markdown-ish content, for feed descriptions."""
    if not md:
        return None
    txt = re.sub(r"```.*?```", " ", md, flags=re.S)
    txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", txt)
    txt = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", txt)
    txt = re.sub(r"[#*_>`~\-]+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0] + "…"
    return txt or None


@router.get("/feed-data")
async def feed_data(
    sport: str = "all",
    kind: str = "articles",
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Item data for the RSS feeds (original articles+recaps and/or previews).

    Returns site-relative `path` values; the frontend prefixes the origin.
    kind: "articles" | "previews" | "both".  sport: a sport code or "all".
    """
    try:
        lim = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        lim = 50
    kinds = ["articles", "previews"] if kind == "both" else [kind]
    sports = SPORTS if sport == "all" else [sport]
    items: list[dict] = []

    for sp in sports:
        if sp not in VALID_SPORTS:
            continue
        if "articles" in kinds:
            rows = await db.execute(text("""
                SELECT slug, title, summary, published_at, section,
                       preview_image, author
                FROM public.original_articles
                WHERE status = 'published' AND visibility = 'public'
                  AND sport = :sp AND slug IS NOT NULL AND slug <> ''
                ORDER BY published_at DESC NULLS LAST, id DESC
                LIMIT :lim
            """), {"sp": sp, "lim": lim})
            for r in rows.mappings().all():
                section = (r["section"] or "article").replace("_", " ").title()
                items.append({
                    "sport": sp,
                    "kind": "articles",
                    "title": r["title"],
                    "path": f"/{sp}/articles/{r['slug']}",
                    "summary": (
                        _strip_repeat_of_title(r["title"], r["summary"])
                        if r["summary"]
                        else None
                    ),
                    "published_at": r["published_at"].isoformat() if r["published_at"] else None,
                    "image": r["preview_image"],
                    "categories": [sp.upper(), section],
                    "author": r["author"] or "Earl",
                })
        if "previews" in kinds:
            try:
                rows = await db.execute(text(f"""
                    SELECT COALESCE(NULLIF(w.slug, ''), CAST(w.id AS text)) AS ident,
                           w.title, w.public_content, w.published_at, w.preview_image
                    FROM {sp}.game_writeups w
                    WHERE w.status = 'published'
                    ORDER BY w.published_at DESC NULLS LAST, w.id DESC
                    LIMIT :lim
                """), {"lim": lim})
                for r in rows.mappings().all():
                    items.append({
                        "sport": sp,
                        "kind": "previews",
                        "title": r["title"],
                        "path": f"/{sp}/articles/previews/{r['ident']}",
                        "summary": _excerpt(r["public_content"]),
                        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
                        "image": r["preview_image"],
                        "categories": [sp.upper(), "Preview"],
                        "author": "Earl",
                    })
            except Exception:
                # game_writeups may be absent on some schemas; skip quietly.
                pass

    # ISO-8601 sorts lexicographically; newest first.
    items.sort(key=lambda x: x["published_at"] or "", reverse=True)
    return {"sport": sport, "kind": kind, "items": items[:lim]}
