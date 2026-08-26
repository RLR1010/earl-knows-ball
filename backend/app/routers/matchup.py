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

# Lazy-import the sport chat-tool modules so we don't pay import cost unless
# that sport is hit, and so a single sport's import errors don't break the app.
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
    sport = (sport or "").strip().lower()
    if sport not in _VALID:
        raise HTTPException(400, f"Invalid sport '{sport}'. Choose one of: {', '.join(_VALID)}.")

    if game_id is None and home is None and away is None:
        raise HTTPException(400, "Provide a game_id, or both home and away team names.")

    mod = _tools(sport)

    # Resolve team names: prefer game_id, else require home+away.
    if game_id is not None:
        g = (await db.execute(
            text(f"SELECT home_team_id, away_team_id, date FROM {sport}.games WHERE id = :gid"),
            {"gid": game_id},
        )).mappings().first()
        if not g:
            raise HTTPException(404, f"No {sport} game with id {game_id}.")
        names = {}
        for side, tid in (("home", g["home_team_id"]), ("away", g["away_team_id"])):
            t = (await db.execute(
                text(f"SELECT name, abbreviation FROM {sport}.teams WHERE id = :tid"),
                {"tid": tid},
            )).mappings().first()
            names[side] = {"id": tid, "name": t["name"] if t else str(tid), "abbr": t["abbreviation"] if t else str(tid)}
        home_name = names["home"]["name"]
        away_name = names["away"]["name"]
        game_date = str(g["date"])[:10]
    else:
        if not home or not away:
            raise HTTPException(400, "Provide both home and away team names/abbrs (or a game_id).")
        home_name, away_name = home, away
        game_date = None

    # 1) Trends for each team (recent form, ATS, O/U). Pass the abbreviation
    # (unique) rather than the full name, since _resolve_team_id raises
    # MultipleResultsFound on ambiguous full names.
    trends_home = await mod._get_team_trends(db, {"team_name": names["home"]["abbr"] if game_id is not None else home})
    trends_away = await mod._get_team_trends(db, {"team_name": names["away"]["abbr"] if game_id is not None else away})

    # 2) Split stats (home/road, F1/full) if available.
    split_home = None
    split_away = None
    if hasattr(mod, "_get_team_split_stats"):
        try:
            split_home = await mod._get_team_split_stats(db, {"team_name": names["home"]["abbr"] if game_id is not None else home})
        except Exception:
            split_home = None
        try:
            split_away = await mod._get_team_split_stats(db, {"team_name": names["away"]["abbr"] if game_id is not None else away})
        except Exception:
            split_away = None

    # 3) Side-by-side comparison.
    comparison = await mod._get_team_comparison(
        db,
        {
            "team_a": names["home"]["abbr"] if game_id is not None else home,
            "team_b": names["away"]["abbr"] if game_id is not None else away,
        },
    )

    teams = {
        "home": {
            "name": home_name,
            "id": names["home"]["id"] if game_id is not None else None,
            "abbr": names["home"]["abbr"] if game_id is not None else (home or home_name),
            "trends": trends_home if not isinstance(trends_home, dict) or "error" not in trends_home else None,
            "trends_error": trends_home.get("error") if isinstance(trends_home, dict) and "error" in trends_home else None,
            "splits": split_home if isinstance(split_home, dict) and "error" not in split_home else None,
        },
        "away": {
            "name": away_name,
            "id": names["away"]["id"] if game_id is not None else None,
            "abbr": names["away"]["abbr"] if game_id is not None else (away or away_name),
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
