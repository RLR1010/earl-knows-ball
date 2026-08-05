"""Validate NFL Week-1 prior-season seeding via load_data() (production path).

Calls NFLDataLoader.load_data() which runs the real team-stats + QB_SQL
(now with prior-season `_prev` fallback) + build_features(). Inspects whether
QB pre-game features for a Week-1 game are populated (>0) instead of 0.

Run: PYTHONPATH=backend venv/bin/python backend/scripts/_validate_prior_seed.py 401772510
"""
import os, sys
sys.path.insert(0, os.path.abspath("backend"))
os.environ.setdefault("PYTHONPATH", os.path.abspath("backend"))

import pandas as pd
from app.db_urls import SYNC_DATABASE_URL
from app.handicapping.nfl.data_loader import NFLDataLoader

GAME_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 401772510

dl = NFLDataLoader(db_url=SYNC_DATABASE_URL)

df = dl.load_data(seasons=[2024, 2025], include_upcoming=True)
print("load_data rows:", len(df), "cols:", len(df.columns))

row = df[df["game_id"] == GAME_ID]
if row.empty:
    print("NOTE: game not in model-feature output rows; listing QB feature columns present.")
    present = [c for c in df.columns if c.startswith(("home_qb", "away_qb", "qb_"))]
    print("QB model-feature columns:", present)
    raise SystemExit(0)
row = row.iloc[0]

qb_cols = [c for c in row.index if c.startswith(("home_qb", "away_qb", "qb_"))]
print(f"\n=== QB feature values for game {GAME_ID} ===")
nonzero = 0
for c in qb_cols:
    v = row[c]
    is_zero = (isinstance(v, (int, float)) and not pd.isna(v) and v == 0.0)
    if not is_zero:
        nonzero += 1
    print(f"  {c:36s} = {v if not pd.isna(v) else 'NaN'}{'  <-- ZERO' if is_zero else ''}")
print(f"\n({nonzero}/{len(qb_cols)} QB features non-zero)")

# team features too
team_cols = [c for c in row.index if c.startswith(("home_off_ypg", "home_ypp", "home_first_downs",
    "home_fourth_down_pct", "home_rz_trips", "home_ints_thrown", "home_win_streak",
    "away_def_ypg", "away_def_ypp", "away_off_ypg", "away_ints_thrown", "away_rz_trips",
    "home_off_pts_stddev_5", "away_def_pts_stddev_5"))]
print(f"\n=== Team previously-zero features for game {GAME_ID} ===")
for c in team_cols:
    if c in row.index:
        v = row[c]
        print(f"  {c:30s} = {v if not pd.isna(v) else 'NaN'}")

leftover = [c for c in row.index if "_prev" in c]
print(f"\nleftover _prev columns (should be none): {leftover}")
