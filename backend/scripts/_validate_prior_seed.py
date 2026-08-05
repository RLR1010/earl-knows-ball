"""Standalone validation for NFL Week-1 prior-season feature seeding (2026-08-04)."""
import os, sys, inspect
sys.path.insert(0, os.path.abspath("backend"))
os.environ.setdefault("PYTHONPATH", os.path.abspath("backend"))

import pandas as pd
import sqlalchemy as sa
from sqlalchemy import text
from app.db_urls import SYNC_DATABASE_URL, PSYCOPG2_DATABASE_URL
from app.handicapping.nfl.data_loader import NFLDataLoader, build_features

GAME_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 401772510
_sync_engine = sa.create_engine(SYNC_DATABASE_URL)

dl = NFLDataLoader(db_url=SYNC_DATABASE_URL)

raw_df = dl.load_games(game_ids=[GAME_ID], include_upcoming=True)
assert not raw_df.empty, f"no game {GAME_ID}"
season_val = int(raw_df.iloc[0]["season_year"])
print(f"game {GAME_ID} season_year={season_val}  home={raw_df.iloc[0].get('home_abbr')} away={raw_df.iloc[0].get('away_abbr')}")

full_raw_df = dl.load_games(seasons=[season_val - 1, season_val], include_upcoming=True)
print(f"loaded {len(full_raw_df)} game rows for seasons [{season_val-1}, {season_val}]")

CUM_SQL = text("""
    SELECT t.season, t.week, t.team_abbr,
        t.off_yds_r5          AS off_ypg,
        t.ypp_r5              AS ypp,
        t.pass_yds_r5         AS pass_ypg,
        t.rush_yds_r5         AS rush_ypg,
        t.pass_ypa_r5         AS pass_ypa,
        t.rush_ypa_r5         AS rush_ypa,
        t.turnover_margin_r5  AS turnover_diff,
        t.def_yds_r5          AS def_ypg,
        t.def_ypp_r5          AS def_ypp,
        t.def_pass_yds_r5     AS def_pass_ypg,
        t.def_rush_yds_r5     AS def_rush_ypg,
        t.first_downs_r5      AS first_downs,
        t.third_down_pct_r5   AS third_down_pct,
        t.fourth_down_pct_r5  AS fourth_down_pct,
        t.rz_trips_r5         AS rz_trips,
        t.rz_td_pct_r5        AS rz_td_pct,
        t.explosive_rate_r5   AS explosive_plays,
        t.three_and_out_rate_r5 AS three_and_outs,
        t.ints_thrown_r5      AS ints_thrown,
        t.def_first_downs_r5   AS def_first_downs,
        t.def_third_down_pct_r5 AS def_third_down_pct,
        t.def_fourth_down_pct_r5 AS def_fourth_down_pct,
        t.def_rz_trips_r5      AS def_rz_trips,
        t.def_rz_td_pct_r5    AS def_rz_td_pct,
        t.def_explosive_rate_r5 AS def_explosive_plays,
        t.def_three_and_outs_r5 AS def_three_and_outs,
        t.def_ints_thrown_r5   AS def_ints_thrown,
        t.epa_per_play_r5     AS off_epa_per_play,
        t.win_streak,
        t.off_pts_stddev_r5   AS off_pts_stddev_5,
        t.off_yds_stddev_r5   AS off_yds_stddev_5,
        c.rw_off_ppg,
        c.rw_off_ypg,
        c.adj_off_ppg,
        c.adj_off_ypg,
        t.def_epa_per_play_r5 AS def_epa_per_play,
        t.opp_pts_stddev_r5   AS def_pts_stddev_5,
        t.opp_yds_stddev_r5   AS def_yds_stddev_5,
        c.rw_def_ppg,
        c.rw_def_ypg,
        c.adj_def_ppg,
        c.adj_def_ypg,
        t.off_yardage_rank,
        t.def_yardage_rank,
        t.off_scoring_rank,
        t.def_scoring_rank,
        t.off_rushing_rank,
        t.def_rushing_rank,
        t.off_passing_rank,
        t.def_passing_rating_rank,
        t.feeds_into_game_id
    FROM nfl.team_rolling_stats t
    LEFT JOIN nfl.cumulative_game_stats c
        ON t.game_id = c.game_id AND t.team_abbr = c.team_abbr
    ORDER BY t.season, t.week, t.team_abbr
""")
with _sync_engine.connect() as conn:
    _ts_df = pd.read_sql(CUM_SQL, conn)
_qb_stats = pd.DataFrame()
print("ts_df rows:", len(_ts_df), "cols:", len(_ts_df.columns))

full_built_df = build_features(full_raw_df, team_stats=_ts_df, qb_stats=_qb_stats)
print("built rows:", len(full_built_df))

row = full_built_df[full_built_df["game_id"] == GAME_ID]
if row.empty:
    raise SystemExit("no row found for game")
row = row.iloc[0]

keys = [
    "home_off_ypg","home_ypp","home_first_downs","home_fourth_down_pct",
    "home_rz_trips","home_ints_thrown","home_off_pts_stddev_5","home_off_yds_stddev_5",
    "home_win_streak","home_turnover_diff_r5","home_third_down_pct","home_rz_td_pct",
    "away_def_ypg","away_def_ypp","away_def_first_downs","away_def_fourth_down_pct",
    "away_def_rz_trips","away_def_ints_thrown","away_def_pts_stddev_5","away_def_yds_stddev_5",
    "away_off_ypg","away_first_downs","away_ints_thrown","away_rz_trips","away_win_streak",
    "away_third_down_pct","away_fourth_down_pct",
]
print(f"\n=== Week-1 features for game {GAME_ID} (previously many were 0) ===")
for k in keys:
    if k in row.index:
        v = row[k]
        print(f"  {k:30s} = {v if not pd.isna(v) else 'NaN'}")
    else:
        print(f"  {k:30s} = <missing>")

import numpy as np
zero_num = [c for c in row.index if isinstance(row[c], (int, float)) and not pd.isna(row[c]) and row[c] == 0.0]
print(f"\n(numeric features exactly 0.0 for this game: {len(zero_num)})")
