"""
MLB XGBoost Backtester — Vectorized feature engineering + rolling year-by-year evaluation.

Predicts run differential using rolling team stats, betting lines, and situational
features. Evaluates on MAE, ATS (run line), O/U (total), and ML accuracy.

Usage:
    docker exec earl-knows-football-api-1 python -m app.handicapping.mlb_backtest
    docker exec earl-knows-football-api-1 python -m app.handicapping.mlb_backtest --test-year 2023
    docker exec earl-knows-football-api-1 python -m app.handicapping.mlb_backtest --features simple
"""
import asyncio
import logging
import warnings
import json
import math
import os
import pickle
import shutil
import sys
from datetime import datetime, timezone, date
from typing import Optional, Any
from pathlib import Path
import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sklearn.metrics import mean_absolute_error
import xgboost as xgb

# ── Training DB persistence (safe import) ──
try:
    from app.handicapping.db_training import (
        save_training_run,
        update_pkl_filename,
        get_current_training_run,
        get_model_pkl_path,
    )
    _DB_HELPERS_AVAILABLE = True
except ImportError:
    _DB_HELPERS_AVAILABLE = False

warnings.filterwarnings("ignore")


async def load_data(engine):
    """Load training data from DB for MLB models."""
    query = """
        SELECT g.*,
               h.abbreviation AS ha,
               a.abbreviation AS aa,
               h.division AS hdiv,
               a.division AS adiv,
               (g.home_score - g.away_score) AS margin,
               s.year,
               g.date AS game_date,
               c.closing_spread AS spread,
               c.closing_home_ml AS home_moneyline,
               c.closing_away_ml AS away_moneyline,
               c.closing_ou AS over_under,
               c.closing_ou_sportsbook AS sportsbook,
               c.opening_ou AS opening_total,
               c.has_verified_ou
        FROM mlb.games g
        LEFT JOIN mlb.teams h ON h.id = g.home_team_id
        LEFT JOIN mlb.teams a ON a.id = g.away_team_id
        LEFT JOIN mlb.seasons s ON s.id = g.season_id
        LEFT JOIN mlb.betting_lines_consolidated c ON c.game_id = g.id
        WHERE g.status = 'FINAL'
        ORDER BY g.date DESC
    """
    async with engine.begin() as conn:
        result = await conn.execute(text(query))
        rows = result.fetchall()
        return pd.DataFrame(rows, columns=result.keys())


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("earl.mlb_xgb_ats")
log = logger.info

import os
DB = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DSN = DB.replace("+asyncpg", "")  # sync DSN for inference

# ── Team timezone map (IANA, for scheduling) ──
TZ = {
    "ARI": -7, "ATL": -5, "BAL": -5, "BOS": -5, "CHC": -6, "CWS": -6,
    "CIN": -5, "CLE": -5, "COL": -7, "DET": -5, "HOU": -6, "KC": -6,
    "LAA": -8, "LAD": -8, "MIA": -5, "MIL": -6, "MIN": -6, "NYM": -5,
    "NYY": -5, "OAK": -8, "PHI": -5, "PIT": -5, "SD": -8, "SEA": -8,
    "SF": -8, "STL": -6, "TB": -5, "TEX": -6, "TOR": -5, "WSH": -5,
}

COORDS = {
    "ARI": (33.4, -112.1), "ATL": (33.7, -84.4), "BAL": (39.3, -76.6),
    "BOS": (42.3, -71.1), "CHC": (41.9, -87.7), "CWS": (41.8, -87.6),
    "CIN": (39.1, -84.5), "CLE": (41.5, -81.7), "COL": (39.8, -104.9),
    "DET": (42.3, -83.0), "HOU": (29.8, -95.4), "KC": (39.1, -94.5),
    "LAA": (33.8, -117.9), "LAD": (34.1, -118.2), "MIA": (25.8, -80.2),
    "MIL": (43.0, -87.9), "MIN": (44.9, -93.2), "NYM": (40.8, -73.8),
    "NYY": (40.8, -73.9), "OAK": (37.8, -122.2), "PHI": (39.9, -75.2),
    "PIT": (40.4, -80.0), "SD": (32.7, -117.2), "SEA": (47.6, -122.3),
    "SF": (37.8, -122.4), "STL": (38.6, -90.2), "TB": (27.8, -82.7),
    "TEX": (32.8, -97.1), "TOR": (43.6, -79.4), "WSH": (38.9, -77.0),
}

# ── Feature sets for experimentation ──
# Feature sets for experimentation
# Column names refer to actual columns produced by build_features()
# Convention: h_ = home team, a_ = away team
# h_home_rf = home team's runs scored in home games
# a_home_rf = away team's runs scored in away games
FEATURE_SETS = {
    "simple": [
        "h_rf10", "h_ra10", "a_rf10", "a_ra10",
        "rest_diff", "is_home_fav",
    ],
    "rolling": [
        "h_rf10", "h_ra10", "a_rf10", "a_ra10",
        "h_rf20", "h_ra20", "a_rf20", "a_ra20",
        "h_home_rf", "h_home_ra", "a_home_rf", "a_home_ra",
        "rest_diff", "rest_h", "rest_a",
    ],
    "full": [
        "h_rf10", "h_ra10", "a_rf10", "a_ra10",
        "h_rf20", "h_ra20", "a_rf20", "a_ra20",
        "h_home_rf", "h_home_ra", "a_home_rf", "a_home_ra",
        "rest_diff", "rest_h", "rest_a",
        "h_winpct", "a_winpct", "winpct_diff",
        "h_implied", "a_implied",
        "is_home_fav", "ou_line",
        "travel_miles", "tz_diff", "is_div", "is_dome",
        "month", "is_summer",
    ],
    "ml_only": [
        "h_rf10", "h_ra10", "a_rf10", "a_ra10",
        "h_home_rf", "h_home_ra", "a_home_rf", "a_home_ra",
        "rest_diff", "winpct_diff",
        "h_implied", "a_implied",
    ],
}


def haversine(lat1, lon1, lat2, lon2):
    """Distance in miles between two lat/lon points."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))




def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build all features in vectorized fashion.

    Rolling features computed from ALL games to avoid look-ahead gaps.
    Final filter drops games without complete line data (no spread = no ATS eval).

    Strategy:
    1. Flatten to team-game view (each game -> 2 rows: home team, away team)
    2. Compute rolling stats per team
    3. Join back to game level
    4. Drop games without spread + moneyline (incomplete data)
    """
    log("Building team-game table...")

    # ── 1. Flatten to team-game view ──
    rows = []
    for _, g in df.iterrows():
        gid = g.game_id if 'game_id' in df.columns else g.id
        # Home team row
        rows.append({
            "game_id": gid, "year": g.year, "game_date": g.game_date,
            "team": g.ha, "opp": g.aa,
            "pf": g.home_score, "pa": g.away_score,
            "is_home": 1, "margin": g.margin,
            "roof": g.roof_type, "temp": g.temperature, "wind": g.wind_speed,
            "division": g.hdiv,
        })
        # Away team row
        rows.append({
            "game_id": gid, "year": g.year, "game_date": g.game_date,
            "team": g.aa, "opp": g.ha,
            "pf": g.away_score, "pa": g.home_score,
            "is_home": 0, "margin": -g.margin,
            "roof": g.roof_type, "temp": g.temperature, "wind": g.wind_speed,
            "division": g.adiv,
        })
    tg = pd.DataFrame(rows).sort_values(["team", "game_date", "game_id"]).reset_index(drop=True)

    # ── 2. Compute rolling stats per team (NO LOOK-AHEAD: shift(1)) ──
    log("  Rolling stats (5, 10, 20 game windows)...")
    tg["game_date_dt"] = pd.to_datetime(tg["game_date"])

    for window in [5, 10, 20]:
        # Rolling runs for and against
        tg[f"rf{window}"] = (
            tg.groupby("team")["pf"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )
        tg[f"ra{window}"] = (
            tg.groupby("team")["pa"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )

    # ── Prior-season smoothing for early-season games ──
    # Compute each team's prior-season per-game averages for blending
    log("  Computing prior-season averages for early-season smoothing...")
    team_season_avg = tg.groupby(["team", "year"]).agg(
        prior_pf=("pf", "mean"),
        prior_pa=("pa", "mean"),
    ).reset_index()
    # Build (team, prev_year) → (pf, pa) lookup
    team_prior = {}
    for _, row in team_season_avg.iterrows():
        team_prior[(row["team"], row["year"])] = (row["prior_pf"], row["prior_pa"])

    # Games played counter per team per season
    tg["season_game_no"] = tg.groupby(["team", "year"]).cumcount() + 1

    # Blend: for first ~10 games, blend current rolling average with prior season's averages
    BLEND_WINDOW = 10
    log("  Blending rolling stats with prior-season averages...")
    for window in [5, 10, 20]:
        # Left-merge prior season averages onto early-season games
        early = tg[tg["season_game_no"] <= BLEND_WINDOW][["team", "year", "season_game_no"]].copy()
        early["prior_year"] = early["year"] - 1
        prior_lookup = tg.groupby(["team", "year"]).agg(
            prior_pf=("pf", "mean"), prior_pa=("pa", "mean")
        ).reset_index()
        prior_lookup.columns = ["team", "prior_year", "prior_pf", "prior_pa"]
        merged = early.merge(prior_lookup, on=["team", "prior_year"], how="left")
        merged = merged.dropna(subset=["prior_pf", "prior_pa"])
        if len(merged) == 0:
            continue
        prior_weight = (BLEND_WINDOW - merged["season_game_no"]) / BLEND_WINDOW
        merged["prior_weight"] = prior_weight.clip(0, 1)
        # Apply blend: new_val = (1-w)*current + w*prior
        current_rf = tg.loc[merged.index, f"rf{window}"]
        current_ra = tg.loc[merged.index, f"ra{window}"]
        has_rf = current_rf.notna()
        has_ra = current_ra.notna()
        if has_rf.any():
            tg.loc[merged.index[has_rf], f"rf{window}"] = (
                (1 - merged["prior_weight"][has_rf]) * current_rf[has_rf]
                + merged["prior_weight"][has_rf] * merged["prior_pf"].values[has_rf]
            )
        if has_ra.any():
            tg.loc[merged.index[has_ra], f"ra{window}"] = (
                (1 - merged["prior_weight"][has_ra]) * current_ra[has_ra]
                + merged["prior_weight"][has_ra] * merged["prior_pa"].values[has_ra]
            )

    # Home/away splits: rolling within each team's home/away subset
    log("  Home/away splits...")
    for team in tg["team"].unique():
        mask = tg["team"] == team
        for is_h in [1, 0]:
            sub = tg[mask & (tg["is_home"] == is_h)].copy()
            if len(sub) < 2:
                continue
            sub["home_rf"] = sub["pf"].shift(1).expanding().mean()
            sub["home_ra"] = sub["pa"].shift(1).expanding().mean()
            for idx, row in sub.iterrows():
                tg.at[idx, f"{'h' if is_h else 'a'}_home_rf"] = row["home_rf"]
                tg.at[idx, f"{'h' if is_h else 'a'}_home_ra"] = row["home_ra"]

    # Rest days
    log("  Rest days...")
    tg["prev_date"] = tg.groupby("team")["game_date_dt"].shift(1)
    tg["rest"] = (tg["game_date_dt"] - tg["prev_date"]).dt.days.fillna(1).clip(0, 30)

    # Win percentage — also blend with prior season for early games
    tg["is_win"] = (tg["pf"] > tg["pa"]).astype(int)
    tg["winpct"] = (
        tg.groupby("team")["is_win"]
        .transform(lambda x: x.shift(1).expanding().mean())
    )
    # Vectorized winpct blending with prior season
    log("  Blending winpct with prior-season averages...")
    early = tg[tg["season_game_no"] <= BLEND_WINDOW][["team", "year", "season_game_no"]].copy()
    early["prior_year"] = early["year"] - 1
    prior_wp = tg.groupby(["team", "year"])["is_win"].mean().reset_index()
    prior_wp.columns = ["team", "prior_year", "prior_winpct"]
    merged = early.merge(prior_wp, on=["team", "prior_year"], how="left")
    merged = merged.dropna(subset=["prior_winpct"])
    if len(merged) > 0:
        prior_weight = ((BLEND_WINDOW - merged["season_game_no"]) / BLEND_WINDOW).clip(0, 1)
        current_wp = tg.loc[merged.index, "winpct"]
        has_wp = current_wp.notna()
        if has_wp.any():
            tg.loc[merged.index[has_wp], "winpct"] = (
                (1 - prior_weight[has_wp]) * current_wp[has_wp]
                + prior_weight[has_wp] * merged["prior_winpct"].values[has_wp]
            )

    # ── 3. Rejoin home/away into game-level features ──
    log("  Building game-level features...")
    h = tg[tg["is_home"] == 1][
        ["game_id", "team", "opp", "rf5", "ra5", "rf10", "ra10", "rf20", "ra20",
         "rest", "winpct",
         "h_home_rf", "h_home_ra",
         "division"]
    ].rename(columns={
        "team": "ha", "opp": "aa",
        "rf5": "h_rf5", "ra5": "h_ra5",
        "rf10": "h_rf10", "ra10": "h_ra10",
        "rf20": "h_rf20", "ra20": "h_ra20",
        "rest": "rest_h", "winpct": "h_winpct",
        "division": "hdiv",
    })

    a = tg[tg["is_home"] == 0][
        ["game_id", "team", "opp", "rf5", "ra5", "rf10", "ra10", "rf20", "ra20",
         "rest", "winpct",
         "a_home_rf", "a_home_ra",
         "division"]
    ].rename(columns={
        "team": "aa", "opp": "ha",
        "rf5": "a_rf5", "ra5": "a_ra5",
        "rf10": "a_rf10", "ra10": "a_ra10",
        "rf20": "a_rf20", "ra20": "a_ra20",
        "rest": "rest_a", "winpct": "a_winpct",
        "division": "adiv",
    })

    feats = h.merge(a, on="game_id", suffixes=("_h", "_a"))
    # Fix suffixed team cols (both should match)
    feats["ha"] = feats["ha_h"]
    feats["aa"] = feats["aa_h"]
    feats["rest_diff"] = feats["rest_h"] - feats["rest_a"]
    feats["winpct_diff"] = feats["h_winpct"] - feats["a_winpct"]
    feats["is_div"] = (feats["hdiv"] == feats["adiv"]).astype(int)

    # ── 5. Add game-level features from original df ──
    # Columns we need from original df that aren't already in feats
    orig_cols = df[["game_id", "year", "game_date", "margin", "spread", "over_under",
                     "home_moneyline", "away_moneyline",
                     "home_implied_probability", "away_implied_probability",
                     "roof_type", "temperature", "wind_speed",
                     "day_night", "home_score", "away_score"]].copy()
    feats = feats.merge(orig_cols, on="game_id")

    # ── 6. Filter to games with complete line data ──
    # Rolling features used ALL games (correct), but only train/predict on complete lines
    pre_filter = len(feats)
    feats = feats[feats["home_moneyline"].notna() & feats["spread"].notna()].copy()
    log(f"  Filtered to {len(feats)} games with complete line data (dropped {pre_filter - len(feats)} without)")

    # Moneyline features
    feats["is_home_fav"] = (
        (feats["home_moneyline"].notna()) & (feats["home_moneyline"] < 0)
    ).astype(int)
    feats["h_implied"] = feats["home_implied_probability"].fillna(0.5)
    feats["a_implied"] = feats["away_implied_probability"].fillna(0.5)

    # Target: actual margin
    feats["actual_margin"] = feats["margin"]

    # Over/under line
    feats["ou_line"] = feats["over_under"].fillna(8.5)  # default ~MLB avg
    feats["actual_total"] = feats["home_score"] + feats["away_score"]

    # Seasonal & weather
    feats["game_date_dt"] = pd.to_datetime(feats["game_date"])
    feats["month"] = feats["game_date_dt"].dt.month
    feats["is_summer"] = feats["month"].isin([6, 7, 8]).astype(int)
    feats["is_dome"] = (feats["roof_type"] == "dome").astype(int)
    feats["roof_type"].fillna("outdoor", inplace=True)

    # Travel distance
    feats["travel_miles"] = feats.apply(
        lambda r: haversine(
            *COORDS.get(r["aa"], (0, 0)), *COORDS.get(r["ha"], (0, 0))
        ),
        axis=1,
    )
    feats.loc[feats["travel_miles"] < 50, "travel_miles"] = 0

    # Timezone diff (home is home team's TZ)
    feats["tz_diff"] = feats.apply(
        lambda r: TZ.get(r["ha"], -5) - TZ.get(r["aa"], -5), axis=1
    )

    return feats


async def run_backtest(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    test_year: int = 2023,
    train_years: Optional[list] = None,
    feature_set: str = "full",
    xgb_params: Optional[dict] = None,
    training_id: Optional[str] = None,
) -> dict:
    """Run rolling XGBoost backtest for a test year."""
    if train_years is None:
        train_years = [y for y in range(2021, test_year) if y != test_year]

    features = FEATURE_SETS.get(feature_set, FEATURE_SETS["full"])

    if xgb_params is None:
        xgb_params = {
            "n_estimators": 200,
            "max_depth": 4,
            "learning_rate": 0.05,
            "subsample": 0.7,
            "colsample_bytree": 0.7,
            "reg_alpha": 1.0,
            "reg_lambda": 2.0,
            "random_state": 42,
            "n_jobs": -1,
            "verbosity": 0,
        }

    feats["game_date_dt"] = pd.to_datetime(feats["game_date"])
    feats["month"] = feats["game_date_dt"].dt.month

    # Split data
    tr_mask = feats["year"].isin(train_years)
    te_mask = feats["year"] == test_year
    tr_all = feats[tr_mask].reset_index(drop=True)
    te_all = feats[te_mask].sort_values(["month", "game_date"]).reset_index(drop=True)

    log(f"  Train: {len(tr_all)} games ({train_years})")
    log(f"  Test:  {len(te_all)} games ({test_year})")

    if len(tr_all) < 50 or len(te_all) < 10:
        log("  ⚠ Not enough data, skipping")
        return {"error": "insufficient_data"}

    X_te = te_all[features].fillna(0).astype(np.float32)
    y_te = te_all["actual_margin"].values

    # Train model from scratch (no caching by year)
    X_tr = tr_all[features].fillna(0).astype(np.float32)
    y_tr = tr_all["actual_margin"].values

    # Clip outliers at 1st and 99th percentile
    q01, q99 = np.percentile(y_tr, [1, 99])
    clip_mask = (y_tr >= q01) & (y_tr <= q99)
    clipped = (~clip_mask).sum()
    tr_all = tr_all[clip_mask].reset_index(drop=True)
    X_tr = tr_all[features].fillna(0).astype(np.float32)
    y_tr = tr_all["actual_margin"].values
    if clipped:
        log(f"  Clipped {clipped} outlier games ({q01:.1f}-{q99:.1f} run differential)")

    n_tr = len(tr_all)
    w = np.ones(n_tr)
    for i in range(n_tr):
        s = tr_all.at[tr_all.index[i], "year"]
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
    model.fit(X_tr, y_tr, sample_weight=w, verbose=False)
    # Save model by test year for engine backtest to use
    model_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
    model_dir.mkdir(parents=True, exist_ok=True)
    pkl_name = f"{training_id}-{test_year}.pkl" if training_id else f"mlb_ats_{test_year}.pkl"
    save_path = model_dir / pkl_name
    with open(save_path, "wb") as f:
        pickle.dump(model, f)
    log(f"  Saved ATS model to {save_path}")

    # Predict
    pred = model.predict(X_te)
    te_all = te_all.copy()
    te_all["pred_margin"] = pred
    te_all["pred_error"] = te_all["actual_margin"] - pred

    # ── Evaluate ──
    mae = mean_absolute_error(y_te, pred)
    err_mean = te_all["pred_error"].mean()
    err_std = te_all["pred_error"].std()

    # ATS: Run line evaluation
    # Spread is from home team perspective:
    #   spread=-1.5 means home favored by 1.5 -> home covers if margin > +1.5
    #   spread=+1.5 means home underdog -> home covers if margin > -1.5 (lose by ≤1)
    # Formula: home covers if actual_margin > -spread
    has_rl = te_all["spread"].notna()
    if has_rl.any():
        rl_df = te_all[has_rl].copy()
        rl_df["actual_ats_win"] = rl_df["actual_margin"] > (-rl_df["spread"])
        rl_df["pred_ats_win"] = rl_df["pred_margin"] > (-rl_df["spread"])
        ats_correct = (rl_df["actual_ats_win"] == rl_df["pred_ats_win"]).sum()
        ats_incorrect = len(rl_df) - ats_correct
    else:
        ats_correct = 0
        ats_incorrect = 0
        rl_df = pd.DataFrame()

    # ── Monthly breakdown (run differential only) ──
    monthly = []
    for m in sorted(te_all["month"].unique()):
        sub = te_all[te_all["month"] == m]
        if len(sub) < 5:
            continue
        mae_m = mean_absolute_error(sub["actual_margin"], sub["pred_margin"])
        monthly.append({
            "month": int(m),
            "games": int(len(sub)),
            "mae": float(round(mae_m, 2)),
        })

    # Feature importance
    imp = pd.DataFrame({
        "feature": features,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)

    # ── Results ──
    ats_total = int(ats_correct + ats_incorrect)
    total_games = int(len(te_all))

    result = {
        "test_year": test_year,
        "train_years": train_years,
        "feature_set": feature_set,
        "total_games": total_games,
        "mae": round(mae, 2),
        "err_mean": round(err_mean, 2),
        "err_std": round(err_std, 2),
        "within_3": float(round((abs(te_all["pred_error"]) < 3).mean(), 3)),
        "within_5": float(round((abs(te_all["pred_error"]) < 5).mean(), 3)),
        "ats": {
            "correct": int(ats_correct),
            "incorrect": int(ats_incorrect),
            "total": ats_total,
            "pct": float(round(100 * ats_correct / max(ats_total, 1), 1)),
        },
        "monthly": monthly,
        "feature_importance": [
            {"feature": str(r["feature"]), "importance": float(round(float(r["importance"]), 4))}
            for _, r in imp.iterrows()
        ],
    }

    # Print summary
    print_summary(result, feats, te_all)

    return result, te_all


def print_summary(result: dict, feats: pd.DataFrame, te_all: pd.DataFrame):
    """Pretty-print backtest results."""
    print(f"\n{'='*62}")
    print(f"MLB BACKTEST — {result['test_year']} Season")
    print(f"Features: {result['feature_set']} ({len(result['feature_importance'])} feats)")
    print(f"Train: {result['train_years']}")
    print(f"{'='*62}")

    print(f"\n📊 RUN DIFFERENTIAL PREDICTION")
    print(f"  MAE:       {result['mae']:.2f} runs")
    print(f"  Bias:      {result['err_mean']:+.2f} runs")
    print(f"  Std Dev:   {result['err_std']:.2f} runs")
    print(f"  ±3 runs:   {result['within_3']:.1%}")
    print(f"  ±5 runs:   {result['within_5']:.1%}")

    print(f"\n🏆 BETTING PERFORMANCE")
    ats = result["ats"]
    print(f"  ATS (RL):  {ats['correct']:4d}-{ats['incorrect']:4d}  ({ats['pct']:.1f}%)  [{ats['total']} games]")

    print(f"\n📅 MONTHLY BREAKDOWN (MAE)")
    print(f"  {'Month':>6s}  {'Games':>5s}  {'MAE':>5s}")
    for m in result["monthly"]:
        print(f"  {m['month']:>6d}  {m['games']:>5d}  {m['mae']:>5.2f}")

    print(f"\n🔑 TOP FEATURES")
    for i, fi in enumerate(result["feature_importance"][:10]):
        bar = "█" * int(fi["importance"] * 100) + "░" * max(0, 20 - int(fi["importance"] * 100))
        print(f"  {i+1:2d}. {fi['feature']:>15s}: {fi['importance']:.4f} {bar}")

    print()


async def run_all_years(
    feature_sets: Optional[list[str]] = None,
    test_years: Optional[list[int]] = None,
    train_from: int = 2021,
):
    """Run backtests across multiple years and feature sets, compare results."""
    if feature_sets is None:
        feature_sets = ["simple", "rolling", "full", "ml_only"]
    if test_years is None:
        test_years = [2025, 2026]

    t0 = datetime.now()
    engine = create_async_engine(DB)

    df = await load_data(engine)
    df = _enrich_df(df)
    log(f"\nBuilding features...")
    feats = build_features(df)
    log(f"Feature table: {len(feats)} rows, {len(feats.columns)} columns")

    # Temp ID for PKL naming (will be renamed to UUID after save_training_run)
    training_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    all_results = []

    for year in test_years:
        train = [y for y in range(train_from, year)]
        for fs in feature_sets:
            log(f"\n{'─'*62}")
            log(f"Testing year={year}, features={fs}")
            log(f"{'─'*62}")
            result, _te = await run_backtest(
                df, feats,
                test_year=year,
                train_years=train,
                feature_set=fs,
                training_id=training_id,
            )
            if "error" not in result:
                all_results.append(result)

    await engine.dispose()

    # Comparison table
    print(f"\n{'='*62}")
    print("FEATURE SET COMPARISON")
    print(f"{'='*62}")

    # Group by feature set
    from collections import defaultdict
    by_fs: dict[str, list] = defaultdict(list)
    for r in all_results:
        by_fs[r["feature_set"]].append(r)

    print(f"{'Feat Set':>12s}  {'Year':>4s}  {'Games':>5s}  {'MAE':>5s}  {'ATS%':>5s}")
    print("─" * 62)
    for fs in feature_sets:
        entries = sorted(by_fs[fs], key=lambda x: x["test_year"])
        for r in entries:
            print(f"  {fs:>10s}  {r['test_year']:>4d}  {r['total_games']:>5d}  "
                  f"{r['mae']:>5.2f}  {r['ats']['pct']:>5.1f}")
        # Average
        if entries:
            avg_mae = np.mean([e["mae"] for e in entries])
            avg_ats = np.mean([e["ats"]["pct"] for e in entries])
            total_g = sum(e["total_games"] for e in entries)
            print(f"  {'─' * 48}")
            print(f"  {fs:>10s}  {'AVG':>4s}  {total_g:>5d}  "
                  f"{avg_mae:>5.2f}  {avg_ats:>5.1f}")
            print()

    # Save results
    out = {
        "run_time": str(datetime.now() - t0),
        "results": all_results,
    }
    out_path = "/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb_backtest_results.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    log(f"\nResults saved to {out_path}")

    # Save best feature set to training_runs DB
    if _DB_HELPERS_AVAILABLE:
        # Pick the best feature set (lowest avg MAE)
        fs_avgs = {}
        for fs in feature_sets:
            entries = by_fs.get(fs, [])
            if entries:
                fs_avgs[fs] = np.mean([e["mae"] for e in entries])
        best_fs = min(fs_avgs, key=fs_avgs.get) if fs_avgs else feature_sets[0]
        log(f"\nSaving best feature set '{best_fs}' to training DB...")

        # Build combined results dict from per-year entries
        best_entries = by_fs.get(best_fs, [])
        combined_results = []
        for entry in best_entries:
            row = {
                "test_year": entry["test_year"],
                "total_games": entry["total_games"],
                "mae": entry["mae"],
                "ats": entry.get("ats", {}),
                "name": "ATS",
                "ats_correct": entry.get("ats", {}).get("correct", 0),
                "ats_total": entry.get("ats", {}).get("total", 0),
                "ats_pct": entry.get("ats", {}).get("pct", 0.0),
            }
            # Add feature importance if available
            if "feature_importance" in entry:
                row["feature_importance"] = entry["feature_importance"]
            combined_results.append(row)

        pkl_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
        pkl_dir.mkdir(parents=True, exist_ok=True)

        temp_prefix = training_id  # date-based training_id from outer scope, used as temp PKL prefix

        training_id = save_training_run(
            sport="mlb",
            model_type="ats",
            results_json=combined_results,
            pkl_filename="",
            algorithm="xgboost",
            description=f"MLB ATS full backtest: {best_fs} features",
            test_year=(max(e["test_year"] for e in best_entries) if best_entries else 2025),
            train_years=[y for y in range(2021, (max(e["test_year"] for e in best_entries) if best_entries else 2025) + 1)],
        )

        # Rename PKL files from temp_prefix (date-based) to UUID
        if best_entries:
            latest_year = max(e["test_year"] for e in best_entries)
            for yr in best_entries:
                year = yr["test_year"]
                temp_pkl = pkl_dir / f"{temp_prefix}-{year}.pkl"
                uuid_pkl = pkl_dir / f"{training_id}-{year}.pkl"
                if temp_pkl.exists():
                    shutil.move(str(temp_pkl), str(uuid_pkl))
                    log(f"  Renamed {temp_prefix}-{year}.pkl → {training_id}-{year}.pkl")

            # Copy the latest year's PKL as prod model
            pkl_name = f"{training_id}.pkl"
            src_pkl = pkl_dir / f"{training_id}-{latest_year}.pkl"
            pkl_path = pkl_dir / pkl_name
            if src_pkl.exists():
                shutil.copy2(str(src_pkl), str(pkl_path))
                update_pkl_filename("mlb", training_id, pkl_name)
                log(f"  Prod model saved as {pkl_name}")

    log(f"Total time: {datetime.now() - t0}")


def _enrich_df(df: pd.DataFrame) -> pd.DataFrame:
    """Pre-process raw DB DataFrame: compute derived columns expected by build_features()."""
    df = df.copy()
    # game_id alias (games table has 'id')
    if "game_id" not in df.columns:
        df["game_id"] = df["id"]
    # Moneyline implied probabilities
    df["home_implied_probability"] = df["home_moneyline"].apply(_ml_implied)
    df["away_implied_probability"] = df["away_moneyline"].apply(_ml_implied)
    return df


async def run_single(test_year: int = 2025, feature_set: str = "full",
                      train_years: Optional[list] = None):
    """Run a single backtest for quick iteration."""
    if train_years is None:
        train_years = [y for y in range(2021, test_year)]
    engine = create_async_engine(DB)
    df = await load_data(engine)
    df = _enrich_df(df)
    feats = build_features(df)
    run_training_id_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    result, _ = await run_backtest(df, feats, test_year=test_year, train_years=train_years, feature_set=feature_set, training_id=run_training_id_ts)
    await engine.dispose()
    # Write results to the DB (falls back to JSON file)
    if _DB_HELPERS_AVAILABLE:
        pkl_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
        pkl_dir.mkdir(parents=True, exist_ok=True)

        training_id = save_training_run(
            sport="mlb",
            model_type="ats",
            results_json=result,
            pkl_filename="",
            algorithm="xgboost",
            description=f"MLB ATS single backtest: {test_year}",
            test_year=test_year,
            train_years=train_years,
        )

        pkl_name = f"{training_id}.pkl"
        src_pkl = pkl_dir / f"{run_training_id_ts}-{test_year}.pkl"
        pkl_path = pkl_dir / pkl_name
        if src_pkl.exists():
            shutil.copy2(str(src_pkl), str(pkl_path))

        update_pkl_filename("mlb", training_id, pkl_name)
        print(f"\nResults saved to DB (training_id={training_id}, pkl={pkl_name})")
    else:
        import json
        out_path = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb_backtest_results.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            with open(out_path) as f:
                existing = json.load(f)
            existing = [r for r in existing if r.get("test_year") != test_year]
            existing.append(result)
            data = existing
        else:
            data = [result]
        with open(out_path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        print(f"\nResults saved to {out_path}")


# ── Helpers ────────────────────────────────────────────────────────────

def _ml_implied(v):
    """Moneyline to implied probability."""
    if v is None or v == 0: return 0.5
    return abs(v) / (abs(v) + 100) if v < 0 else 100.0 / (v + 100)


# ── Model management & inference (imported by mlb_engine.py) ──────────

ATS_MODEL_PATH = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb/mlb_margin_model_prod.pkl")
_ats_model = None


def set_model_path(path: str):
    """Override ATS model path (used by admin panel / backtesting)."""
    global ATS_MODEL_PATH, _ats_model
    ATS_MODEL_PATH = Path(path)
    _ats_model = None


def _load_ats_model():
    global _ats_model
    if _ats_model is not None:
        return _ats_model
    if not ATS_MODEL_PATH.exists():
        raise FileNotFoundError(f"ATS model not found at {ATS_MODEL_PATH}")
    with open(ATS_MODEL_PATH, "rb") as f:
        payload = pickle.load(f)
    model = payload["model"] if isinstance(payload, dict) else payload
    _ats_model = model
    feats = (payload.get("features") or FEATURE_SETS["full"]) if isinstance(payload, dict) else FEATURE_SETS["full"]
    log(f"Loaded ATS model ({len(feats)} features)")
    return _ats_model


# ── Cached build_features() output for predict_ats ──
# Set once, reused across all predict_ats() calls until the cache is invalidated.
_ats_feature_cache: Optional[pd.DataFrame] = None



async def predict_ats(game_id: int, home_abbr: str, away_abbr: str,
                       yr: int, game_date: str,
                       home_stats, away_stats,
                       line_obj,
                       conn: Optional[object] = None) -> tuple[Optional[float], float]:
    """
    Predict home run margin for one MLB game.

    Uses the shared feature_pipeline cache (which itself uses the same
    load_data() as training). Guarantees identical features to training.
    Returns (margin, confidence) or (None, 0.0) if model unavailable.
    """
    global _ats_feature_cache
    try:
        model = _load_ats_model()
    except FileNotFoundError:
        logger.warning("ATS model not found at %s", ATS_MODEL_PATH)
        return None, 0.0
    
    gd_obj = date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
    try:
        if _ats_feature_cache is None:
            cache = get_cache()
            if not cache.is_warm:
                logger.warning("predict_ats: cache cold, warming from DB...")
                from sqlalchemy.ext.asyncio import create_async_engine as _cae
                eng = _cae(DB)
                await cache.refresh(eng)
                await eng.dispose()

            raw_df = cache.get_raw_df()
            _ats_feature_cache = build_features(raw_df)
            logger.info(f"ATS predict: {len(_ats_feature_cache)} features built")

        feats_df = _ats_feature_cache
        logger.info(f"ATS predict: {len(feats_df)} features from cache")
        
        row = feats_df[feats_df["game_id"] == game_id]
        if len(row) == 0:
            logger.warning(f"Game {game_id} not found in ATS features")
            return None, 0.50
        
        r = row.iloc[0]
        vals = {f: float(r[f]) if pd.notna(r[f]) else 0.0
                for f in FEATURE_SETS["full"]}
        
        x = np.array([[vals.get(f, 0.0) for f in FEATURE_SETS["full"]]], dtype=np.float32)
        margin = float(model.predict(x)[0])
        conf = min(0.50 + abs(margin) * 0.12, 0.95)
        return round(margin, 1), round(conf, 2)

    except Exception as e:
        logger.error(f"ATS pred failed [{game_id} {home_abbr}@{away_abbr}]: {e}")
        hm = float(getattr(home_stats, 'run_margin', 0.0))
        am = float(getattr(away_stats, 'run_margin', 0.0))
        return round(hm - am, 1), 0.50

async def train_model(year: int, train_years: list[int], feature_set: str = "full",
                       xgb_params: Optional[dict] = None) -> object:
    """Train ATS model from scratch on given years. Returns trained XGBoost model."""
    if xgb_params is None:
        xgb_params = {
            "n_estimators": 200, "max_depth": 4, "learning_rate": 0.05,
            "subsample": 0.7, "colsample_bytree": 0.7,
            "reg_alpha": 1.0, "reg_lambda": 2.0,
            "random_state": 42, "n_jobs": -1, "verbosity": 0,
        }
    features = FEATURE_SETS.get(feature_set, FEATURE_SETS["full"])
    engine = create_async_engine(DB)
    df = await load_data(engine)
    feats = build_features(df)
    await engine.dispose()
    
    feats["game_date_dt"] = pd.to_datetime(feats["game_date"])
    feats["month"] = feats["game_date_dt"].dt.month
    
    tr_all = feats[feats["year"].isin(train_years)].reset_index(drop=True)
    log(f"Training ATS on {len(tr_all)} games ({train_years})")
    
    X_tr = tr_all[features].fillna(0).astype(np.float32)
    y_tr = tr_all["actual_margin"].values
    
    w = np.ones(len(tr_all))
    for i in range(len(tr_all)):
        s = tr_all.at[tr_all.index[i], "year"]
        years_back = year - s
        if years_back <= 1: w[i] = 4.0
        elif years_back <= 2: w[i] = 3.0
        elif years_back <= 3: w[i] = 2.0
        elif years_back <= 5: w[i] = 1.5
    
    model = xgb.XGBRegressor(**xgb_params)
    model.fit(X_tr, y_tr, sample_weight=w, verbose=False)
    log(f"ATS model trained ({len(features)} features)")
    # Save by test year so engine backtest can use the same model
    model_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
    model_dir.mkdir(parents=True, exist_ok=True)
    for ty in range(year, year + 2):  # save for current + next year
        if ty <= 2026:
            p = model_dir / f"mlb_ats_{ty}.pkl"
            with open(p, "wb") as f:
                pickle.dump(model, f)
            log(f"  Saved ATS model to {p}")
    return model

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MLB XGBoost Backtester")
    parser.add_argument("--test-year", type=int, default=2025, help="Year to test on")
    parser.add_argument("--train-from", type=int, default=2021,
                        help="Earliest training year")
    parser.add_argument("--features", type=str, default="full",
                        choices=list(FEATURE_SETS.keys()) + ["all"],
                        help="Feature set to use")
    parser.add_argument("--mode", type=str, default="single",
                        choices=["single", "all"],
                        help="Single year or all years comparison")

    args = parser.parse_args()

    if args.mode == "all":
        asyncio.run(run_all_years(
            feature_sets=list(FEATURE_SETS.keys()) if args.features == "all" else [args.features],
            train_from=args.train_from,
        ))
    else:
        train_years = [y for y in range(args.train_from, args.test_year)]
        asyncio.run(run_single(test_year=args.test_year, feature_set=args.features, train_years=train_years))
