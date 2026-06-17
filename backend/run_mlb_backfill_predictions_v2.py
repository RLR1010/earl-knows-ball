#!/usr/bin/env python3
"""
MLB Prediction Backfill v2 — uses raw asyncpg to avoid SQLAlchemy greenlet issues.

Usage:
    python3 run_mlb_backfill_predictions_v2.py                                  # all missing
    python3 run_mlb_backfill_predictions_v2.py --from-date 2026-04-12           # range
    python3 run_mlb_backfill_predictions_v2.py --from-date 2026-06-01 --to-date 2026-06-06
    python3 run_mlb_backfill_predictions_v2.py --dry-run --from-date 2026-06-01
"""
import asyncio
import asyncpg
import logging
import sys
import os
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("APP_ENV", "production")

from app.core.config import settings

# Import the prediction functions directly (they use their own asyncpg connections)
from app.handicapping.mlb.mlb_xgb_model_ats import predict_ats
from app.handicapping.mlb.mlb_xgb_model_ou import predict_ou
from app.handicapping.mlb.mlb_xgb_model_ml import predict_ml
from app.handicapping.mlb.mlb_engine import MLBTeamStats, MLBTeamStatsBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("earl.mlb_backfill_v2")

DRY_RUN = "--dry-run" in sys.argv

FROM_DATE = None
TO_DATE = None
for i, arg in enumerate(sys.argv):
    if arg == "--from-date" and i + 1 < len(sys.argv):
        FROM_DATE = date.fromisoformat(sys.argv[i + 1])
    elif arg == "--to-date" and i + 1 < len(sys.argv):
        TO_DATE = date.fromisoformat(sys.argv[i + 1])

DSN = settings.database_url.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")


async def get_game_info(conn, year):
    """Get all final games with team info, no predictions yet."""
    where_extra = ""
    params = {"year": year}
    if FROM_DATE:
        where_extra += " AND g.date::date >= $2"
        params["from_date"] = FROM_DATE
    if TO_DATE:
        where_extra += " AND g.date::date <= $3"
        params["to_date"] = TO_DATE

    rows = await conn.fetch(f"""
        SELECT g.id, g.mlb_game_id, g.date, g.home_team_id, g.away_team_id,
               g.home_score, g.away_score, g.status,
               g.roof_type,
               ht.abbreviation as home_abbr, at.abbreviation as away_abbr
        FROM mlb.games g
        JOIN mlb.seasons s ON s.id = g.season_id
        JOIN mlb.teams ht ON ht.id = g.home_team_id
        JOIN mlb.teams at ON at.id = g.away_team_id
        WHERE s.year = $1 AND g.status = 'FINAL'
          AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM mlb.game_predictions gp
              WHERE gp.game_id = g.id AND gp.source = 'api'
          )
          {where_extra}
        ORDER BY g.date
    """, year, *(v for k, v in params.items() if k != "year"))
    return rows


async def get_betting_line(conn, game_id: int):
    """Get latest betting line for a game (spread, O/U, moneyline)."""
    row = await conn.fetchrow("""
        SELECT spread, over_under, home_moneyline, away_moneyline
        FROM mlb.betting_lines_consolidated
        WHERE game_id = $1
        LIMIT 1
    """, game_id)
    return row


async def save_prediction(conn, game_id, home_score, away_score,
                          pred_home, pred_away, pred_total, pred_margin,
                          margin_conf, rl_conf, ml_conf, ou_conf,
                          ou_pick, run_line_pick,
                          run_line_result, ou_result, ml_result):
    """Save prediction to mlb.game_predictions."""
    await conn.execute("""
        INSERT INTO mlb.game_predictions
            (game_id, predicted_home_runs, predicted_away_runs, predicted_total,
             predicted_margin, margin_conf, rl_conf, ml_conf, ou_conf,
             ou_pick, run_line_pick,
             actual_home_runs, actual_away_runs, actual_total, actual_margin,
             run_line_result, ou_result, ml_result, source)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,'api')
        ON CONFLICT (game_id, source) DO NOTHING
    """, game_id, pred_home, pred_away, pred_total, pred_margin,
       margin_conf, rl_conf, ml_conf, ou_conf,
       ou_pick, run_line_pick,
       home_score, away_score, home_score + away_score, home_score - away_score,
       run_line_result, ou_result, ml_result)


def make_line_obj(spread, ou, hm, am):
    """Create a simple line object for prediction functions."""
    import types
    obj = types.SimpleNamespace()
    obj.spread = spread
    obj.over_under = ou
    obj.home_moneyline = hm
    obj.away_moneyline = am
    return obj


async def process_game(conn, game, hs, aws, yr, date_str):
    """Run predictions for a single game and return result dict or None."""
    try:
        gid = game["id"]
        home_abbr = game["home_abbr"]
        away_abbr = game["away_abbr"]

        # Get betting line
        bl = await get_betting_line(conn, gid)
        line = make_line_obj(
            bl["spread"] if bl else None,
            bl["over_under"] if bl else None,
            bl["home_moneyline"] if bl else None,
            bl["away_moneyline"] if bl else None,
        )

        # Build dummy team stats (the prediction functions rebuild them internally)
        dummy_home = MLBTeamStats(home_abbr, game["home_team_id"], yr)
        dummy_home.games = 1
        dummy_away = MLBTeamStats(away_abbr, game["away_team_id"], yr)
        dummy_away.games = 1

        # Run all three models
        margin, margin_conf = await predict_ats(
            gid, home_abbr, away_abbr, yr, date_str,
            dummy_home, dummy_away, line
        )
        ou_total, ou_conf = await predict_ou(
            gid, home_abbr, away_abbr, yr, date_str,
            dummy_home, dummy_away, line
        )
        ml_prob, ml_conf_val, ml_edge = await predict_ml(
            gid, home_abbr, away_abbr, yr, date_str,
            dummy_home, dummy_away, line
        )

        # Compute derived values
        margin_v = margin if margin is not None else 0.0
        if margin is not None:
            margin_c = min(0.5 + abs(margin) * 0.04, 0.90)
        else:
            margin_c = 0.50

        # Total
        if margin is not None:
            ou_v = ou_total if ou_total is not None else 8.5
            pred_home = (ou_v + margin_v) / 2
            pred_away = (ou_v - margin_v) / 2
            pred_total = pred_home + pred_away
        else:
            pred_home = None
            pred_away = None
            pred_total = None

        # Picks based on betting line
        ou_pick = None
        rl_pick = None
        rl_result = None
        ou_result_val = None
        ml_result_val = None

        if ou_total is not None and line.over_under is not None:
            diff = ou_total - line.over_under
            if diff > 0.2:
                ou_pick = "over"
            elif diff < -0.2:
                ou_pick = "under"
            else:
                ou_pick = "Push / No edge"
            # Result vs actual
            actual_total = hs + aws
            if abs(actual_total - line.over_under) < 0.5:
                ou_result_val = "Push"
            elif (ou_total > line.over_under) == (actual_total > line.over_under):
                ou_result_val = "Win"
            else:
                ou_result_val = "Loss"

        if margin is not None and line.spread is not None:
            eff = margin + line.spread
            if eff > 0.3:
                rl_pick = home_abbr
            elif eff < -0.3:
                rl_pick = away_abbr
            else:
                rl_pick = "Push / No edge"
            # Result vs actual
            actual_margin = hs - aws
            if abs(actual_margin - line.spread) < 0.3:
                rl_result = "Push"
            elif (margin + line.spread > 0) == (actual_margin + line.spread > 0):
                rl_result = "Win"
            else:
                rl_result = "Loss"

        # ML result
        actual_margin = hs - aws
        if actual_margin != 0 and margin is not None:
            ml_result_val = "Win" if (margin > 0) == (actual_margin > 0) else "Loss"

        return {
            "game_id": gid,
            "pred_home": round(pred_home, 1) if pred_home else None,
            "pred_away": round(pred_away, 1) if pred_away else None,
            "pred_total": round(pred_total, 1) if pred_total else None,
            "pred_margin": round(margin_v, 1),
            "margin_conf": round(ou_conf if ou_total else margin_c, 2) if margin_c else None,
            "rl_conf": round(margin_c, 2),
            "ml_conf": round(ml_conf_val, 2) if ml_conf_val else None,
            "ou_conf": round(ou_conf, 2) if ou_conf else None,
            "ou_pick": ou_pick,
            "rl_pick": rl_pick,
            "rl_result": rl_result,
            "ou_result": ou_result_val,
            "ml_result": ml_result_val,
            "home_score": hs,
            "away_score": aws,
        }

    except Exception as e:
        logger.error(f"    Error on game {game['id']}: {e}")
        return None


async def main():
    year = FROM_DATE.year if FROM_DATE else 2026

    conn = await asyncpg.connect(DSN)

    games = await get_game_info(conn, year)
    logger.info(f"Games to process: {len(games)}")

    if not games:
        logger.info("Nothing to do!")
        await conn.close()
        return

    if DRY_RUN:
        logger.info(f"Dry run: would process {len(games)} games")
        await conn.close()
        return

    saved = 0
    failed = 0
    batch = []
    total = len(games)

    for i, game in enumerate(games):
        gid = game["id"]
        hs = game["home_score"]
        aws = game["away_score"]
        gd = game["date"].date()
        date_str = gd.isoformat()

        if (i + 1) % 10 == 1 or i == 0:
            this_gd = gd.isoformat()
            logger.info(f"  [{i+1}/{total}] game {gid} ({game['home_abbr']} vs {game['away_abbr']}, {this_gd})")

        result = await process_game(conn, game, hs, aws, year, date_str)

        if result:
            await save_prediction(
                conn, result["game_id"],
                result["home_score"], result["away_score"],
                result["pred_home"], result["pred_away"],
                result["pred_total"], result["pred_margin"],
                result["margin_conf"], result["rl_conf"],
                result["ml_conf"], result["ou_conf"],
                result["ou_pick"], result["rl_pick"],
                result["rl_result"], result["ou_result"],
                result["ml_result"],
            )
            saved += 1
        else:
            failed += 1

        if (i + 1) % 25 == 0:
            logger.info(f"  Progress: {saved} saved, {failed} failed ({i+1}/{total})")

    await conn.close()
    logger.info(f"\nDone! {saved} saved, {failed} failed of {total} games")


if __name__ == "__main__":
    asyncio.run(main())
