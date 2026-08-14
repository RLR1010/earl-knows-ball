"""Home page router — upcoming games across all sports (or a single sport)."""

import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter(tags=["home"])

DECIMAL_FIELDS = [
    "spread", "over_under", "home_moneyline", "away_moneyline",
    "opening_spread", "opening_total",
    "opening_home_moneyline", "opening_away_moneyline",
    "predicted_margin",
    "pick_ats_ev", "pick_ou_ev", "pick_ml_ev",
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


# Shared SELECT column names so all three sports return the SAME shape the
# schedule-page shared card (ScheduleGameCard) expects: home_team/away_team are
# abbreviations, and the rich pick_* / result_* fields drive the picks panel.
def _pick_aliases(kind: str):
    """Return SQL alias fragments mapping each sport's prediction columns to the
    shared pick_* / result_* shape."""
    if kind == "mlb":
        return {
            "pick_spread": "gp.run_line_pick AS pick_spread",
            "pick_over_under": "gp.ou_pick AS pick_over_under",
            "pick_moneyline": "gp.ml_pick AS pick_moneyline",
            "pick_ats_ev": "gp.ats_ev AS pick_ats_ev",
            "pick_ou_ev": "gp.ou_ev AS pick_ou_ev",
            "pick_ml_ev": "gp.ml_ev AS pick_ml_ev",
            "result_spread": "gp.run_line_result AS result_spread",
            "result_over_under": "gp.ou_result AS result_over_under",
            "result_moneyline": "gp.ml_result AS result_moneyline",
        }
    if kind == "nba":
        # NBA resolves numeric team-id picks to abbreviations to match nba_stats.py
        return {
            "pick_spread": "CASE WHEN gp.spread_pick IS NOT NULL "
            "THEN (CASE WHEN psp.name = ht.name THEN ht.abbreviation "
            "WHEN psp.name = at.name THEN at.abbreviation "
            "ELSE COALESCE(psp.abbreviation, gp.spread_pick) END) "
            "ELSE NULL END AS pick_spread",
            "pick_over_under": "gp.ou_pick AS pick_over_under",
            "pick_moneyline": "CASE WHEN gp.ml_pick IS NOT NULL "
            "THEN (CASE WHEN pml.name = ht.name THEN ht.abbreviation "
            "WHEN pml.name = at.name THEN at.abbreviation "
            "ELSE COALESCE(pml.abbreviation, gp.ml_pick) END) "
            "ELSE NULL END AS pick_moneyline",
            "pick_ats_ev": "gp.ats_ev AS pick_ats_ev",
            "pick_ou_ev": "gp.ou_ev AS pick_ou_ev",
            "pick_ml_ev": "gp.ml_ev AS pick_ml_ev",
            "result_spread": "gp.ats_result AS result_spread",
            "result_over_under": "gp.ou_result AS result_over_under",
            "result_moneyline": "gp.ml_result AS result_moneyline",
        }
    # nfl uses raw spread_pick / ats_result (matches games.py)
    return {
        "pick_spread": "gp.spread_pick AS pick_spread",
        "pick_over_under": "gp.ou_pick AS pick_over_under",
        "pick_moneyline": "gp.ml_pick AS pick_moneyline",
        "pick_ats_ev": "gp.ats_ev AS pick_ats_ev",
        "pick_ou_ev": "gp.ou_ev AS pick_ou_ev",
        "pick_ml_ev": "gp.ml_ev AS pick_ml_ev",
        "result_spread": "gp.ats_result AS result_spread",
        "result_over_under": "gp.ou_result AS result_over_under",
        "result_moneyline": "gp.ml_result AS result_moneyline",
    }


def _build_sql(schema: str, kind: str):
    """Build the per-sport SELECT. Shared columns are identical so downstream
    consumers (site home + sport home pages) get one uniform shape."""
    p = _pick_aliases(kind)
    pitcher_cols = (
        "g.home_pitcher_name AS home_pitcher_name,\n"
        "        g.away_pitcher_name AS away_pitcher_name,"
        if kind == "mlb"
        else "NULL AS home_pitcher_name,\n        NULL AS away_pitcher_name,"
    )
    # external_id: only MLB/NBA have a *_game_id column on the games table.
    external_id = (
        f"g.{kind}_game_id AS external_id"
        if kind in ("mlb", "nba")
        else "NULL AS external_id"
    )
    joins = (
        f"LEFT JOIN {schema}.teams psp ON psp.id = CASE WHEN gp.spread_pick ~ '^[0-9]+$' "
        f"THEN gp.spread_pick::bigint END\n"
        f"    LEFT JOIN {schema}.teams pml ON pml.id = CASE WHEN gp.ml_pick ~ '^[0-9]+$' "
        f"THEN gp.ml_pick::bigint END"
        if kind == "nba"
        else ""
    )
    return f"""
    SELECT
        '{kind}'::text AS sport,
        g.id,
        {external_id},
        g.date,
        g.status::text AS status,
        ht.abbreviation AS home_team,
        at.abbreviation AS away_team,
        g.home_score,
        g.away_score,
        {pitcher_cols}
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
        {p['pick_spread']},
        {p['pick_over_under']},
        {p['pick_moneyline']},
        {p['pick_ats_ev']},
        {p['pick_ou_ev']},
        {p['pick_ml_ev']},
        {p['result_spread']},
        {p['result_over_under']},
        {p['result_moneyline']}
    FROM {schema}.games g
    JOIN {schema}.teams ht ON ht.id = g.home_team_id
    JOIN {schema}.teams at ON at.id = g.away_team_id
    LEFT JOIN {schema}.betting_lines_consolidated c ON c.game_id = g.id
    LEFT JOIN {schema}.game_predictions gp ON gp.game_id = g.id
    {joins}
    WHERE g.status::text = 'SCHEDULED'
      AND g.date > :now
      AND g.date <= :horizon
    ORDER BY g.date ASC
    LIMIT :limit
    """


@router.get("/home/upcoming-games")
async def upcoming_games(
    sport: str = Query("all", description="Filter by sport: all, mlb, nba, nfl"),
    days: int = Query(5, description="Only show games within this many days from now"),
    db: AsyncSession = Depends(get_db),
):
    """Return upcoming scheduled games, sorted by date ascending.

    - sport=all (default): up to 6 games PER SPORT, for every sport that has
      games within the next `days` days (site home). So the home page can show
      e.g. 6 MLB + 6 NFL + 6 NBA side by side.
    - sport=mlb|nba|nfl: up to 6 games from that sport only (sport home pages).
    """
    sport = (sport or "all").lower()
    if sport not in ("all", "mlb", "nba", "nfl"):
        sport = "all"
    days = max(1, min(days, 14))

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    results = []

    specs = []
    if sport in ("all", "mlb"):
        specs.append(("mlb", "mlb"))
    if sport in ("all", "nba"):
        specs.append(("nba", "nba"))
    if sport in ("all", "nfl"):
        specs.append(("nfl", "nfl"))

    for schema, kind in specs:
        sql = _build_sql(schema, kind)
        rows = (
            await db.execute(
                text(sql), {"now": now, "horizon": horizon, "limit": 6}
            )
        ).mappings().all()
        results.extend(_fix_decimals(dict(r)) for r in rows)

    return results
