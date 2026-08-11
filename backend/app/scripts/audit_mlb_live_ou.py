"""Diagnostic: re-run the LIVE MLB OU/Margin models against already-completed
August games (same code path the live scheduler uses) and compare the OU picks
against actual outcomes.

Determines whether live OU underperformance is a real model/feature problem or
sample variance / line handling. Uses the same model files + build_features.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
import pandas as pd

from app.handicapping.mlb.data_loader import get_data_loader, build_features
from app.handicapping.mlb.mlb_engine import _load_model_for_year, _extract_feature_vector

YEAR = 2026


def main():
    dl = get_data_loader()

    # Live path: all FINAL games (all seasons) + the target games.
    all_historic = dl.load_games(status="FINAL", include_upcoming=False)
    print(f"all_historic rows: {len(all_historic)}")

    # Select this season's August games (now FINAL) to re-predict.
    aug = all_historic[
        (all_historic["season_year"] == YEAR)
        & (all_historic["game_date"].astype(str).str[:10] >= "2026-08-01")
        & (all_historic["game_date"].astype(str).str[:10] <= "2026-08-12")
    ].copy()
    print(f"August FINAL games this season: {len(aug)}")

    if aug.empty:
        print("No August games found; check season_year/game_date columns")
        return []

    # Mimic the live scheduler: build features over all historic + these targets.
    df = build_features(all_historic)
    print(f"feature df: {df.shape}")

    ats_model = _load_model_for_year("ats", YEAR)
    ou_model = _load_model_for_year("ou", YEAR)
    print(f"models: ats={'y' if ats_model is not None else 'n'} ou={'y' if ou_model is not None else 'n'}")

    results = []
    for _, row_s in df[df["game_id"].astype(str).isin(aug["game_id"].astype(str))].iterrows():
        total = row_s.get("over_under")
        if total is None or pd.isna(total):
            total = row_s.get("ou_line", 8.5)
        if pd.isna(total):
            total = 8.5
        ou_feats = _extract_feature_vector(row_s, "ou")
        if ou_feats is None:
            print(f"  skip {row_s.get('game_id')}: no ou feats")
            continue
        pred_total = float(ou_model.predict(np.asarray(ou_feats)[np.newaxis, :])[0])
        pred_over = pred_total > float(total)
        actual_total = float(row_s.get("home_score", 0) + row_s.get("away_score", 0))
        over_hit = actual_total > float(total)
        push = actual_total == float(total)
        results.append({
            "gid": str(row_s["game_id"]),
            "pred_total": round(pred_total, 2),
            "total": float(total),
            "side": "over" if pred_over else "under",
            "actual_total": int(actual_total),
            "hit": (over_hit if pred_over else (not over_hit)),
            "push": push,
        })

    n = len(results)
    wins = sum(1 for r in results if r["hit"])
    losses = sum(1 for r in results if not r["hit"] and not r["push"])
    pushes = sum(1 for r in results if r["push"])
    wl = wins + losses
    print(f"\n=== Re-predicted OU on final Aug games: n={n} ===")
    print(f"  Overall: {wins}-{losses}-{pushes}  win%={round(100*wins/wl,1) if wl else 'n/a'}")
    for name, side in (("OVER", "over"), ("UNDER", "under")):
        grp = [r for r in results if r["side"] == side]
        w = sum(1 for r in grp if r["hit"]); l = sum(1 for r in grp if not r["hit"] and not r["push"])
        print(f"  {name}: n={len(grp)} {w}-{l}  win%={round(100*w/(w+l),1) if w+l else 'n/a'}")
    # Tightness analysis
    for label, cond in (
        ("pred within 1 run of line", lambda r: abs(r["pred_total"] - r["total"]) < 1.0),
        ("pred >=1 run off line", lambda r: abs(r["pred_total"] - r["total"]) >= 1.0),
    ):
        grp = [r for r in results if cond(r)]
        if grp:
            w = sum(1 for r in grp if r["hit"]); l = sum(1 for r in grp if not r["hit"] and not r["push"])
            print(f"  {label}: n={len(grp)} {w}-{l}  win%={round(100*w/(w+l),1) if w+l else 'n/a'}")
    return results


if __name__ == "__main__":
    res = main()
    print("\nSample rows (game_id, pred_total, line, side, actual, hit):")
    for r in res[:15]:
        print(f"  {r['gid']}: pred={r['pred_total']} line={r['total']} {r['side']} act={r['actual_total']} {'HIT' if r['hit'] else ('PUSH' if r['push'] else 'miss')}")
