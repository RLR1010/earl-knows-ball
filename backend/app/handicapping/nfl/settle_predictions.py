"""
Settle `nfl.game_predictions` results for FINAL games.

Mirror of MLB's `update_prediction_results` in `app/ingestion/boxscore_ingest.py`,
adapted for the NFL schema. NFL predictions (picks) are written by the live API
path (`batch_predict_upcoming_games`) *before* the game is played, so the result
columns (`ats_result`/`ou_result`/`ml_result`) stay NULL until a post-game settle
pass runs. This module does that settle: it recomputes each pick's result and
profit from the final score and the closing line.

Call sites:
  - `run_nfl_stats_refresh.py` after the weekly refresh so recently-finalized
    games get their picks colored on the schedule cards.
  - standalone: `venv/bin/python -m app.handicapping.nfl.settle_predictions [--all-years]`
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import asyncpg

from app.db_urls import PSYCOPG2_DATABASE_URL

logger = logging.getLogger(__name__)

# Result columns are only written once (NULL guard), so re-running is idempotent.
SELECT_ROWS_SQL = """
    SELECT gp.game_id,
           gp.predicted_margin, gp.predicted_total,
           gp.spread_pick, gp.ou_pick, gp.ml_pick,
           g.home_score, g.away_score,
           ht.abbreviation AS home_abbrev, at.abbreviation AS away_abbrev,
           blc.closing_spread,
           blc.closing_spread_home_odds, blc.closing_spread_away_odds,
           blc.closing_ou,
           blc.closing_over_odds, blc.closing_under_odds,
           blc.closing_home_ml, blc.closing_away_ml
    FROM nfl.game_predictions gp
    JOIN nfl.games g ON gp.game_id = g.id
    JOIN nfl.teams ht ON ht.id = g.home_team_id
    JOIN nfl.teams at ON at.id = g.away_team_id
    LEFT JOIN nfl.betting_lines_consolidated blc ON gp.game_id = blc.game_id
    WHERE g.status = 'FINAL'
      AND g.home_score IS NOT NULL
      AND g.away_score IS NOT NULL
      AND gp.ats_result IS NULL
    ORDER BY gp.game_id
"""

UPDATE_SQL = """
    UPDATE nfl.game_predictions
    SET actual_home_score = $1,
        actual_away_score = $2,
        actual_total      = $3,
        actual_margin     = $4,
        ats_result        = $5,
        ou_result         = $6,
        ml_result         = $7,
        ats_profit        = $8,
        ou_profit         = $9,
        ml_profit         = $10
    WHERE game_id = $11 AND ats_result IS NULL
"""


def _profit_per_100(odds: float) -> float:
    if odds > 0:
        return odds / 100.0
    return 100.0 / abs(odds)


def _calc_profit(result: Optional[str], odds: Optional[float]) -> Optional[float]:
    if result is None or odds is None:
        return None
    if result != "Win":
        return -100.0
    return round(100.0 * _profit_per_100(odds), 2)


async def settle_nfl_predictions(pg_conn: asyncpg.Connection) -> int:
    """Settle result + profit columns for FINAL NFL games lacking results."""
    rows = await pg_conn.fetch(SELECT_ROWS_SQL)
    if not rows:
        logger.info("  No NFL predictions need result updates")
        return 0

    updated = 0
    for r in rows:
        gid = r["game_id"]
        home_score = float(r["home_score"])
        away_score = float(r["away_score"])
        actual_total = home_score + away_score
        actual_margin = home_score - away_score

        home_a = (r["home_abbrev"] or "").strip().upper()
        away_a = (r["away_abbrev"] or "").strip().upper()
        spread_pick = (r["spread_pick"] or "").strip().upper() if r["spread_pick"] else ""
        ou_pick = (r["ou_pick"] or "").strip().lower() if r["ou_pick"] else ""
        ml_pick = (r["ml_pick"] or "").strip().upper() if r["ml_pick"] else ""

        spr = float(r["closing_spread"]) if r["closing_spread"] is not None else None

        # ---------- ATS result ----------
        # closing_spread is SIGNED for the HOME team (negative = home favored).
        # The side that covers ATS is the favorite when |margin| > |spread| on the
        # favorite side, otherwise the underdog. A pick is a:
        #   Win  -> the picked team covered
        #   Loss -> the picked team did not cover
        #   Push -> margin exactly equals the spread.
        ats_result = None
        if spr is not None and spread_pick and home_a and away_a:
            # Determine which side was picked (home/away).
            picked_home = spread_pick == home_a
            picked_away = spread_pick == away_a

            if abs(actual_margin + spr) < 0.005 and abs(spr) < 0.005:
                # Margin ~0 and line ~0 (pick'em tie) -> push
                ats_result = "Push"
            else:
                # closing_spread is SIGNED for the HOME team. Standard ATS model
                # (matches MLB): HOME covers when (margin + spr) > 0. The away/
                # underdog covers when home does not (margin + spr < 0). Exact
                # equality (|margin + spr| < 0.005) is a Push.
                home_covers = (actual_margin + spr) > 0.005
                away_covers = (actual_margin + spr) < -0.005
                if picked_home:
                    ats_result = "Win" if home_covers else ("Loss" if away_covers else "Push")
                elif picked_away:
                    ats_result = "Win" if away_covers else ("Loss" if home_covers else "Push")
                else:
                    # Pick doesn't match either team - leave unset.
                    ats_result = None

        # ---------- OU result ----------
        ou_result = None
        vegas_ou = float(r["closing_ou"]) if r["closing_ou"] is not None else None
        if vegas_ou is not None and ou_pick:
            ou_picked_over = ou_pick.startswith("over")
            ou_picked_under = ou_pick.startswith("under")
            if abs(actual_total - float(vegas_ou)) < 0.5:
                ou_result = "Push"
            elif ou_picked_over:
                ou_result = "Win" if actual_total > float(vegas_ou) else "Loss"
            elif ou_picked_under:
                ou_result = "Win" if actual_total < float(vegas_ou) else "Loss"

        # ---------- Moneyline result ----------
        ml_result = None
        if ml_pick and actual_margin is not None and home_a and away_a:
            if actual_margin == 0:
                ml_result = "Push"
            elif ml_pick == home_a:
                ml_result = "Win" if actual_margin > 0 else "Loss"
            elif ml_pick == away_a:
                ml_result = "Win" if actual_margin < 0 else "Loss"

        # ---------- Profit ----------
        # ATS odds: use the side we picked.
        ats_odds = None
        if spr is not None and spread_pick and home_a and away_a:
            picked_home = spread_pick == home_a
            ats_odds = float(r["closing_spread_home_odds"]) if r["closing_spread_home_odds"] is not None else None
            if not picked_home:
                ats_odds = float(r["closing_spread_away_odds"]) if r["closing_spread_away_odds"] is not None else None

        ou_odds = None
        if vegas_ou is not None and ou_pick:
            if ou_pick.startswith("over"):
                ou_odds = float(r["closing_over_odds"]) if r["closing_over_odds"] is not None else None
            elif ou_pick.startswith("under"):
                ou_odds = float(r["closing_under_odds"]) if r["closing_under_odds"] is not None else None

        ml_odds = None
        if ml_pick and home_a:
            ml_odds = float(r["closing_home_ml"]) if r["closing_home_ml"] is not None else None
            if ml_pick != home_a:
                ml_odds = float(r["closing_away_ml"]) if r["closing_away_ml"] is not None else None

        ats_profit = _calc_profit(ats_result, ats_odds)
        ou_profit = _calc_profit(ou_result, ou_odds)
        ml_profit = _calc_profit(ml_result, ml_odds)

        try:
            await pg_conn.execute(
                UPDATE_SQL,
                home_score, away_score, actual_total, actual_margin,
                ats_result, ou_result, ml_result,
                ats_profit, ou_profit, ml_profit,
                gid,
            )
            updated += 1
        except Exception as exc:  # noqa: BLE001 - keep going on per-row failures
            logger.error(f"  Failed to settle NFL prediction for game {gid}: {exc}")

    logger.info(f"  Settled {updated} NFL predictions with actual results")
    return updated


async def _run() -> None:
    conn = await asyncpg.connect(PSYCOPG2_DATABASE_URL)
    try:
        n = await settle_nfl_predictions(conn)
        print(f"Settled {n} NFL prediction result(s)")
    finally:
        await conn.close()
    # Verify remaining gap
    conn2 = await asyncpg.connect(PSYCOPG2_DATABASE_URL)
    try:
        remaining = await conn2.fetchval(
            "SELECT count(*) FROM nfl.game_predictions gp JOIN nfl.games g ON gp.game_id = g.id "
            "WHERE g.status='FINAL' AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL "
            "AND gp.ats_result IS NULL"
        )
        print(f"Remaining unsettled FINAL predictions: {remaining}")
    finally:
        await conn2.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(_run())
