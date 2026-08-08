"""Public read-only endpoints for team props + player season props (futures).

Serves the data scraped by the BetMGM scraper (team_props + player_season_props)
to the Props page on each sport (NFL/NBA/MLB). Endpoints mount under the sport's
own path prefix:

    GET /props        -> NFL      (Caddy maps /api/props)
    GET /nba/props    -> NBA      (Caddy maps /api/nba/props)
    GET /mlb/props    -> MLB      (Caddy maps /api/mlb/props)

No auth required — the props are public display data.
"""

from fastapi import APIRouter
from sqlalchemy import text as _sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from fastapi import Depends

router = APIRouter(tags=["props"], responses={404: {"description": "Not found"}})


PROP_DISPLAY = {
    "championship": "Championship",
    "make_playoffs": "Make Playoffs",
    "miss_playoffs": "Miss Playoffs",
    "mvp": "MVP",
    "mvp_al": "AL MVP",
    "mvp_nl": "NL MVP",
    "cy_young": "Cy Young",
    "cy_young_al": "AL Cy Young",
    "cy_young_nl": "NL Cy Young",
    "rookie": "Rookie of the Year",
    "rookie_al": "AL Rookie of the Year",
    "rookie_nl": "NL Rookie of the Year",
    "rookie_of_year": "Rookie of the Year",
    "dpoy": "Defensive Player of the Year",
    "opoy": "Offensive Player of the Year",
    "coy": "Coach of the Year",
    "comeback_player": "Comeback Player of the Year",
    "sixth_man": "Sixth Man of the Year",
    "most_improved": "Most Improved Player",
}


async def _load_props(db: AsyncSession, sport: str) -> dict:
    team_rows = (
        await db.execute(_sa_text(f"""
            SELECT * FROM (
              SELECT DISTINCT ON (t.id)
                t.name AS team_name, t.abbreviation,
                p.win_total, p.win_total_over_odds, p.win_total_under_odds,
                p.championship_odds, p.make_playoffs_odds, p.miss_playoffs_odds
              FROM {sport}.team_props p
              JOIN {sport}.teams t ON t.id = p.team_id
              WHERE p.season_year = (SELECT max(season_year) FROM {sport}.team_props)
                AND (p.championship_odds IS NOT NULL
                     OR p.win_total IS NOT NULL
                     OR p.make_playoffs_odds IS NOT NULL)
              ORDER BY
                t.id,
                CASE WHEN lower(p.bookmaker)='betmgm' THEN 0
                     WHEN lower(p.bookmaker)='draftkings' THEN 1
                     WHEN lower(p.bookmaker)='fanduel' THEN 2
                     ELSE 3 END,
                p.scraped_at DESC
            ) teams
            -- Most likely to win first: ascending American odds puts the
            -- biggest favorite (most negative) at top; NULLs sort last.
            ORDER BY
              CASE WHEN championship_odds IS NULL THEN 1 ELSE 0 END,
              championship_odds ASC NULLS LAST,
              team_name ASC
        """))
    ).fetchall()

    player_rows = (
        await db.execute(_sa_text(f"""
            WITH dedup AS (
              SELECT DISTINCT ON (ps.player_name, ps.prop_type)
                ps.player_name, ps.prop_type, ps.odds, ps.implied_probability,
                t.name AS team_name, t.abbreviation
              FROM {sport}.player_season_props ps
              LEFT JOIN {sport}.teams t ON t.id = ps.team_id
              WHERE ps.season_year = (SELECT max(season_year) FROM {sport}.player_season_props)
              ORDER BY
                ps.player_name, ps.prop_type,
                CASE WHEN lower(ps.bookmaker)='betmgm' THEN 0
                     WHEN lower(ps.bookmaker)='draftkings' THEN 1
                     WHEN lower(ps.bookmaker)='fanduel' THEN 2
                     ELSE 3 END,
                ps.scraped_at DESC
            )
            SELECT * FROM dedup
            ORDER BY
              prop_type,
              COALESCE(implied_probability, 0) DESC,
              player_name
        """))
    ).fetchall()

    return {"team_props": [dict(r._mapping) for r in team_rows],
            "player_season_props": [dict(r._mapping) for r in player_rows]}


async def _props_response(db: AsyncSession, sport: str):
    data = await _load_props(db, sport)
    # Sanitize: convert any non-serializable types, drop empty bookmaker
    for row in data["player_season_props"]:
        row.pop("bookmaker", None)  # don't expose the book
    return {
        "sport": sport,
        "team_props": data["team_props"],
        "player_season_props": data["player_season_props"],
    }


@router.get("/props", name="props:nfl")
async def get_nfl_props(db: AsyncSession = Depends(get_db)):
    return await _props_response(db, "nfl")


@router.get("/nba/props", name="props:nba")
async def get_nba_props(db: AsyncSession = Depends(get_db)):
    return await _props_response(db, "nba")


@router.get("/mlb/props", name="props:mlb")
async def get_mlb_props(db: AsyncSession = Depends(get_db)):
    return await _props_response(db, "mlb")
