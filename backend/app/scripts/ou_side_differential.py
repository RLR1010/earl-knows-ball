"""Per-game differential: backtest-generated picks vs STORED live picks for
Aug 4 -> now. For each game with a stored live pick, recompute what the model
(both paths identical pkl) says, and flag disagreements side-by-side.

Focus: OU side + the line used, to expose any input mismatch (stale/wrong
total line in the live path) vs the closing line the backtest uses.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
import pandas as pd

from app.handicapping.mlb.data_loader import get_data_loader, build_features
from app.handicapping.mlb.mlb_engine import _load_model_for_year, _extract_feature_vector
from app.database import async_session
from sqlalchemy import text

YEAR = 2026
AUG_START = "2026-08-04"
AUG_END = "2026-08-12"


def main():
    dl = get_data_loader()
    games = dl.load_games(seasons=list(range(2020, YEAR + 1)), status="FINAL")
    df = build_features(games)
    aug = df[
        (df["season_year"] == YEAR)
        & (df["game_date"].astype(str).str[:10] >= AUG_START)
        & (df["game_date"].astype(str).str[:10] <= AUG_END)
    ]
    print(f"backtest feature games in window: {len(aug)}")

    ats_model = _load_model_for_year("ats", YEAR)
    ou_model = _load_model_for_year("ou", YEAR)

    backtest_picks = {}  # game_id -> dict(side, line, pred_total, pred_margin)
    for _, row_s in aug.iterrows():
        gid = str(row_s["game_id"])
        spread = row_s.get("run_line")
        total = row_s.get("over_under")
        if pd.isna(total) or total is None:
            total = row_s.get("ou_line", 8.5)
        if pd.isna(total):
            total = None
        ats_feats = _extract_feature_vector(row_s, "ats")
        ou_feats = _extract_feature_vector(row_s, "ou")
        pred_margin = pred_total = None
        if ats_feats is not None:
            pred_margin = float(ats_model.predict(np.asarray(ats_feats)[np.newaxis, :])[0])
        if ou_feats is not None and total is not None:
            pred_total = float(ou_model.predict(np.asarray(ou_feats)[np.newaxis, :])[0])
        side = "over" if (pred_total is not None and pred_total > total) else ("under" if pred_total is not None else None)
        backtest_picks[gid] = {
            "line": total, "pred_margin": pred_margin, "pred_total": pred_total,
            "side": side,
        }

    # Load stored live picks (all api rows in window) incl. the line/odds used
    async def load():
        async with async_session() as db:
            res = await db.execute(text("""
                SELECT gp.game_id::text gid, gp.ou_result, gp.ou_odds, gp.ou_profit,
                       gp.ml_result, gp.run_line_result,
                       bl.closing_ou AS close_ou, bl.closing_over_odds, bl.closing_under_odds,
                       (g.home_score + g.away_score) AS actual_total,
                       g.home_score, g.away_score, g.date
                FROM mlb.game_predictions gp
                JOIN mlb.games g ON g.id=gp.game_id
                LEFT JOIN mlb.betting_lines_consolidated bl ON bl.game_id=g.id
                WHERE g.date >= DATE '2026-08-04' AND g.date <= DATE '2026-08-12'
                  AND gp.source='api'
            """))
            return res.fetchall()
    stored = asyncio.run(load())
    print(f"stored live picks in window: {len(stored)}")

    # OU side stored: infer from ou_odds vs over/under closing odds
    def stored_ou_side(r):
        od = r.ou_odds
        if od is None or r.closing_over_odds is None or r.closing_under_odds is None:
            return None
        return "over" if abs(od - r.closing_over_odds) <= abs(od - r.closing_under_odds) else "under"

    agree_w = agree_l = dis_w = dis_l = 0
    no_bt = 0
    diffs = []
    for r in stored:
        b = backtest_picks.get(r.gid)
        if b is None or b["side"] is None:
            no_bt += 1
            continue
        ss = stored_ou_side(r)
        if ss is None:
            continue
        # Does live match backtest side?
        same_side = (ss == b["side"])
        hit = (r.ou_result == "Win")
        if same_side:
            if hit: agree_w += 1
            else: agree_l += 1
        else:
            if hit: dis_w += 1
            else: dis_l += 1
        if not same_side:
            diffs.append((r.gid, r.date, b["side"], ss, b["line"], r.close_ou,
                          b["pred_total"], r.ou_result, r.actual_total))

    tot = agree_w + agree_l + dis_w + dis_l
    print("\n=== OU pick-level agreement (backtest-model vs stored-live) ===")
    print(f"  same-side picks: {agree_w+agree_l}  (won {agree_w}, lost {agree_l})")
    print(f"  DIFFERENT-side picks: {dis_w+dis_l}  (won {dis_w}, lost {dis_l})")
    if tot:
        print(f"  side agreement rate: {round(100*(agree_w+agree_l)/tot,1)}%")
    print(f"  games with no comparable backtest pick: {no_bt}")

    print(f"\n  Games where backtest-model side != stored-live side ({len(diffs)}):")
    print(f"  {'gid':>6} {'date':<11} {'btSide':<7} {'liveSide':<9} {'btLine':>6} {'close_ou':>8} {'predTot':>7} {'res':<5} {'actTot':>6}")
    for d_ in diffs[:40]:
        print(f"  {d_[0]:>6} {d_[1]:%m-%d} {d_[2]:<7} {d_[3]:<9} {str(d_[4]):>6} {str(d_[5]):>8} {str(round(d_[6],2) if d_[6] else '-'):>7} {str(d_[7]):<5} {str(int(d_[8])):>6}")


if __name__ == "__main__":
    main()
