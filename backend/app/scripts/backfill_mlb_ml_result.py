"""Backfill ml_result (and ml_odds/ml_profit) for MLB FINAL games that never got
their moneyline pick outcome computed.

Why: boxscore_ingest.update_prediction_results() originally only matched
ml_pick == "home"/"away", but MLB stores the TEAM ABBREVIATION (e.g. 'LAD') in
ml_pick, so post-game results were never set -> the moneyline pick showed no
green/red on completed-game cards. This backfill re-applies the corrected
abbreviation-resolving logic to any FINAL game still missing a result.

Run: cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/backfill_mlb_ml_result.py
"""
import asyncio
import logging

from sqlalchemy import text
from app.database import async_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.backfill_mlb_ml_result")


async def main() -> None:
    async with async_session() as db:
        # FINAL games with scores but no moneyline result, where a pick exists.
        rows = (
            await db.execute(
                text("""
                    SELECT gp.game_id,
                           gp.ml_pick,
                           g.home_score, g.away_score,
                           ht.abbreviation AS home_abbrev,
                           at.abbreviation AS away_abbrev,
                           blc.closing_home_ml, blc.closing_away_ml
                    FROM mlb.game_predictions gp
                    JOIN mlb.games g ON g.id = gp.game_id
                    JOIN mlb.teams ht ON ht.id = g.home_team_id
                    JOIN mlb.teams at ON at.id = g.away_team_id
                    LEFT JOIN mlb.betting_lines_consolidated blc ON blc.game_id = g.id
                    WHERE g.status = 'FINAL'
                      AND g.home_score IS NOT NULL
                      AND g.away_score IS NOT NULL
                      AND gp.ml_pick IS NOT NULL
                      AND gp.ml_result IS NULL
                    ORDER BY gp.game_id
                """)
            )
        ).mappings().all()

        if not rows:
            logger.info("No MLB FINAL games need a moneyline result backfill.")
            return

        logger.info("Found %d MLB FINAL game(s) to backfill moneyline result.", len(rows))
        updated = 0
        for r in rows:
            margin = r["home_score"] - r["away_score"]
            pick = (r["ml_pick"] or "").strip().upper()
            home_a = (r["home_abbrev"] or "").strip().upper()
            away_a = (r["away_abbrev"] or "").strip().upper()
            resolved_home = pick == home_a or pick in ("home", "HOME")
            resolved_away = pick == away_a or pick in ("away", "AWAY")

            if margin == 0:
                ml_result = "Push"
                ml_odds = None
            elif resolved_home:
                ml_result = "Win" if margin > 0 else "Loss"
                ml_odds = r["closing_home_ml"]
            elif resolved_away:
                ml_result = "Win" if margin < 0 else "Loss"
                ml_odds = r["closing_away_ml"]
            else:
                logger.warning("  game %s: ml_pick=%r didn't match %r/%r — skipping",
                               r["game_id"], r["ml_pick"], home_a, away_a)
                continue

            profit = 0.0
            if ml_result == "Win" and ml_odds is not None:
                o = float(ml_odds)
                profit = (o / 100.0) if o > 0 else (-100.0 / o)
            elif ml_result == "Loss":
                profit = -1.0

            await db.execute(
                text("""
                    UPDATE mlb.game_predictions gp
                    SET ml_result = :res, ml_odds = :odds, ml_profit = :profit
                    WHERE gp.game_id = :gid AND gp.ml_pick IS NOT NULL
                """),
                {"res": ml_result, "odds": ml_odds, "profit": profit, "gid": r["game_id"]},
            )
            updated += 1

        await db.commit()
        logger.info("Backfilled %d MLB game(s).", updated)


if __name__ == "__main__":
    asyncio.run(main())
