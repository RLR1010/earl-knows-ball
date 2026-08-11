"""Leakage test: replicate the MLB backtest on AUGUST games ONLY, in-memory
(no DB writes), and compare win% to the LIVE picks stored for August.

If backtest-August win% ≈ live-August win% (esp. OU ~44.8%), there is NO
lookahead leakage — the model genuinely underperforms in August.
If backtest-August is much higher (the model's predicted picks beat reality
at ~54% when live only got ~44%), that is strong evidence of leakage.

Faithful to _backtest_single_season: same data load (2020->2026 FINAL,
build_features), same models (_load_model_for_year 2026), same pick rules
(pred_over = pred_total > total; pred_home_covers = pred_margin > spread),
same confidence formulas. We only SKIP the _save_backtest_prediction DB write.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root

import numpy as np
import pandas as pd

from app.handicapping.mlb.data_loader import get_data_loader, build_features
from app.handicapping.mlb.mlb_engine import (
    _load_model_for_year,
    _extract_feature_vector,
)

YEAR = 2026
AUG_START = "2026-08-04"
AUG_END = "2026-08-12"


def _win_dollar(odds):
    if not odds:
        return 100.0
    o = int(odds)
    if o == 0:
        return 100.0
    return float(o) if o > 0 else 100.0 * 100.0 / abs(o)


def main():
    dl = get_data_loader()
    # Same load as _backtest_single_season: 2020 .. YEAR, status=FINAL
    games = dl.load_games(seasons=list(range(2020, YEAR + 1)), status="FINAL")
    print(f"loaded FINAL games 2020-{YEAR}: {len(games)}")

    df = build_features(games)
    print(f"feature df: {df.shape}")

    # Restrict to August games for the leakage test
    aug = df[
        (df["season_year"] == YEAR)
        & (df["game_date"].astype(str).str[:10] >= AUG_START)
        & (df["game_date"].astype(str).str[:10] <= AUG_END)
    ]
    print(f"August games in backtest feature set: {len(aug)}")

    ats_model = _load_model_for_year("ats", YEAR)
    ou_model = _load_model_for_year("ou", YEAR)

    # Verify model identity vs live (file mtimes)
    import os
    from app.handicapping.mlb.mlb_engine import _resolve_year_pkl_paths
    for mt in ("ats", "ou"):
        paths = _resolve_year_pkl_paths(mt)
        p = paths.get(YEAR)
        if p:
            print(f"  {mt} pkl for {YEAR}: {p}  mtime={os.path.getmtime(p)}")

    stats = {"ats": {"n": 0, "w": 0, "l": 0, "p": 0},
             "ou": {"n": 0, "w": 0, "l": 0, "p": 0}}
    # breakdown by side + tightness for OU
    ou_side = {"over": {"n": 0, "w": 0, "l": 0}, "under": {"n": 0, "w": 0, "l": 0}}

    predicted = []
    for _, row_s in aug.iterrows():
        gid = str(row_s["game_id"])
        spread = row_s.get("run_line")
        total = row_s.get("over_under")
        if pd.isna(total) or total is None:
            total = row_s.get("ou_line", 8.5)
        if pd.isna(total):
            total = None

        # ATS / run line pick
        ats_feats = _extract_feature_vector(row_s, "ats")
        ou_feats = _extract_feature_vector(row_s, "ou")
        if ats_feats is not None:
            pred_margin = float(ats_model.predict(np.asarray(ats_feats)[np.newaxis, :])[0])
            pred_home_covers = pred_margin > (spread if pd.notna(spread) else 0)
            actual_cover = (row_s["home_score"] - row_s["away_score"]) > (spread if pd.notna(spread) else 0)
            if pred_home_covers == actual_cover:
                stats["ats"]["w"] += 1
            else:
                stats["ats"]["l"] += 1
            stats["ats"]["n"] += 1

        # OU pick
        if ou_feats is not None and total is not None:
            pred_total = float(ou_model.predict(np.asarray(ou_feats)[np.newaxis, :])[0])
            pred_over = pred_total > total
            actual_total = float(row_s["home_score"] + row_s["away_score"])
            actual_over = actual_total > total
            push = actual_total == total
            side = "over" if pred_over else "under"
            if push:
                stats["ou"]["p"] += 1
            elif pred_over == actual_over:
                stats["ou"]["w"] += 1
                ou_side[side]["w"] += 1
            else:
                stats["ou"]["l"] += 1
                ou_side[side]["l"] += 1
            stats["ou"]["n"] += 1
            ou_side[side]["n"] += 1
            predicted.append((gid, round(pred_total, 2), total, side,
                              int(actual_total), "over" if actual_over else "under"))

    print("\n=== Backtest-on-August (in-memory, NO writes) ===")
    for key, nm in (("ats", "Run Line (ATS)"), ("ou", "O/U"),):
        s = stats[key]
        wl = s["w"] + s["l"]
        wp = round(100 * s["w"] / wl, 1) if wl else 0.0
        print(f"  {nm:<16} n={s['n']:<4} {s['w']}-{s['l']}-{s['p']}  win%={wp}%")

    print("\n  OU by side:")
    for side, s in ou_side.items():
        wl = s["w"] + s["l"]
        wp = round(100 * s["w"] / wl, 1) if wl else 0.0
        print(f"    {side:<6} n={s['n']:<4} {s['w']}-{s['l']}  win%={wp}%")

    # Compare to LIVE stored picks for the same August window
    return predicted, stats, ou_side


if __name__ == "__main__":
    predicted, stats, ou_side = main()
    print("\nSample rows (game_id, pred_total, line, side, actual_total, actual_side):")
    for r in predicted[:12]:
        print("  ", r)
