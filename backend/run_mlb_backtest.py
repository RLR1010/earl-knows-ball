#!/usr/bin/env python3
"""
MLB backtest runner — runs all three XGBoost models through every completed game
in a season and saves predictions to mlb.game_predictions for the admin page.

Usage:
    python3 run_mlb_backtest.py 2025
    python3 run_mlb_backtest.py 2025 --to 2026
"""
import asyncio, asyncpg, logging, sys, os
from datetime import date, datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("APP_ENV", "production")

from app.core.config import settings

from app.handicapping.mlb.mlb_xgb_model_ats import predict_ats
from app.handicapping.mlb.mlb_xgb_model_ou import predict_ou
from app.handicapping.mlb.mlb_xgb_model_ml import predict_ml
from app.handicapping.mlb.mlb_engine import MLBTeamStats, MLBTeamStatsBuilder
from app.models.mlb import MLBGamePrediction

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("earl.mlb_backtest")

DSN = settings.database_url.replace("+asyncpg", "")

async def backtest_year(year: int):
    """Run full backtest for one year using raw asyncpg (no ORM greenlet issues)."""
    conn = await asyncpg.connect(DSN)

    # Get season
    season = await conn.fetchrow("SELECT id FROM mlb.seasons WHERE year=$1", year)
    if not season:
        logger.error(f"Season {year} not found")
        await conn.close()
        return
    season_id = season["id"]

    # Clear existing api predictions for this year
    await conn.execute("""
        DELETE FROM mlb.game_predictions gp
        USING mlb.games g
        WHERE gp.game_id = g.id AND g.season_id = $1 AND gp.source = 'api'
    """, season_id)

    # Get all game dates with completed games
    dates = await conn.fetch("""
        SELECT DISTINCT g.date::date as game_date
        FROM mlb.games g
        WHERE g.season_id = $1 AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
        ORDER BY game_date
    """, season_id)

    logger.info(f"Processing {len(dates)} game dates for {year}...")

    home_wins = 0; home_losses = 0
    away_wins = 0; away_losses = 0
    rl_correct = 0; rl_incorrect = 0; rl_pushes = 0
    ou_correct = 0; ou_incorrect = 0; ou_pushes = 0
    ml_correct = 0; ml_incorrect = 0

    for idx, row in enumerate(dates):
        game_date = row["game_date"]

        # Build team stats up to this date
        stats = await _build_stats(conn, year, game_date)
        if not stats:
            continue

        # Get games on this date
        games = await conn.fetch("""
            SELECT g.id, g.home_team_id, g.away_team_id, g.home_score, g.away_score,
                   g.roof_type, g.temperature, g.wind_speed, g.date::date as gd,
                   ht.abbreviation as ha, at.abbreviation as aa
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.season_id = $1 AND g.date::date = $2
              AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
        """, season_id, game_date)

        for g in games:
            home_abbr = g["ha"]; away_abbr = g["aa"]
            home_stats = stats.get(home_abbr)
            away_stats = stats.get(away_abbr)
            if not home_stats or not away_stats:
                continue

            # Get betting lines
            lines = await conn.fetchrow("""
                SELECT home_moneyline, away_moneyline, spread, over_under,
                       spread_home_odds, spread_away_odds
                FROM mlb.betting_lines_consolidated WHERE game_id=$1 LIMIT 1
            """, g["id"])

            # Convert to SimpleNamespace for predict functions
            class LineObj:
                pass
            line_obj = LineObj()
            if lines:
                line_obj.home_moneyline = lines["home_moneyline"]
                line_obj.away_moneyline = lines["away_moneyline"]
                line_obj.spread = lines["spread"]
                line_obj.over_under = lines["over_under"]
                line_obj.spread_home_odds = lines["spread_home_odds"]
                line_obj.spread_away_odds = lines["spread_away_odds"]
            else:
                line_obj.home_moneyline = None
                line_obj.away_moneyline = None
                line_obj.spread = None
                line_obj.over_under = None
                line_obj.spread_home_odds = None
                line_obj.spread_away_odds = None

            date_str = game_date.isoformat()

            # Run all three models
            margin, _ = await predict_ats(g["id"], home_abbr, away_abbr, year, date_str,
                                            home_stats, away_stats, line_obj)
            ou_total, _ = await predict_ou(g["id"], home_abbr, away_abbr, year, date_str,
                                            home_stats, away_stats, line_obj)
            ml_prob, _, _ = await predict_ml(g["id"], home_abbr, away_abbr, year, date_str,
                                              home_stats, away_stats, line_obj)

            if margin is None or ou_total is None or ml_prob is None:
                logger.warning(f"  Skipping game {g['id']} — model failed")
                continue

            # Derived score
            home_runs = round((ou_total + margin) / 2, 1)
            away_runs = round((ou_total - margin) / 2, 1)
            pred_total = round(home_runs + away_runs, 1)
            pred_margin = round(margin, 1)

            hs = int(g["home_score"])
            aws = int(g["away_score"])
            actual_margin = hs - aws
            actual_total = hs + aws

            # RL result — spread convention: negative = home favored
            rl_result = None
            rl_pick = None
            if line_obj.spread is not None:
                pred_covers_home = margin + line_obj.spread > 0
                actual_covers_home = actual_margin + line_obj.spread > 0
                rl_pick = home_abbr if pred_covers_home else away_abbr
                if abs(actual_margin + line_obj.spread) < 0.3:
                    rl_result = "Push"
                elif pred_covers_home == actual_covers_home:
                    rl_result = "Win"
                else:
                    rl_result = "Loss"

            # OU result
            ou_result = None
            if line_obj.over_under is not None:
                vegas_ou = line_obj.over_under
                if abs(actual_total - vegas_ou) < 0.5:
                    ou_result = "Push"
                elif (pred_total > vegas_ou) == (actual_total > vegas_ou):
                    ou_result = "Win"
                else:
                    ou_result = "Loss"

            # ML result — from ATS margin direction
            ml_result = None
            if actual_margin != 0:
                ml_result = "Win" if (pred_margin > 0) == (actual_margin > 0) else "Loss"

            # PnL
            def _pl(result, odds):
                if result == "Win":
                    return round(100 * (100.0 / abs(odds) if odds < 0 else odds / 100.0), 2)
                elif result == "Loss":
                    return -100.0
                return 0.0

            ou_odds = -110
            ml_odds = line_obj.home_moneyline if pred_margin > 0 else line_obj.away_moneyline

            # Use actual spread odds from consolidated table instead of hardcoded -110
            if rl_pick and rl_pick == home_abbr:
                rl_odds = line_obj.spread_home_odds if line_obj.spread_home_odds else -110
            else:
                rl_odds = line_obj.spread_away_odds if line_obj.spread_away_odds else -110

            rl_profit = _pl(rl_result, rl_odds) if rl_result else None
            ou_profit = _pl(ou_result, ou_odds) if ou_result else None
            ml_profit = _pl(ml_result, ml_odds) if ml_result and ml_odds else None

            # EV per $100 flat bet
            def _ev(conf: float, odds: int | None) -> float:
                if not odds or conf <= 0 or conf >= 1:
                    return 0.0
                profit = (100.0 * 100.0 / float(abs(odds))) if odds < 0 else float(odds)
                return round((conf * profit) - ((1.0 - conf) * 100.0), 2)

            ml_conf = round(min(0.50 + abs(pred_margin) * 0.12, 0.95), 2) if pred_margin else 0.50

            rl_ev = _ev(0.5, rl_odds)
            ou_ev = _ev(0.5, ou_odds)
            ml_ev = _ev(ml_conf, ml_odds) if ml_odds else None

            # Save prediction
            await conn.execute("""
                INSERT INTO mlb.game_predictions (
                    game_id, source,
                    predicted_home_runs, predicted_away_runs, predicted_total, predicted_margin,
                    rl_conf, ou_conf, ml_conf, margin_conf,
                    run_line_pick, ou_pick, ml_pick,
                    actual_home_runs, actual_away_runs, actual_total, actual_margin,
                    run_line_result, ou_result, ml_result,
                    ats_odds, ou_odds, ml_odds, ats_profit, ou_profit, ml_profit,
                    ats_ev, ou_ev, ml_ev
                ) VALUES ($1,'api',
                    $2,$3,$4,$5,
                    0.5,0.5,$25,0.5,
                    $6,$7,$8,
                    $9,$10,$11,$12,
                    $13,$14,$15,
                    $16,$17,$18,$19,$20,$21,
                    $22,$23,$24
                ) ON CONFLICT (game_id, source) DO NOTHING
            """,
                g["id"],
                home_runs, away_runs, pred_total, pred_margin,
                rl_pick,
                "Over" if ou_result == "Win" else ("Under" if ou_result == "Loss" else None),
                "home" if pred_margin > 0 else "away",
                hs, aws, actual_total, actual_margin,
                rl_result, ou_result, ml_result,
                rl_odds, ou_odds, ml_odds,
                rl_profit, ou_profit, ml_profit,
                rl_ev, ou_ev, ml_ev,
                ml_conf
            )

            # Track totals
            if rl_result == "Win": rl_correct += 1
            elif rl_result == "Loss": rl_incorrect += 1
            elif rl_result == "Push": rl_pushes += 1
            if ou_result == "Win": ou_correct += 1
            elif ou_result == "Loss": ou_incorrect += 1
            elif ou_result == "Push": ou_pushes += 1
            if ml_result == "Win": ml_correct += 1
            elif ml_result == "Loss": ml_incorrect += 1

        if (idx + 1) % 30 == 0:
            logger.info(f"  Progress: {idx+1}/{len(dates)} dates")

    await conn.close()

    rl_total = rl_correct + rl_incorrect
    ou_total = ou_correct + ou_incorrect
    ml_total = ml_correct + ml_incorrect
    total = rl_total

    results = {
        "season": year,
        "total_games": total,
        "run_line": {"correct": rl_correct, "incorrect": rl_incorrect, "pushes": rl_pushes,
                      "pct": round(rl_correct / max(rl_total, 1), 3)},
        "over_under": {"correct": ou_correct, "incorrect": ou_incorrect, "pushes": ou_pushes,
                        "pct": round(ou_correct / max(ou_total, 1), 3)},
        "moneyline": {"correct": ml_correct, "incorrect": ml_incorrect,
                       "pct": round(ml_correct / max(ml_total, 1), 3)},
    }

    logger.info(f"\nBacktest {year} complete!")
    logger.info(f"  Games: {total}")
    logger.info(f"  RL:  {rl_correct}-{rl_incorrect}-{rl_pushes} ({results['run_line']['pct']*100:.1f}%)")
    logger.info(f"  OU:  {ou_correct}-{ou_incorrect}-{ou_pushes} ({results['over_under']['pct']*100:.1f}%)")
    logger.info(f"  ML:  {ml_correct}-{ml_incorrect} ({results['moneyline']['pct']*100:.1f}%)")

    return results


async def _build_stats(conn, year, up_to_date):
    """Build MLBTeamStats dict for all teams up to a given date (asyncpg version)."""
    teams = await conn.fetch("SELECT id, abbreviation FROM mlb.teams")

    stats = {}
    for t in teams:
        stats[t["abbreviation"]] = MLBTeamStats(t["abbreviation"], t["id"], year)

    games = await conn.fetch("""
        SELECT g.home_team_id, g.away_team_id, g.home_score, g.away_score, g.date::date as gd
        FROM mlb.games g JOIN mlb.seasons s ON s.id=g.season_id
        WHERE s.year=$1 AND g.date::date < $2
          AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
        ORDER BY g.date
    """, year, up_to_date)

    for g in games:
        ha = next((t["abbreviation"] for t in teams if t["id"] == g["home_team_id"]), None)
        aa = next((t["abbreviation"] for t in teams if t["id"] == g["away_team_id"]), None)
        if not ha or not aa:
            continue
        home = stats[ha]; away = stats[aa]
        hs = g["home_score"]; aws = g["away_score"]
        home.games += 1; home.home_games += 1
        home.runs_for += hs; home.runs_against += aws
        home.home_runs_for += hs; home.home_runs_against += aws
        away.games += 1; away.away_games += 1
        away.runs_for += aws; away.runs_against += hs
        away.away_runs_for += aws; away.away_runs_against += hs
        if hs > aws:
            home.ml_wins += 1; away.ml_losses += 1
            home.recent_form.append("W"); away.recent_form.append("L")
        else:
            home.ml_losses += 1; away.ml_wins += 1
            home.recent_form.append("L"); away.recent_form.append("W")

    for abbr, s in stats.items():
        last10 = [r for r in s.recent_form[-10:]]
        s.last_10_wins = last10.count("W")
        s.last_10_losses = last10.count("L")

    return stats


if __name__ == "__main__":
    years = [int(a) for a in sys.argv[1:] if a.isdigit()]
    if not years:
        print("Usage: python3 run_mlb_backtest.py <year> [year...]")
        sys.exit(1)

    async def main():
        for y in years:
            await backtest_year(y)

    asyncio.run(main())
