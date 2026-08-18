#!/usr/bin/env python3
"""
NON-DESTRUCTIVE test: does the MLB backtest produce the same predictions as the
live batch_predict_upcoming_games path?

Approach
--------
We run the REAL `_backtest_single_season` code path for the current season (2026),
but monkeypatch `_save_backtest_prediction` to CAPTURE the computed prediction
values into an in-memory dict instead of writing to `mlb.game_predictions`.

The stored `source='api'` rows for games on 2026-08-15 / 2026-08-16 (written by
the live batch predict when those games were still scheduled) are then compared
against the captured backtest predictions. NOTHING is written to the DB.

Important context
-----------------
- The live `batch_predict_upcoming_games` only predicts games that are still
  SCHEDULED (date > now). Since we're on Aug 17, the Aug 15-16 games are now
  FINAL and can no longer be re-run through the live path — so we use the
  backtest path (which evaluates FINAL games) and compare to the stored rows.
- Both paths load the same year-specific models (CURRENT_YEAR / season 2026)
  and both use `build_features()` → identical per-game feature vectors
  (rolling stats come from prior same-season games only via the LATERAL).
- The known divergence point is the LINE SOURCE:
    * backtest  : row["spread"] / row["ou_line"] (games-table / feature values)
    * live batch: MLBBettingLineConsolidated closing_spread/closing_ou FIRST,
                  then falls back to row["spread"].
  If the consolidated closing lines differ from the games-table lines, the ATS
  and OU *flags* (covers/over) can differ even when the model margin/total match.
"""

import asyncio
import os
import sys

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

# ─── DB setup ────────────────────────────────────────────────────────────────
from app.db_urls import ASYNC_DATABASE_URL, PSYCOPG2_DATABASE_URL  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.handicapping.mlb.mlb_engine import (  # noqa: E402
    _backtest_single_season,
    _save_backtest_prediction,  # original, to be monkeypatched
)

# Target games: source='api', played on these dates
TARGET_DATES = ("2026-08-15", "2026-08-16")

# Captured backtest predictions: {game_id: {...values...}}
CAPTURED = {}


async def _capture_save(
    db, row, year,
    home_score, away_score, spread, total,
    pred_margin, pred_total, pred_home_covers, pred_over, pred_home_wins,
    home_covers, actual_over, home_wins,
    pick_card_features_meta=None, curve_data=None, shap_info=None,
    ats_model_file=None, ou_model_file=None,
):
    """Drop-in replacement that records predictions without writing to DB."""
    try:
        gid = str(row.get("game_id"))
        when = str(row.get("game_date"))
        season = str(row.get("season_year"))
    except Exception:
        gid = when = season = "?"
    HOME_TEAM_ID = row.get("home_team_id", None)
    AWAY_TEAM_ID = row.get("away_team_id", None)
    home_team = str(row.get("ha", ""))
    away_team = str(row.get("aa", ""))
    # Replicate pick-string derivation (matches _save_api_prediction / _save_backtest_prediction)
    rl_picked_home = bool(pred_home_covers)
    ou_picked_over = bool(pred_over)
    ml_picked_home = bool(pred_home_wins)
    if spread is not None and spread:
        home_run_line_val = float(spread)
        away_run_line_val = -float(spread)
        rl_pick_str = f"{home_team} {home_run_line_val:+g}" if rl_picked_home else f"{away_team} {away_run_line_val:+g}"
    else:
        rl_pick_str = ""
    CAPTURED[gid] = {
        "game_id": gid,
        "home_team_id": HOME_TEAM_ID,
        "away_team_id": AWAY_TEAM_ID,
        "game_date": when,
        "season": season,
        "spread": float(spread or 0.0),
        "total": float(total or 0.0),
        "pred_margin": round(float(pred_margin or 0.0), 2),
        "pred_total": round(float(pred_total or 0.0), 2),
        "pred_home_covers": rl_picked_home,
        "pred_over": ou_picked_over,
        "pred_home_wins": ml_picked_home,
        "home_covers": bool(home_covers),
        "actual_over": bool(actual_over),
        "home_wins": bool(home_wins),
        "run_line_pick": rl_pick_str,
        "ou_pick": "Over" if ou_picked_over else "Under",
        "ml_pick": home_team if ml_picked_home else away_team,
    }
    return 1  # pretend one row saved


def monitor():
    sync_engine = create_engine(PSYCOPG2_DATABASE_URL, pool_pre_ping=True)
    with sync_engine.connect() as c:
        r = c.execute(text("""
            SELECT g.id, g.date, ht.abbreviation, at.abbreviation, gp.source,
                   gp.predicted_margin, gp.predicted_total,
                   gp.run_line_pick, gp.ou_pick, gp.ml_pick,
                   gp.predicted_home_runs, gp.predicted_away_runs, gp.created_at
            FROM mlb.game_predictions gp
            JOIN mlb.games g ON g.id = gp.game_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.date::date IN :dates
            ORDER BY g.date, g.id
        """), {"dates": tuple(TARGET_DATES)})
        rows = r.fetchall()
    sync_engine.dispose()
    return rows


async def main():
    # Patch the save function on the module so _backtest_single_season uses ours
    import app.handicapping.mlb.mlb_engine as eng
    eng._save_backtest_prediction = _capture_save

    # We need an async session that *never commits* — pass None db is not viable
    # (_save is patched, but the loop calls db.commit() at the end). Use a DB
    # session; our patched save never writes, and the final db.commit() commits
    # nothing.
    async_engine = create_async_engine(
        ASYNC_DATABASE_URL,
        pool_pre_ping=True,
    )
    async with AsyncSession(async_engine) as db:
        # Run the REAL backtest for the current season (2026)
        print("Running real backtest for 2026 (predictions captured in memory, DB untouched)...")
        result = await _backtest_single_season(db, year=2026, resume=False, num_games=0)
        print("Backtest result object:", result)

    await async_engine.dispose()

    # Refresh stored rows
    stored = monitor()
    print("\n=== STORED source='api' rows for 08-15/08-16 ===")
    fieldnames = ("predicted_margin", "predicted_total", "run_line_pick", "ou_pick", "ml_pick", "pred_home_runs", "pred_away_runs")
    for r in stored:
        idx, dt, ha, aa, src = r[0], r[1], r[2], r[3], r[4]
        vals = r[5:12]
        print(f"  game {idx} {dt} {aa}@ {ha} src={src}")
        for name, v in zip(fieldnames, vals):
            print(f"      {name:20s}= {v}")

    print("\n=== CAPTURED backtest predictions ===")
    for gid, v in sorted(CAPTURED.items()):
        print(f"  game {gid} {v['game_date']} spread={v['spread']} total={v['total']} "
              f"margin={v['pred_margin']:.4f} total={v['pred_total']:.4f} "
              f"covers={v['pred_home_covers']} over={v['pred_over']} hwin={v['pred_home_wins']}")

    # ─── Comparison ────────────────────────────────────────────────────────
    print("\n=== COMPARISON (stored api vs backtest) ===")
    stored_map = {}
    for r in stored:
        idx = str(r[0])
        stored_map[idx] = {
            "pred_margin": r[5],
            "pred_total": r[6],
            "run_line_pick": r[7],
            "ou_pick": r[8],
            "ml_pick": r[9],
        }

    def _norm_f(v):
        return None if v is None else round(float(v), 2)

    matches = mismatches = missing_in_backtest = 0
    for gid, st in sorted(stored_map.items()):
        bt = CAPTURED.get(gid)
        if bt is None:
            missing_in_backtest += 1
            print(f"  {gid}: STORED but NOT in backtest output")
            continue
        diffs = []
        # margin + total: numeric, rounded to 2dp both sides
        for k, name in [("pred_margin", "predicted_margin"), ("pred_total", "predicted_total")]:
            sv, bv = _norm_f(st[k]), _norm_f(bt[k])
            if sv is None and bv is None:
                continue
            if sv is None or bv is None or abs(sv - bv) > 1e-6:
                diffs.append(f"  {name}: stored={sv} backtest={bv}")
        # pick strings: exact string equality
        for k, name in [("run_line_pick", "run_line_pick"), ("ou_pick", "ou_pick"), ("ml_pick", "ml_pick")]:
            sv, bv = st[k], bt[k]
            if sv is None and bv is None:
                continue
            if sv != bv:
                diffs.append(f"  {name}: stored={sv!r} backtest={bv!r}")
        if diffs:
            mismatches += 1
            print(f"  MISMATCH game {gid}")
            for dline in diffs:
                print(dline)
        else:
            matches += 1
            print(f"  MATCH   game {gid}")

    print(f"\n─── SUMMARY ───")
    print(f"  stored rows:            {len(stored_map)}")
    print(f"  exact matches:          {matches}")
    print(f"  mismatches:             {mismatches}")
    print(f"  stored but not backtest:{missing_in_backtest}")
    print(f"  backtest captured:      {len(CAPTURED)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
