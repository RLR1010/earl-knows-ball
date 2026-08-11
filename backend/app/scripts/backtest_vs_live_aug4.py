"""THE DEFINITIVE leakage/consistency test (Rich's request):

Run the model exactly as the BACKTEST does over Aug 4 -> now (feature frame =
ALL 2020-2026 FINAL games via build_features, same pkls, same pick rule
pred_over = pred_total > total), and compare each game's resulting
ou_pick / predicted_total / run_line_pick / predicted_margin to the STORED
LIVE picks for those same games.

If backtest-model == stored-live on (almost) every game -> identical results,
confirms no leakage and that live picks came from this model.
If they differ on many games -> the two paths feed DIFFERENT inputs (feature
construction or line), which is the real root cause.

NO DB writes. Reads stored live picks only.
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

    bt = {}  # game_id -> backtest-model pick values
    for _, row in aug.iterrows():
        gid = str(row["game_id"])
        spread = row.get("run_line")
        total = row.get("over_under")
        if pd.isna(total) or total is None:
            total = row.get("ou_line", 8.5)
        if pd.isna(total):
            total = None
        feats_ats = _extract_feature_vector(row, "ats")
        feats_ou = _extract_feature_vector(row, "ou")
        pred_margin = float(ats_model.predict(feats_ats[np.newaxis, :])[0]) if feats_ats is not None else None
        pred_total = float(ou_model.predict(feats_ou[np.newaxis, :])[0]) if feats_ou is not None else None
        pred_home_covers = (pred_margin + (spread or 0)) > 0 if pred_margin is not None else None
        pred_over = pred_total > total if (pred_total is not None and total is not None) else None
        bt[gid] = {
            "pred_margin": pred_margin,
            "pred_total": pred_total,
            "rl_pick": "home" if pred_home_covers else "away",
            "ou_pick": "over" if pred_over else ("under" if pred_over is not None else None),
            "line": total,
        }

    async def load_live():
        async with async_session() as db:
            res = await db.execute(text("""
                SELECT gp.game_id::text gid, gp.ou_pick, gp.predicted_total,
                       gp.run_line_pick, gp.predicted_margin, gp.ou_result
                FROM mlb.game_predictions gp
                JOIN mlb.games g ON g.id=gp.game_id
                WHERE g.date >= DATE '2026-08-04' AND g.date <= DATE '2026-08-12'
                  AND gp.source='api'
            """))
            return res.fetchall()
    live = asyncio.run(load_live())
    print(f"stored live picks in window: {len(live)}")

    both = [r for r in live if r.gid in bt]
    print(f"games with both backtest-model + live: {len(both)}")

    ou_agree = ou_diff = 0
    rl_agree = rl_diff = 0
    pred_total_diff = []
    for r in both:
        b = bt[r.gid]
        # OU
        if b["ou_pick"] and r.ou_pick:
            if b["ou_pick"] == r.ou_pick.lower():
                ou_agree += 1
            else:
                ou_diff += 1
                pred_total_diff.append((r.gid, b["pred_total"], r.predicted_total, b["ou_pick"], r.ou_pick, r.ou_result))
        # RL: compare stored predicted_margin to recomputed pred_margin (numeric model output)
        if b["pred_margin"] is not None and r.predicted_margin is not None:
            margin_diff = abs(b["pred_margin"] - float(r.predicted_margin))
            if margin_diff < 0.5:  # effectively same model output
                rl_agree += 1
            else:
                rl_diff += 1

    n = ou_agree + ou_diff
    print(f"\n=== OU pick agreement (backtest-model vs stored-live) ===")
    print(f"  agree={ou_agree}  differ={ou_diff}  agreement%={round(100*ou_agree/n,1) if n else 'n/a'}")
    nrl = rl_agree + rl_diff
    print(f"  RL (predicted_margin closeness <0.5): agree={rl_agree} differ={rl_diff} "
          f"agreement%={round(100*rl_agree/nrl,1) if nrl else 'n/a'}")

    # predicted_total closeness
    pt_closeness = [abs(b["pred_total"] - float(r.predicted_total))
                    for r in both if b["pred_total"] is not None and r.predicted_total is not None]
    if pt_closeness:
        import statistics as _st
        under05 = sum(1 for d in pt_closeness if d < 0.2)
        print(f"  predicted_total |bt-live| : n={len(pt_closeness)} mean={round(_st.mean(pt_closeness),3)} "
              f"within 0.2 runs={round(100*under05/len(pt_closeness),1)}% ")

    print(f"\n  Games where backtest-model OU side != stored live OU side ({ou_diff}):")
    print(f"  {'gid':>6} {'bt_predTot':>9} {'live_predTot':>11} {'btSide':<6} {'liveSide':<8} {'liveRes':<4}")
    for g, bp, lp, bs, ls, res in pred_total_diff:
        print(f"  {g:>6} {str(round(bp,2) if bp else '-'):>9} {str(round(lp,2) if lp else '-'):>11} {bs:<6} {ls:<8} {str(res):<4}")

    # Win% of both sets
    def wp(rows, side_attr):
        tot = len(rows); w = sum(1 for r in rows if r.ou_result=='Win'); l = sum(1 for r in rows if r.ou_result=='Loss')
        return f"{w}-{l}" + (f" ({round(100*w/(w+l),1)}%)" if (w+l) else "")
    print(f"\n  Live OU record (settled): {wp([r for r in live if r.ou_result],'')}")


if __name__ == "__main__":
    main()
