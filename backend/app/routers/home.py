"""Home page router — upcoming games across all sports."""

import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(tags=["home"])

DECIMAL_FIELDS = [
    "spread", "over_under", "home_moneyline", "away_moneyline",
    "opening_spread", "opening_total",
    "opening_home_moneyline", "opening_away_moneyline",
    "predicted_margin",
]


def _fix_decimals(row: dict) -> dict:
    """Cast Decimal values to Python floats for JSON serialization."""
    for field in DECIMAL_FIELDS:
        val = row.get(field)
        if val is not None:
            try:
                row[field] = float(val)
            except (TypeError, ValueError, OverflowError):
                row[field] = None
    return row


@router.get("/home/upcoming-games")
async def upcoming_games(db: AsyncSession = Depends(get_db)):
    """Return the next 6 scheduled games across MLB, NBA, and NFL, sorted by date ascending."""
    now = datetime.now(timezone.utc)
    results = []

    # ── MLB ──
    sql_mlb = """
    SELECT
        'mlb' AS sport,
        g.id,
        g.mlb_game_id AS external_id,
        g.date,
        g.status::text AS status,
        ht.abbreviation AS home_team_name,
        at.abbreviation AS away_team_name,
        g.home_score,
        g.away_score,
        g.home_pitcher_name,
        g.away_pitcher_name,
        g.venue,
        c.closing_spread AS spread,
        c.closing_ou AS over_under,
        c.closing_home_ml AS home_moneyline,
        c.closing_away_ml AS away_moneyline,
        c.opening_spread,
        c.opening_ou AS opening_total,
        c.opening_home_ml AS opening_home_moneyline,
        c.opening_away_ml AS opening_away_moneyline,
        gp.predicted_margin,
        gp.run_line_result AS pred_rl_result,
        gp.ml_result AS pred_ml_result,
        gp.ou_result AS pred_ou_result,
        gp.run_line_pick AS pred_rl_pick
    FROM mlb.games g
    JOIN mlb.teams ht ON ht.id = g.home_team_id
    JOIN mlb.teams at ON at.id = g.away_team_id
    JOIN mlb.seasons s ON s.id = g.season_id
    LEFT JOIN mlb.betting_lines_consolidated c ON c.game_id = g.id
    LEFT JOIN mlb.game_predictions gp ON gp.game_id = g.id
    WHERE g.status::text = 'SCHEDULED'
      AND g.date > :now
    ORDER BY g.date ASC
    LIMIT 12
    """
    rows = (await db.execute(text(sql_mlb), {"now": now})).mappings().all()
    results.extend(_fix_decimals(dict(r)) for r in rows)

    # ── NBA ──
    sql_nba = """
    SELECT
        'nba' AS sport,
        g.id,
        g.nba_game_id AS external_id,
        g.date,
        g.status::text AS status,
        ht.abbreviation AS home_team_name,
        at.abbreviation AS away_team_name,
        g.home_score,
        g.away_score,
        NULL AS home_pitcher_name,
        NULL AS away_pitcher_name,
        g.venue,
        blc.closing_spread AS spread,
        blc.closing_ou AS over_under,
        blc.closing_home_ml AS home_moneyline,
        blc.closing_away_ml AS away_moneyline,
        blc.opening_spread,
        blc.opening_ou AS opening_total,
        blc.opening_home_ml AS opening_home_moneyline,
        blc.opening_away_ml AS opening_away_moneyline,
        gp.predicted_margin,
        gp.ats_result AS pred_rl_result,
        gp.ml_result AS pred_ml_result,
        gp.ou_result AS pred_ou_result,
        gp.spread_pick AS pred_rl_pick
    FROM nba.games g
    JOIN nba.teams ht ON ht.id = g.home_team_id
    JOIN nba.teams at ON at.id = g.away_team_id
    JOIN nba.seasons s ON s.id = g.season_id
    LEFT JOIN nba.betting_lines_consolidated blc ON blc.game_id = g.id
    LEFT JOIN nba.game_predictions gp ON gp.game_id = g.id
    WHERE g.status::text = 'SCHEDULED'
      AND g.date > :now
    ORDER BY g.date ASC
    LIMIT 12
    """
    rows = (await db.execute(text(sql_nba), {"now": now})).mappings().all()
    results.extend(_fix_decimals(dict(r)) for r in rows)

    # ── NFL ──
    sql_nfl = """
    SELECT
        'nfl' AS sport,
        g.id,
        NULL AS external_id,
        g.date,
        g.status::text AS status,
        ht.abbreviation AS home_team_name,
        at.abbreviation AS away_team_name,
        g.home_score,
        g.away_score,
        NULL AS home_pitcher_name,
        NULL AS away_pitcher_name,
        g.venue,
        blc.closing_spread AS spread,
        blc.closing_ou AS over_under,
        NULL AS home_moneyline,
        NULL AS away_moneyline,
        NULL AS opening_spread,
        NULL AS opening_total,
        NULL AS opening_home_moneyline,
        NULL AS opening_away_moneyline,
        gp.predicted_margin,
        gp.ats_result AS pred_rl_result,
        gp.ml_result AS pred_ml_result,
        gp.ou_result AS pred_ou_result,
        gp.spread_pick AS pred_rl_pick
    FROM nfl.games g
    JOIN nfl.teams ht ON ht.id = g.home_team_id
    JOIN nfl.teams at ON at.id = g.away_team_id
    JOIN nfl.seasons s ON s.id = g.season_id
    LEFT JOIN nfl.betting_lines_consolidated blc ON blc.game_id = g.id
    LEFT JOIN nfl.game_predictions gp ON gp.game_id = g.id
    WHERE g.status::text = 'SCHEDULED'
      AND g.date > :now
    ORDER BY g.date ASC
    LIMIT 12
    """
    rows = (await db.execute(text(sql_nfl), {"now": now})).mappings().all()
    results.extend(_fix_decimals(dict(r)) for r in rows)

    # Sort all by date and take the next 6
    results.sort(key=lambda g: g["date"])
    return results[:6]
