"""
NBA XGBoost OU Backtester — predicts total points (home_score + away_score).

Dedicated O/U model with features tuned for NBA total prediction:
- Rolling scoring (both teams, recent windows)
- Pace proxies (possessions per game)
- Shooting efficiency (FG%, 3PT%, FT%)
- Team total momentum (recent totals, over frequency)
- Market anchors (closing total line)
- Situational (back-to-backs, travel, altitude, rest)

Usage:
    docker exec earl-knows-football-api-1 python -m app.handicapping.nba.nba_xgb_model_ou --test-year 2023
    docker exec earl-knows-football-api-1 python -m app.handicapping.nba.nba_xgb_model_ou --mode all
"""
import asyncio
import pickle
from typing import Optional
import logging
import warnings
import json
import math
from datetime import datetime, date

warnings.filterwarnings("ignore")
from pathlib import Path
import numpy as np
import asyncpg
import pandas as pd
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from sklearn.metrics import mean_absolute_error
import xgboost as xgb

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("earl.nba_xgb_ou")
log = logger.info

import os
DB = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DSN = DB.replace("+asyncpg", "")

COORDS = {
    "ATL": (33.8, -84.4), "BKN": (40.7, -73.9), "BOS": (42.4, -71.1),
    "CHA": (35.2, -80.8), "CHI": (41.9, -87.6), "CLE": (41.5, -81.7),
    "DAL": (32.8, -96.8), "DEN": (39.7, -104.9), "DET": (42.3, -83.0),
    "GSW": (37.8, -122.4), "HOU": (29.8, -95.4), "IND": (39.8, -86.2),
    "LAC": (34.0, -118.3), "LAL": (34.0, -118.3), "MEM": (35.1, -90.0),
    "MIA": (25.8, -80.2), "MIL": (43.0, -87.9), "MIN": (45.0, -93.3),
    "NJ": (40.7, -73.9), "NOP": (29.9, -90.1), "NYK": (40.8, -73.9),
    "OKC": (35.5, -97.5), "ORL": (28.5, -81.4), "PHI": (39.9, -75.2),
    "PHX": (33.4, -112.1), "POR": (45.5, -122.7), "SAC": (38.6, -121.5),
    "SAS": (29.4, -98.5), "SEA": (47.6, -122.3), "TOR": (43.6, -79.4),
    "UTA": (40.8, -112.0), "WAS": (38.9, -77.0),
}

ALTITUDE = {
    "ATL": 1050, "BKN": 10, "BOS": 141, "CHA": 748, "CHI": 579,
    "CLE": 653, "DAL": 430, "DEN": 5280, "DET": 585, "GSW": 52,
    "HOU": 43, "IND": 718, "LAC": 262, "LAL": 262, "MEM": 259,
    "MIA": 6, "MIL": 617, "MIN": 815, "NJ": 10, "NOP": 7,
    "NYK": 10, "OKC": 1195, "ORL": 82, "PHI": 28, "PHX": 1086,
    "POR": 50, "SAC": 23, "SAS": 636, "SEA": 141, "TOR": 249,
    "UTA": 4220, "WAS": 10,
}

# ── OU Feature Set ──
# NBA-specific: pace, efficiency, rest effects
TOTAL_OU_FEATURES = [
    "ou_movement",
    "h_ppg5", "a_ppg5",
    "h_ppg10", "a_ppg10",
    "h_oppg10", "a_oppg10",
    "h_fg_pct10", "a_fg_pct10",
    "h_3pt_pct10", "a_3pt_pct10",
    "opening_ou",
    "over_pct_h_r5", "over_pct_a_r5",
    "travel_miles",
    "h_home_ppg", "a_away_ppg",
    "h_winpct", "a_winpct",
    "is_div", "is_conf",
    "tz_diff",
    "altitude_diff",
    "home_b2b", "away_b2b",
    "rest_h", "rest_a",
    "is_playoff",
]

FEATURES_TRAINING = TOTAL_OU_FEATURES.copy()


def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def load_data(engine):
    """Load all completed NBA games with scores, detailed stats, and betting lines."""
    log("Loading games...")
    async with engine.connect() as conn:
        r = await conn.execute(text("""
            SELECT g.id, s.year, g.date::date as game_date,
                   ht.abbreviation as ha, at.abbreviation as aa,
                   g.home_score, g.away_score,
                   (g.home_score + g.away_score) as total,
                   (g.home_score - g.away_score) as margin,
                   g.game_type,
                   g.home_team_id as htid, g.away_team_id as atid,
                   ht.conference as hconf, at.conference as aconf,
                   ht.division as hdiv, at.division as adiv,
                   g.home_field_goals_made as h_fgm,
                   g.home_field_goals_attempted as h_fga,
                   g.home_three_points_made as h_3pm,
                   g.home_three_points_attempted as h_3pa,
                   g.home_rebounds as h_reb,
                   g.away_field_goals_made as a_fgm,
                   g.away_field_goals_attempted as a_fga,
                   g.away_three_points_made as a_3pm,
                   g.away_three_points_attempted as a_3pa,
                   g.away_rebounds as a_reb
            FROM nba.games g
            JOIN nba.seasons s ON s.id = g.season_id
            JOIN nba.teams ht ON ht.id = g.home_team_id
            JOIN nba.teams at ON at.id = g.away_team_id
            WHERE g.home_score IS NOT NULL AND g.away_score IS NOT NULL
            ORDER BY s.year, g.date, g.id
        """))
        games = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])

        log("Loading betting lines...")
        r = await conn.execute(text("""
            SELECT
                game_id,
                over_under,
                home_moneyline, away_moneyline,
                home_implied_probability, away_implied_probability,
                spread
            FROM nba.betting_lines_consolidated
            WHERE over_under IS NOT NULL
        """))
        lines = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])

    log(f"  Games: {len(games)} ({games.year.min()}-{games.year.max()})")
    log(f"  Lines with OU: {lines.over_under.notna().sum()}")

    games = games.rename(columns={"id": "game_id"})
    df = games.merge(lines, on="game_id", how="left")
    log(f"  Merged: {len(df)} rows")
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build OU-optimized features for NBA."""
    log("Building team-game table...")

    rows = []
    for _, g in df.iterrows():
        rows.append({
            "game_id": g["game_id"], "year": g["year"], "game_date": g["game_date"],
            "team": g["ha"], "opp": g["aa"],
            "pf": g["home_score"], "pa": g["away_score"],
            "total": g["total"], "is_home": 1, "margin": g["margin"],
            "fgm": g.h_fgm, "fga": g.h_fga,
            "pm3": g.h_3pm, "pa3": g.h_3pa,
            "reb": g.h_reb,
            "conference": g.hconf, "division": g.hdiv,
            "game_type": g.game_type,
        })
        rows.append({
            "game_id": g["game_id"], "year": g["year"], "game_date": g["game_date"],
            "team": g["aa"], "opp": g["ha"],
            "pf": g["away_score"], "pa": g["home_score"],
            "total": g["total"], "is_home": 0, "margin": -g["margin"],
            "fgm": g.a_fgm, "fga": g.a_fga,
            "pm3": g.a_3pm, "pa3": g.a_3pa,
            "reb": g.a_reb,
            "conference": g.aconf, "division": g.adiv,
            "game_type": g.game_type,
        })

    tg = pd.DataFrame(rows).sort_values(["team", "game_date", "game_id"]).reset_index(drop=True)
    tg["game_date_dt"] = pd.to_datetime(tg["game_date"])

    # Rolling stats
    log("  Rolling scoring stats...")
    for window in [5, 10]:
        tg[f"ppg{window}"] = (
            tg.groupby("team")["pf"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )
        tg[f"oppg{window}"] = (
            tg.groupby("team")["pa"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )
        tg[f"total_avg_r{window}"] = (
            tg.groupby("team")["total"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )

    # Shooting efficiency (rolling 10)
    log("  Shooting efficiency...")
    for window in [10]:
        tg[f"fg_pct{window}"] = (
            tg.groupby("team")["fgm"].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).sum()
            ) / tg.groupby("team")["fga"].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).sum()
            )
        )
        tg[f"3pt_pct{window}"] = (
            tg.groupby("team")["pm3"].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).sum()
            ) / tg.groupby("team")["pa3"].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).sum()
            )
        )

    # Rest days
    log("  Rest days...")
    tg["prev_date"] = tg.groupby("team")["game_date_dt"].shift(1)
    tg["rest"] = (tg["game_date_dt"] - tg["prev_date"]).dt.days.fillna(1).clip(0, 30)

    # Win percentage
    tg["team_win"] = (tg["pf"] > tg["pa"]).astype(int)
    tg["winpct"] = tg.groupby(["team", "year"])["team_win"].transform(
        lambda x: x.shift(1).expanding().mean()
    ).fillna(0.5)

    # Home/road splits
    log("  Home/road splits...")
    split_rf = tg.groupby(["team", "is_home"])["pf"].transform(
        lambda x: x.shift(1).expanding().mean()
    )
    tg["h_home_ppg"] = split_rf.where(tg["is_home"] == 1, 105.0)
    tg["a_away_ppg"] = split_rf.where(tg["is_home"] == 0, 105.0)

    # Rejoin to game level
    log("  Rejoining to game level...")
    h = tg[tg["is_home"] == 1][
        ["game_id", "team", "ppg5", "ppg10", "oppg5", "oppg10",
         "total_avg_r5", "total_avg_r10", "rest",
         "fg_pct10", "3pt_pct10",
         "winpct", "h_home_ppg"]
    ].rename(columns={
        "team": "ha",
        "ppg5": "h_ppg5", "ppg10": "h_ppg10",
        "oppg5": "h_oppg5", "oppg10": "h_oppg10",
        "rest": "rest_h",
        "fg_pct10": "h_fg_pct10", "3pt_pct10": "h_3pt_pct10",
        "winpct": "h_winpct",
        "total_avg_r5": "h_total_avg_r5",
        "total_avg_r10": "h_total_avg_r10",
    })

    a = tg[tg["is_home"] == 0][
        ["game_id", "team", "ppg5", "ppg10", "oppg5", "oppg10",
         "total_avg_r5", "total_avg_r10", "rest",
         "fg_pct10", "3pt_pct10",
         "winpct", "a_away_ppg"]
    ].rename(columns={
        "team": "aa",
        "ppg5": "a_ppg5", "ppg10": "a_ppg10",
        "oppg5": "a_oppg5", "oppg10": "a_oppg10",
        "rest": "rest_a",
        "fg_pct10": "a_fg_pct10", "3pt_pct10": "a_3pt_pct10",
        "winpct": "a_winpct",
        "total_avg_r5": "a_total_avg_r5",
        "total_avg_r10": "a_total_avg_r10",
    })

    feats = h.merge(a, on="game_id")
    div_map = df.set_index("game_id")[["hdiv", "adiv", "hconf", "aconf"]].to_dict("index")
    feats["is_div"] = feats["game_id"].apply(
        lambda gid: 1 if gid in div_map and div_map[gid]["hdiv"] == div_map[gid]["adiv"] else 0
    )
    feats["is_conf"] = feats["game_id"].apply(
        lambda gid: 1 if gid in div_map and div_map[gid]["hconf"] == div_map[gid]["aconf"] else 0
    )

    # Back-to-back indicators
    feats["home_b2b"] = (feats["rest_h"] == 0).astype(int)
    feats["away_b2b"] = (feats["rest_a"] == 0).astype(int)

    # Add original game data
    orig_cols = df[["game_id", "year", "game_date", "total", "margin",
                     "over_under", "home_score", "away_score",
                     "game_type"]].copy()
    feats = feats.merge(orig_cols, on="game_id")
    feats["actual_total"] = feats["total"]

    # Over frequency
    log("  Computing over frequency...")
    feats["game_date_dt"] = pd.to_datetime(feats["game_date"])
    ou_line_map = df.set_index("game_id")["over_under"].fillna(220.0).to_dict()
    tg["ou_line_game"] = tg["game_id"].map(ou_line_map)
    tg["game_over"] = (tg["total"] > tg["ou_line_game"]).astype(int)
    for window in [5, 10]:
        tg[f"over_pct_r{window}"] = (
            tg.groupby("team")["game_over"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )

    over_pct_h = tg[tg["is_home"] == 1].set_index("game_id")[["over_pct_r5", "over_pct_r10"]]
    over_pct_a = tg[tg["is_home"] == 0].set_index("game_id")[["over_pct_r5", "over_pct_r10"]]

    feats = feats.merge(
        over_pct_h.rename(columns={"over_pct_r5": "over_pct_h_r5", "over_pct_r10": "over_pct_h_r10"}),
        left_on="game_id", right_index=True, how="left"
    )
    feats = feats.merge(
        over_pct_a.rename(columns={"over_pct_r5": "over_pct_a_r5", "over_pct_r10": "over_pct_a_r10"}),
        left_on="game_id", right_index=True, how="left"
    )

    # Implied totals
    feats["implied_total_10"] = (feats["h_ppg10"].fillna(105) + feats["a_ppg10"].fillna(105) +
                                  feats["h_oppg10"].fillna(105) + feats["a_oppg10"].fillna(105)) / 2
    feats["closing_ou"] = feats["over_under"].fillna(220.0)
    feats["opening_ou"] = feats.get("opening_ou", feats["closing_ou"])
    feats["ou_movement"] = feats["closing_ou"] - feats["opening_ou"]

    # Travel
    feats["travel_miles"] = feats.apply(
        lambda r: haversine(
            *COORDS.get(r["aa"], (0, 0)), *COORDS.get(r["ha"], (0, 0))
        ),
        axis=1,
    )
    feats.loc[feats["travel_miles"] < 50, "travel_miles"] = 0

    # Altitude
    feats["altitude_diff"] = feats["ha"].map(ALTITUDE) - feats["aa"].map(ALTITUDE)

    # Playoff flag
    feats["is_playoff"] = (feats["game_type"].isin(["P", "PO", "PS"])).astype(int)

    # Filter to games with OU line
    pre_filter = len(feats)
    feats = feats[feats["over_under"].notna()].copy()
    log(f"  Filtered to {len(feats)} games with O/U line (dropped {pre_filter - len(feats)} without)")

    return feats


async def run_backtest(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    test_year: int = 2023,
    train_years: list[int] = None,
    xgb_params: dict = None,
) -> dict:
    """Run XGBoost OU backtest for a single year."""
    if train_years is None:
        train_years = [y for y in range(test_year - 5, test_year)]

    if xgb_params is None:
        xgb_params = {
            "n_estimators": 300,
            "max_depth": 5,
            "learning_rate": 0.04,
            "subsample": 0.7,
            "colsample_bytree": 0.7,
            "reg_alpha": 1.0,
            "reg_lambda": 2.0,
            "min_child_weight": 3,
            "random_state": 42,
            "n_jobs": -1,
            "verbosity": 0,
        }

    tr_mask = feats["year"].isin(train_years)
    te_mask = feats["year"] == test_year
    tr = feats[tr_mask].reset_index(drop=True)
    te = feats[te_mask].sort_values(["game_date"]).reset_index(drop=True)

    log(f"  Train: {len(tr)} games ({train_years})")
    log(f"  Test:  {len(te)} games ({test_year})")

    if len(tr) < 50 or len(te) < 10:
        log("  ⚠ Not enough data, skipping")
        return {"error": "insufficient_data"}

    X_te = te[FEATURES_TRAINING].fillna(0).astype(np.float32)
    target_te = te["actual_total"].values

    X_tr = tr[FEATURES_TRAINING].fillna(0).astype(np.float32)
    target_tr = tr["actual_total"].values

    # Clip outliers
    q01, q99 = np.percentile(target_tr, [1, 99])
    clip_mask = (target_tr >= q01) & (target_tr <= q99)
    tr = tr[clip_mask].reset_index(drop=True)
    X_tr = tr[FEATURES_TRAINING].fillna(0).astype(np.float32)
    target_tr = tr["actual_total"].values

    # Time-weighted training
    n_tr = len(tr)
    w = np.ones(n_tr)
    for i in range(n_tr):
        s = tr.at[tr.index[i], "year"]
        years_back = test_year - s
        if years_back <= 1:
            w[i] = 4.0
        elif years_back <= 2:
            w[i] = 3.0
        elif years_back <= 3:
            w[i] = 2.0
        elif years_back <= 5:
            w[i] = 1.5

    model = xgb.XGBRegressor(**xgb_params)
    model.fit(X_tr, target_tr, sample_weight=w, verbose=False)

    te = te.copy()
    te["pred_total"] = model.predict(X_te)
    te["pred_error"] = te["actual_total"] - te["pred_total"]
    mae = mean_absolute_error(te["actual_total"].values, te["pred_total"].values)
    err_mean = te["pred_error"].mean()
    err_std = te["pred_error"].std()

    # O/U evaluation
    has_ou = te["over_under"].notna()
    if has_ou.any():
        ou_df = te[has_ou].copy()
        actual_total = ou_df["actual_total"].values
        pred_total = ou_df["pred_total"].values
        closing_ou = ou_df["over_under"].values

        actual_over = actual_total > closing_ou
        actual_push = actual_total == closing_ou
        pred_over = pred_total > closing_ou

        ou_correct = int(((pred_over == actual_over) & ~actual_push).sum())
        ou_incorrect = int(((pred_over != actual_over) & ~actual_push).sum())
        ou_pushes = int(actual_push.sum())
    else:
        ou_correct = 0
        ou_incorrect = 0
        ou_pushes = 0

    # Monthly breakdown
    monthly = []
    feats["game_date_dt"] = pd.to_datetime(feats["game_date"])
    te["month"] = pd.to_datetime(te["game_date"]).dt.month
    for m in sorted(te["month"].unique()):
        sub = te[te["month"] == m]
        if len(sub) < 5:
            continue
        mae_m = mean_absolute_error(sub["actual_total"], sub["pred_total"])
        monthly.append({
            "month": int(m),
            "games": int(len(sub)),
            "mae": float(round(mae_m, 2)),
        })

    # Feature importance
    imp = pd.DataFrame({
        "feature": list(FEATURES_TRAINING),
        "importance": list(model.feature_importances_),
    }).sort_values("importance", ascending=False)

    ou_total = ou_correct + ou_incorrect

    result = {
        "test_year": test_year,
        "train_years": train_years,
        "feature_set": "nba_ou",
        "total_games": int(len(te)),
        "mae": round(mae, 2),
        "err_mean": round(err_mean, 2),
        "err_std": round(err_std, 2),
        "within_5": float(round((abs(te["pred_error"]) < 5).mean(), 3)),
        "within_10": float(round((abs(te["pred_error"]) < 10).mean(), 3)),
        "ou": {
            "correct": ou_correct,
            "incorrect": ou_incorrect,
            "pushes": ou_pushes,
            "total": ou_total,
            "pct": float(round(100 * ou_correct / max(ou_total, 1), 1)),
        },
        "monthly": monthly,
        "feature_importance": [
            {"feature": str(r["feature"]), "importance": float(round(float(r["importance"]), 4))}
            for _, r in imp.iterrows()
        ],
    }

    print_ou_summary(result, te)

    return result


def print_ou_summary(result: dict, te: pd.DataFrame):
    print(f"\n{'='*62}")
    print(f"NBA O/U BACKTEST — {result['test_year']} Season")
    print(f"Features: {result['feature_set']} ({len(result['feature_importance'])} feats)")
    print(f"Train: {result['train_years']}")
    print(f"{'='*62}")

    print(f"\n📊 TOTAL POINTS PREDICTION")
    print(f"  MAE:       {result['mae']:.2f} pts")
    print(f"  Bias:      {result['err_mean']:+.2f} pts")
    print(f"  Std Dev:   {result['err_std']:.2f} pts")
    print(f"  ±5 pts:    {result['within_5']:.1%}")
    print(f"  ±10 pts:   {result['within_10']:.1%}")

    print(f"\n🎲 OVER/UNDER PERFORMANCE")
    ou = result["ou"]
    print(f"  O/U:       {ou['correct']:4d}-{ou['incorrect']:4d}-{ou['pushes']}  ({ou['pct']:.1f}%)  [{ou['total']} games]")

    if result["monthly"]:
        print(f"\n📅 MONTHLY BREAKDOWN")
        print(f"  {'Month':>6s}  {'Games':>5s}  {'MAE':>5s}")
        for m in result["monthly"]:
            print(f"  {m['month']:>6d}  {m['games']:>5d}  {m['mae']:>5.2f}")

    print(f"\n🔑 TOP FEATURES")
    for i, fi in enumerate(result["feature_importance"][:12]):
        bar = "█" * int(fi["importance"] * 100)
        print(f"  {i+1:2d}. {fi['feature']:>20s}: {fi['importance']:.4f} {bar}")
    print()


async def run_all_years(
    test_years: list[int] = None,
    train_from: int = 2011,
):
    """Run OU backtests across multiple years."""
    if test_years is None:
        test_years = [2021, 2022, 2023, 2024, 2025, 2026]

    t0 = datetime.now()
    engine = create_async_engine(DB)

    df = await load_data(engine)
    log(f"\nBuilding features...")
    feats = build_features(df)
    log(f"Feature table: {len(feats)} rows, {len(feats.columns)} columns")

    all_results = []

    for year in test_years:
        log(f"\n{'─'*62}")
        log(f"Testing year={year}")
        log(f"{'─'*62}")
        result = await run_backtest(df, feats, test_year=year)
        if "error" not in result:
            all_results.append(result)

    await engine.dispose()

    print(f"\n{'='*62}")
    print("NBA O/U BACKTEST — ALL YEARS")
    print(f"{'='*62}")
    print(f"\n{'Year':>4s}  {'Games':>5s}  {'MAE':>5s}  {'OU%':>6s}")
    print("─" * 42)
    total_c = 0
    total_i = 0
    total_g = 0
    for r in sorted(all_results, key=lambda x: x["test_year"]):
        ou = r["ou"]
        total_c += ou["correct"]
        total_i += ou["incorrect"]
        total_g += ou["total"]
        print(f"  {r['test_year']:>4d}  {r['total_games']:>5d}  {r['mae']:>5.2f}  {ou['pct']:>5.1f}%")

    tp = total_c + total_i
    print(f"  {'─'*38}")
    print(f"  {'TOTAL':>4s}  {total_g:>5d}  {'':>5s}  {round(100*total_c/max(tp,1),1):>5.1f}%")

    log(f"\nTotal time: {datetime.now() - t0}")


async def run_single(test_year: int = 2023):
    """Run a single OU backtest for quick iteration."""
    engine = create_async_engine(DB)
    df = await load_data(engine)
    feats = build_features(df)
    await run_backtest(df, feats, test_year=test_year)
    await engine.dispose()


# ── Model management & inference (imported by nba_engine.py) ──────────

OU_MODEL_PATH = Path("/app/data/nba_ou_model_prod.pkl")
_ou_model = None


def set_model_path(path: str):
    global OU_MODEL_PATH, _ou_model
    OU_MODEL_PATH = Path(path)
    _ou_model = None


def _load_ou_model():
    global _ou_model
    if _ou_model is not None:
        return _ou_model
    if not OU_MODEL_PATH.exists():
        raise FileNotFoundError(f"OU model not found at {OU_MODEL_PATH}")
    with open(OU_MODEL_PATH, "rb") as f:
        payload = pickle.load(f)
    model = payload["model"] if isinstance(payload, dict) else payload
    _ou_model = model
    log(f"Loaded OU model ({len(FEATURES_TRAINING)} features)")
    return _ou_model


async def predict_ou(game_id: int, home_abbr: str, away_abbr: str,
                      yr: int, game_date: str,
                      home_stats, away_stats,
                      line_obj,
                      conn: Optional[asyncpg.Connection] = None) -> tuple[Optional[float], float]:
    """Predict total points for one NBA game."""
    try:
        model = _load_ou_model()
    except FileNotFoundError:
        logger.warning("OU model not found at %s", OU_MODEL_PATH)
        return None, 0.0

    gd_obj = date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
    _close_conn = False
    if conn is None:
        conn = await asyncpg.connect(DSN)
        _close_conn = True
    try:
        async def _avg(abbr, sel, lim, extra="", default=105.0):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.v) FROM (
                    SELECT {sel} as v FROM nba.games g
                    JOIN nba.seasons s ON s.id=g.season_id
                    JOIN nba.teams ht ON ht.id=g.home_team_id
                    JOIN nba.teams at ON at.id=g.away_team_id
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL {extra}
                    ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else default

        # Team scoring
        h_ppg5 = await _avg(home_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END", 5)
        a_ppg5 = await _avg(away_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END", 5)
        h_ppg10 = await _avg(home_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END", 10)
        a_ppg10 = await _avg(away_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END", 10)
        h_oppg10 = await _avg(home_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END", 10)
        a_oppg10 = await _avg(away_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END", 10)

        # Shooting efficiency
        async def _shooting(abbr, stat, side, lim=10):
            col_prefix = "home" if side == "h" else "away"
            tbl = "ht" if side == "h" else "at"
            if stat == "fg":
                num = f"g.{col_prefix}_field_goals_made"
                den = f"g.{col_prefix}_field_goals_attempted"
            else:
                num = f"g.{col_prefix}_three_points_made"
                den = f"g.{col_prefix}_three_points_attempted"
            r = await conn.fetchrow(f"""
                SELECT SUM(sub.num)::float / NULLIF(SUM(sub.den), 0) FROM (
                    SELECT {num} as num, {den} as den FROM nba.games g
                    JOIN nba.seasons s ON s.id=g.season_id
                    JOIN nba.teams {tbl} ON {tbl}.id=g.{'home' if side=='h' else 'away'}_team_id
                    WHERE {tbl}.abbreviation=$1 AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL
                    ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.45

        h_fg_pct10 = await _shooting(home_abbr, "fg", "h")
        a_fg_pct10 = await _shooting(away_abbr, "fg", "a")
        h_3pt_pct10 = await _shooting(home_abbr, "3pt", "h")
        a_3pt_pct10 = await _shooting(away_abbr, "3pt", "a")

        # OU movement
        r = await conn.fetchrow(
            "SELECT over_under FROM nba.betting_lines_consolidated WHERE game_id=$1 LIMIT 1", game_id)
        co = float(r['over_under']) if (r and r['over_under'] is not None) else 220.0
        oo = co  # No opening_total column — assume closing = opening if no movement data

        # Rest
        async def _last(abbr):
            r = await conn.fetchrow("""
                SELECT MAX(g.date::date) FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
                WHERE (g.home_team_id=(SELECT id FROM nba.teams WHERE abbreviation=$1)
                    OR g.away_team_id=(SELECT id FROM nba.teams WHERE abbreviation=$1))
                  AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                  AND g.home_score IS NOT NULL
            """, abbr, yr, gd_obj)
            return r[0] if r and r[0] else None

        hl, al = await _last(home_abbr), await _last(away_abbr)
        gd = date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
        rest_h = (gd - hl).days if (gd and hl) else 3
        rest_a = (gd - al).days if (gd and al) else 3

        # Home/road splits
        async def _homescore(abbr, lim=999):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.v) FROM (
                    SELECT g.home_score as v FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
                    JOIN nba.teams ht ON ht.id=g.home_team_id
                    WHERE ht.abbreviation=$1 AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 105.0

        async def _awayscore(abbr, lim=999):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.v) FROM (
                    SELECT g.away_score as v FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
                    JOIN nba.teams at ON at.id=g.away_team_id
                    WHERE at.abbreviation=$1 AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 102.0

        h_home_ppg = await _homescore(home_abbr)
        a_away_ppg = await _awayscore(away_abbr)

        # Win %
        async def _wp(abbr, lim=999):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.w) FROM (
                    SELECT CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int
                           ELSE (g.away_score>g.home_score)::int END as w
                    FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
                    JOIN nba.teams ht ON ht.id=g.home_team_id JOIN nba.teams at ON at.id=g.away_team_id
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.5
        h_wp, a_wp = await _wp(home_abbr), await _wp(away_abbr)

        gr = await conn.fetchrow("SELECT game_type FROM nba.games WHERE id=$1", game_id)
        is_playoff = 1 if (gr and gr['game_type'] in ('P', 'PO', 'PS')) else 0

        # Travel
        hc, ac = COORDS.get(home_abbr, (0, 0)), COORDS.get(away_abbr, (0, 0))
        tm = haversine(hc[0], hc[1], ac[0], ac[1])
        if tm < 50:
            tm = 0.0

        rows = await conn.fetch("SELECT abbreviation, division, conference FROM nba.teams")
        divs = {r['abbreviation']: r['division'] for r in rows}
        confs = {r['abbreviation']: r['conference'] for r in rows}
        idiv = 1 if (divs.get(home_abbr) and divs.get(away_abbr) and divs[home_abbr] == divs[away_abbr]) else 0
        iconf = 1 if (confs.get(home_abbr) and confs.get(away_abbr) and confs[home_abbr] == confs[away_abbr]) else 0
        alt_diff = ALTITUDE.get(home_abbr, 0) - ALTITUDE.get(away_abbr, 0)

        vals = {
            "ou_movement": 0.0,
            "h_ppg5": h_ppg5, "a_ppg5": a_ppg5,
            "h_ppg10": h_ppg10, "a_ppg10": a_ppg10,
            "h_oppg10": h_oppg10, "a_oppg10": a_oppg10,
            "h_fg_pct10": h_fg_pct10, "a_fg_pct10": a_fg_pct10,
            "h_3pt_pct10": h_3pt_pct10, "a_3pt_pct10": a_3pt_pct10,
            "opening_ou": oo,
            "over_pct_h_r5": 0.5, "over_pct_a_r5": 0.5,
            "travel_miles": tm,
            "h_home_ppg": h_home_ppg,
            "a_away_ppg": a_away_ppg,
            "h_winpct": h_wp, "a_winpct": a_wp,
            "is_div": idiv, "is_conf": iconf,
            "tz_diff": 0.0,
            "altitude_diff": alt_diff,
            "home_b2b": 1 if rest_h == 0 else 0,
            "away_b2b": 1 if rest_a == 0 else 0,
            "rest_h": rest_h, "rest_a": rest_a,
            "is_playoff": is_playoff,
        }
        x = np.array([[vals.get(f, 0.0) for f in FEATURES_TRAINING]], dtype=np.float32)
        total = float(model.predict(x)[0])
        conf = min(0.50 + abs(total - co) * 0.008, 0.95)
        return round(total, 1), round(conf, 2)

    except Exception as e:
        logger.error(f"OU pred failed [{game_id} {home_abbr}@{away_abbr}]: {e}")
        return None, 0.50
    finally:
        if _close_conn and conn:
            await conn.close()


async def train_model(year: int, train_years: list[int]) -> object:
    """Train OU model from scratch on given years. Returns trained XGBoost model."""
    engine = create_async_engine(DB)
    df = await load_data(engine)
    feats = build_features(df)
    await engine.dispose()

    tr_all = feats[feats["year"].isin(train_years)].reset_index(drop=True)
    log(f"Training OU on {len(tr_all)} games ({train_years})")

    target_tr = tr_all["actual_total"]
    q01, q99 = target_tr.quantile(0.01), target_tr.quantile(0.99)
    valid = (target_tr >= q01) & (target_tr <= q99)
    tr_all = tr_all[valid].reset_index(drop=True)
    target_tr = target_tr[valid].reset_index(drop=True)

    X_tr = tr_all[FEATURES_TRAINING].fillna(0).astype(np.float32)

    w = np.ones(len(tr_all))
    for i in range(len(tr_all)):
        s = tr_all.at[tr_all.index[i], "year"]
        years_back = year - s
        if years_back <= 1:
            w[i] = 4.0
        elif years_back <= 2:
            w[i] = 3.0
        elif years_back <= 3:
            w[i] = 2.0
        elif years_back <= 5:
            w[i] = 1.5

    model = xgb.XGBRegressor(
        n_estimators=300, max_depth=5, learning_rate=0.04,
        subsample=0.7, colsample_bytree=0.7,
        reg_alpha=1.0, reg_lambda=2.0,
        min_child_weight=3,
        random_state=42, n_jobs=-1, verbosity=0,
    )
    model.fit(X_tr, target_tr, sample_weight=w, verbose=False)
    log(f"OU model trained ({len(FEATURES_TRAINING)} features)")
    return model


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NBA XGBoost O/U Backtester")
    parser.add_argument("--test-year", type=int, default=2023, help="Year to test on")
    parser.add_argument("--mode", type=str, default="single",
                        choices=["single", "all"], help="Single year or all years")
    args = parser.parse_args()

    if args.mode == "all":
        asyncio.run(run_all_years())
    else:
        asyncio.run(run_single(test_year=args.test_year))
