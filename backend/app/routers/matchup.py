"""Team matchup — bends #2 (TrendSquad trends) + #3 (side-by-side comparison)
into one user-facing endpoint. Reuses the same sport logic that already powers
the chat tools' get_team_trends / get_team_comparison / get_team_split_stats,
so the data shown in the matchup modal matches what Earl would say in chat.

Endpoint: GET /matchup?sport={nfl|nba|mlb}&game_id={id}  (user-facing, 8001)
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db

router = APIRouter(tags=["matchup"])

_VALID = ("nfl", "nba", "mlb")


def _tools(sport: str):
    if sport == "nfl":
        from ..chat_tools import nfl as m
    elif sport == "nba":
        from ..chat_tools import nba as m
    else:
        from ..chat_tools import mlb as m
    return m


@router.get("/matchup")
async def matchup(
    sport: str = Query(..., description="nfl | nba | mlb"),
    game_id: int | None = Query(None, description="Game id (resolves home/away teams)"),
    home: str | None = Query(None, description="Home team name or abbr (optional if game_id given)"),
    away: str | None = Query(None, description="Away team name or abbr (optional if game_id given)"),
    db: AsyncSession = Depends(get_db),
):
    if sport not in _VALID:
        raise HTTPException(status_code=400, detail=f"sport must be one of {_VALID}")

    mod = _tools(sport)

    # Resolve teams + game_date mostly via game_id when present.
    names = {"home": {}, "away": {}}
    game_date = None
    try:
        if game_id is not None:
            q = text(
                f"""
                SELECT g.id, g.date,
                       h.id AS home_id, h.name AS home_name, h.abbreviation AS home_abbr,
                       a.id AS away_id, a.name AS away_name, a.abbreviation AS away_abbr
                FROM {sport}.games g
                JOIN {sport}.teams h ON h.id = g.home_team_id
                JOIN {sport}.teams a ON a.id = g.away_team_id
                WHERE g.id = :gid
                """
            )
            row = (await db.execute(q, {"gid": game_id})).mappings().first()
            if row:
                game_date = row["date"]
                names["home"] = {"id": row["home_id"], "name": row["home_name"], "abbr": row["home_abbr"]}
                names["away"] = {"id": row["away_id"], "name": row["away_name"], "abbr": row["away_abbr"]}
    except Exception:
        pass  # fall back to name/abbr lookups below

    # 1) trends - reuse the same logic Earl's chat uses.
    trends_home = await mod._get_team_trends(db, {"team_name": home or names["home"].get("name")})
    trends_away = await mod._get_team_trends(db, {"team_name": away or names["away"].get("name")})

    # 2) splits.
    try:
        split_home = await mod._get_team_split_stats(db, {"team_name": home or names["home"].get("name")})
    except Exception:
        split_home = {"error": "splits unavailable"}
    try:
        split_away = await mod._get_team_split_stats(db, {"team_name": away or names["away"].get("name")})
    except Exception:
        split_away = {"error": "splits unavailable"}

    # 3) Side-by-side comparison.
    comparison = await mod._get_team_comparison(
        db,
        {
            "team_a": names["home"].get("abbr") if game_id is not None else home,
            "team_b": names["away"].get("abbr") if game_id is not None else away,
        },
    )

    home_name = (names["home"].get("name") or home or "Home").__str__()
    away_name = (names["away"].get("name") or away or "Away").__str__()

    teams = {
        "home": {
            "name": home_name,
            "id": names["home"].get("id") if game_id is not None else None,
            "abbr": names["home"].get("abbr") if game_id is not None else (home or home_name),
            "trends": trends_home if not isinstance(trends_home, dict) or "error" not in trends_home else None,
            "trends_error": trends_home.get("error") if isinstance(trends_home, dict) and "error" in trends_home else None,
            "splits": split_home if isinstance(split_home, dict) and "error" not in split_home else None,
        },
        "away": {
            "name": away_name,
            "id": names["away"].get("id") if game_id is not None else None,
            "abbr": names["away"].get("abbr") if game_id is not None else (away or away_name),
            "trends": trends_away if not isinstance(trends_away, dict) or "error" not in trends_away else None,
            "trends_error": trends_away.get("error") if isinstance(trends_away, dict) and "error" in trends_away else None,
            "splits": split_away if isinstance(split_away, dict) and "error" not in split_away else None,
        },
    }

    return {
        "sport": sport,
        "game_id": game_id,
        "game_date": game_date,
        "teams": teams,
        "comparison": comparison if isinstance(comparison, dict) and "error" not in comparison else None,
        "comparison_error": comparison.get("error") if isinstance(comparison, dict) and "error" in comparison else None,
    }
