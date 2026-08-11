"""DECISIVE test: does the SAME game produce DIFFERENT feature vectors + model
output depending on whether fetched as UPCOMING (live path) vs FINAL (backtest)?

Optimized: load the historic FINAL frame ONCE, then for each test game fetch its
row as (a) part of the FINAL frame (backtest) and (b) an 'upcoming' target row
appended to the historic frame (live) — both go through the same build_features.

No DB writes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import numpy as np
import pandas as pd

from app.handicapping.mlb.data_loader import get_data_loader, build_features
from app.handicapping.mlb.mlb_engine import _load_model_for_year, _extract_feature_vector

YEAR = 2026
TEST_GAMES = [48824, 48826, 48839, 48772, 48790, 48825, 48856, 48834, 48848]


def main():
    dl = get_data_loader()
    ou = _load_model_for_year("ou", YEAR)
    ats = _load_model_for_year("ats", YEAR)

    print("Loading historic FINAL frame (2020-2026) once...", flush=True)
    historic = dl.load_games(seasons=list(range(2020, YEAR + 1)), status="FINAL")
    df_final = build_features(historic)
    print(f"  historic rows={len(df_final)}", flush=True)

    # backtest frame index by game_id
    final_by_id = {str(r["game_id"]): r for _, r in df_final.iterrows()}
    gids_set = {str(g) for g in TEST_GAMES}

    # upcoming frame: historic + each target as upcoming, ONE build_features call (all at once)
    targets = dl.load_games(seasons=[YEAR], status=None, include_upcoming=True, game_ids=TEST_GAMES)
    df_live = build_features(pd.concat([historic, targets], ignore_index=True))
    live_by_id = {str(r["game_id"]): r for _, r in df_live.iterrows()}

    print(f"\n{'gid':>6} {'total':>5} | {'final_pt':>8} {'live_pt':>8} {'dPT':>5} | {'final_m':>7} {'live_m':>7} {'dM':>5} | {'final':>6} {'live':>6}")
    print("  " + "-" * 78)
    for gid in TEST_GAMES:
        g = str(gid)
        fr, lr = final_by_id.get(g), live_by_id.get(g)
        if fr is None or lr is None:
            print(f"{gid:>6} missing fr={fr is not None} lr={lr is not None}")
            continue
        total_f = fr.get("ou_line", fr.get("over_under"))
        total_l = lr.get("ou_line", lr.get("over_under"))
        if pd.isna(total_f) or total_f is None:
            total_f = 8.5
        if pd.isna(total_l) or total_l is None:
            total_l = 8.5
        fo_f = _extract_feature_vector(fr, "ou"); fo_l = _extract_feature_vector(lr, "ou")
        fa_f = _extract_feature_vector(fr, "ats"); fa_l = _extract_feature_vector(lr, "ats")
        pf = float(ou.predict(np.asarray(fo_f)[np.newaxis, :])[0]) if fo_f is not None else None
        pl = float(ou.predict(np.asarray(fo_l)[np.newaxis, :])[0]) if fo_l is not None else None
        mf = float(ats.predict(np.asarray(fa_f)[np.newaxis, :])[0]) if fa_f is not None else None
        ml = float(ats.predict(np.asarray(fa_l)[np.newaxis, :])[0]) if fa_l is not None else None
        dp = (abs(pf - pl) if (pf is not None and pl is not None) else None)
        dm = (abs(mf - ml) if (mf is not None and ml is not None) else None)
        sf = ("OVER" if pf > float(total_f) else "UNDER") if pf is not None else "-"
        sl = ("OVER" if pl > float(total_l) else "UNDER") if pl is not None else "-"
        flag = "  <-- DIFFERS" if (dp is not None and dp > 0.05) else ""
        print(f"{gid:>6} {total_f:>5} | {('%.2f'%pf) if pf is not None else '-':>8} "
              f"{('%.2f'%pl) if pl is not None else '-':>8} {('%.3f'%dp) if dp is not None else '-':>5} | "
              f"{('%.2f'%mf) if mf is not None else '-':>7} {('%.2f'%ml) if ml is not None else '-':>7} "
              f"{('%.3f'%dm) if dm is not None else '-':>5} | {sf:>6} {sl:>6}{flag}")


if __name__ == "__main__":
    main()
