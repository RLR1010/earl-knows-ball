#!/usr/bin/env python3
"""
DEFINITIVE A/B diagnostic: do the MLB backtest and live batch-predict load the
SAME data and produce the SAME feature vectors → SAME predictions?

This compares the two exact data-loading + feature-building code paths from
mlb_engine.py for the SAME target games, cell-by-cell on the model input
feature vectors, and then computes predictions with the same year models.

Non-destructive: read-only, nothing written to the DB.

Path A (live batch_predict_upcoming_games):
    all_historic = dl.load_games(status="FINAL", include_upcoming=False)
    target_games = dl.load_games(status=None, include_upcoming=True, game_ids=game_ids)
    dfA = build_features(pd.concat([all_historic, target_games]))

Path B (backtest _backtest_single_season, year matching the target game):
    games = dl.load_games(seasons=[2020..year], status="FINAL")
    dfB = build_features(games)
    dfB = dfB[dfB.season_year == year]

We then extract feature vectors for each target game from both frames and
compare them element-by-element, plus the resulting margin/total predictions.
"""

import asyncio
import os
import sys

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from app.db_urls import PSYCOPG2_DATABASE_URL  # noqa: E402
from app.handicapping.mlb.data_loader import get_data_loader, build_features  # noqa: E402
from app.handicapping.mlb.mlb_engine import (  # noqa: E402
    _get_features,
    _extract_feature_vector,
    _load_model_for_year,
    CURRENT_YEAR,
)

TARGET_DATES = ("2026-08-15", "2026-08-16")


def main():
    dl = get_data_loader()

    # ── Identify target games + their season years ──
    engine = __import__("sqlalchemy").create_engine(PSYCOPG2_DATABASE_URL, pool_pre_ping=True)
    with engine.connect() as c:
        rows = c.execute(__import__("sqlalchemy").text("""
            SELECT g.id, g.date, s.year AS season_year, g.status, ht.abbreviation, at.abbreviation
            FROM mlb.games g
            JOIN mlb.seasons s ON s.id = g.season_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.date::date IN :dates
            ORDER BY g.date, g.id
        """), {"dates": TARGET_DATES}).fetchall()
    engine.dispose()

    game_ids = [r[0] for r in rows]
    year_map = {str(r[0]): r[2] for r in rows}
    print(f"Found {len(game_ids)} target games:")
    for r in rows:
        print(f"  game {r[0]} {r[1]} {r[5]}@{r[4]} season={r[2]} status={r[3]}")

    # ── Path A: live batch data load ──
    print("\n[Path A] loading live-batch-style data...")
    all_historic = dl.load_games(status="FINAL", include_upcoming=False)
    target_games = dl.load_games(status=None, include_upcoming=True, game_ids=game_ids)
    dfA = build_features(pd.concat([all_historic, target_games], ignore_index=True))
    print(f"  historic={all_historic.shape} target={target_games.shape} dfA={dfA.shape}")

    # ── Path B: backtest data load for each target year ──
    # Backtest processes one year at a time via _backtest_single_season(year).
    # For each distinct season among targets, load that backtest frame.
    dfB_by_year = {}
    for year in sorted(set(year_map.values())):
        print(f"[Path B] building backtest frame for year {year}...")
        games = dl.load_games(seasons=list(range(2020, year + 1)), status="FINAL")
        fb = build_features(games)
        fb = fb[fb["season_year"] == year].copy()
        dfB_by_year[year] = fb
        print(f"  framefor year {year}: {fb.shape}")

    # ── Compare feature vectors per target game ──
    ats_model = _load_model_for_year("ats", CURRENT_YEAR)
    ou_model = _load_model_for_year("ou", CURRENT_YEAR)
    ats_cols = _get_features()["ats"]
    ou_cols = _get_features()["ou"]

    # Stored api predictions for these games
    engine2 = __import__("sqlalchemy").create_engine(PSYCOPG2_DATABASE_URL, pool_pre_ping=True)
    with engine2.connect() as c:
        stored = c.execute(__import__("sqlalchemy").text("""
            SELECT gp.game_id, gp.source, gp.predicted_margin, gp.predicted_total,
                   gp.run_line_pick, gp.ou_pick, gp.ml_pick, gp.created_at
            FROM mlb.game_predictions gp
            WHERE gp.game_id IN :gids
            ORDER BY gp.game_id
        """), {"gids": tuple(game_ids)}).fetchall()
    engine2.dispose()
    stored_map = {str(x[0]): x for x in stored}

    print("\n=== PER-GAME COMPARISON ===")
    for gid, gdate, yr, status, ha, aa in rows:
        gid_s = str(gid)
        ra = dfA[dfA["game_id"].astype(str) == gid_s]
        rf = dfB_by_year.get(int(yr))
        rb = rf[rf["game_id"].astype(str) == gid_s] if rf is not None else pd.DataFrame()

        if ra.empty:
            print(f"\n  game {gid} {gdate} {aa}@{ha}: NOT in Path A frame !!")
            continue
        if rb.empty:
            print(f"\n  game {gid} {gdate} {aa}@{ha}: NOT in Path B frame (season {yr}) !!")
            continue

        rowA = ra.iloc[0]
        rowB = rb.iloc[0]

        # Feature vectors
        va = _extract_feature_vector(rowA, "ats")
        vb = _extract_feature_vector(rowB, "ats")
        voa = _extract_feature_vector(rowA, "ou")
        vob = _extract_feature_vector(rowB, "ou")

        def _feat_diff(v1, v2, cols, label):
            if v1 is None or v2 is None:
                return [f"{label}: one vector None (A={v1 is not None}, B={v2 is not None})"]
            if v1.shape != v2.shape:
                return [f"{label}: shape differs A={v1.shape} B={v2.shape}"]
            diffs = []
            for i, (a, b) in enumerate(zip(v1, v2)):
                if abs(float(a) - float(b)) > 1e-9:
                    diffs.append(f"  {label}.{cols[i]}: A={float(a):.6f} B={float(b):.6f} Δ={float(a)-float(b):+.6f}")
            return diffs

        ats_diff = _feat_diff(va, vb, ats_cols, "ats")
        ou_diff = _feat_diff(voa, vob, ou_cols, "ou")

        # Predictions (same models)
        pa = float(ats_model.predict(va[np.newaxis, :])[0]) if va is not None else None
        pb = float(ats_model.predict(vb[np.newaxis, :])[0]) if vb is not None else None
        ta = float(ou_model.predict(voa[np.newaxis, :])[0]) if voa is not None else None
        tb = float(ou_model.predict(vob[np.newaxis, :])[0]) if vob is not None else None

        # line values used (spread/total)
        spreadA = rowA.get("spread"); spreadB = rowB.get("spread")
        ouA = rowA.get("ou_line"); ouB = rowB.get("ou_line")

        # stored row
        st = stored_map.get(gid_s, None)
        sm = st[2] if st else None
        st2 = st[3] if st else None

        print(f"\n  game {gid} {gdate} {aa}@{ha} (season {yr}, now status {status})")
        print(f"    ATS feat diffs: {len(ats_diff)}   OU feat diffs: {len(ou_diff)}")
        for d in ats_diff[:12]:
            print(d)
        for d in ou_diff[:12]:
            print(d)
        print(f"    pred_margin: A={pa:.6f} B={pb:.6f} {'✅ SAME' if pa==pb else '❌ DIFF'}")
        print(f"    pred_total : A={ta:.6f} B={tb:.6f} {'✅ SAME' if ta==tb else '❌ DIFF'}")
        print(f"    spread     : A={spreadA} B={spreadB}  ou_line A={ouA} B={ouB}")
        if st:
            print(f"    STORED src={st[1]} margin={sm} total={st2} created={st[7]}  "
                  f"{'✅ matches model' if (sm is not None and abs(float(sm)-pa)<5e-3 and st2 is not None and abs(float(st2)-ta)<5e-3) else '❌ differs from model'}")

    print("\nDONE.")


if __name__ == "__main__":
    main()
