"""
MLB XGBoost Moneyline Backtester — binary classifier predicting home team win probability.

Dedicated ML model optimized for winner prediction, NOT derived from margin:
- Binary classification (XGBClassifier with objective='binary:logistic')
- Starter pitcher quality is the #1 feature (different from OU/margin models)
- Bullpen quality + fatigue (critical for holding leads)
- Market anchors + movement (sharp money signal)
- Team quality + momentum

Usage:
    docker exec earl-knows-football-api-1 python -m app.handicapping.mlb.mlb_xgb_model_ml --test-year 2025
    docker exec earl-knows-football-api-1 python -m app.handicapping.mlb.mlb_xgb_model_ml --mode all
"""
import asyncio
import json
import logging
import math
import os
import pickle
import shutil
import warnings
from datetime import datetime, date
from typing import Optional

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
from pathlib import Path
import numpy as np
import asyncpg
import pandas as pd
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score, brier_score_loss
import xgboost as xgb

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("earl.mlb_xgb_ml")
log = logger.info

import os
DB = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DSN = DB.replace("+asyncpg", "")  # sync DSN for inference

# ── Team coordinates (for travel distance) ──
COORDS = {
    "ARI": (33.4, -112.1), "ATL": (33.7, -84.4), "BAL": (39.3, -76.6),
    "BOS": (42.3, -71.1), "CHC": (41.9, -87.7), "CHW": (41.8, -87.6),
    "CIN": (39.1, -84.5), "CLE": (41.5, -81.7), "COL": (39.8, -104.9),
    "DET": (42.3, -83.0), "HOU": (29.8, -95.4), "KC": (39.1, -94.5),
    "LAA": (33.8, -117.9), "LAD": (34.1, -118.2), "MIA": (25.8, -80.2),
    "MIL": (43.0, -87.9), "MIN": (44.9, -93.2), "NYM": (40.8, -73.8),
    "NYY": (40.8, -73.9), "OAK": (37.8, -122.2), "PHI": (39.9, -75.2),
    "PIT": (40.4, -80.0), "SD": (32.7, -117.2), "SEA": (47.6, -122.3),
    "SF": (37.8, -122.4), "STL": (38.6, -90.2), "TB": (27.8, -82.7),
    "TEX": (32.8, -97.1), "TOR": (43.6, -79.4), "WSH": (38.9, -77.0),
}

# ── ML Feature Set ──
# Tier 1: Market anchor (1) — the Vegas line is the best starting point
# Tier 2: Market movement (1) — sharp money direction (closing - opening implied prob)
# Tier 3: Starting pitcher quality (4) — single biggest factor in MLB outcomes
# Tier 4: Bullpen quality + fatigue (4) — critical for holding leads
# Tier 5: Team quality (4) — rolling run differential + win percentage
# Tier 6: Recent form (2) — momentum (last 10 games)
# Tier 7: Situational (3) — home field, rest, travel, division

ML_FEATURES = [
    # ── Market anchor (1) ──
    "home_implied",                   # Market's win probability from closing ML
    # ── Market movement (1) ──
    "ml_implied_movement",            # Closing implied - opening implied (sharp money)
    # ── Starting pitcher quality (4) ──
    "h_pitcher_era_l5",               # Home starter ERA last 5 starts
    "a_pitcher_era_l5",               # Away starter ERA last 5 starts
    "h_pitcher_era_l20",              # Home starter ERA last 20 (talent baseline)
    "a_pitcher_era_l20",              # Away starter ERA last 20
    # ── Bullpen quality + fatigue (4) ──
    "h_bullpen_era_l5",               # Home bullpen ERA last 5 games
    "a_bullpen_era_l5",               # Away bullpen ERA last 5 games
    "h_bullpen_ip_l5",                # Home bullpen IP last 5 (fatigue proxy)
    "a_bullpen_ip_l5",                # Away bullpen IP last 5
    # ── Team quality (4) ──
    "h_rf10",                         # Home runs scored, rolling 10
    "a_rf10",                         # Away runs scored, rolling 10
    "h_ra10",                         # Home runs allowed, rolling 10
    "a_ra10",                         # Away runs allowed, rolling 10
    "h_winpct",                       # Home season win %
    "a_winpct",                       # Away season win %
    # ── Home/road splits (2) ──
    "h_home_rf",                      # Home scoring at home
    "a_away_rf",                      # Away scoring on road
    # ── Recent form (2) ──
    "h_form_l10",                     # Home wins last 10
    "a_form_l10",                     # Away wins last 10
    # ── Situational (3) ──
    "rest_diff",                      # Rest advantage (home - away days off)
    "travel_miles",                   # Away team travel distance
    "is_div",                         # Division game (familiarity)
    "is_dome",                        # Dome advantage (no weather)
]

# Features passed to XGBoost (exclude home_implied since it's the market baseline)
FEATURES_TRAINING = [f for f in ML_FEATURES if f not in ("home_implied",)]

TOTAL_ML_FEATURES = len(ML_FEATURES)


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


async def load_data(engine):
    """Load all completed MLB games with scores, lines, and pitcher stats."""
    log("Loading games...")
    async with engine.connect() as conn:
        r = await conn.execute(text("""
            SELECT g.id, s.year, g.date::date as game_date,
                   ht.abbreviation as ha, at.abbreviation as aa,
                   g.home_score, g.away_score,
                   (g.home_score - g.away_score) as margin,
                   g.roof_type, g.temperature, g.wind_speed,
                   g.venue, g.day_night,
                   g.home_team_id as htid, g.away_team_id as atid,
                   ht.division as hdiv, at.division as adiv
            FROM mlb.games g
            JOIN mlb.seasons s ON s.id = g.season_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.home_score IS NOT NULL AND g.away_score IS NOT NULL
            ORDER BY s.year, g.date, g.id
        """))
        games = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])

        log("Loading betting lines from consolidated table...")
        r = await conn.execute(text("""
            SELECT
                game_id,
                closing_home_ml AS home_moneyline,
                closing_away_ml AS away_moneyline,
                opening_home_ml AS opening_home_moneyline,
                opening_away_ml AS opening_away_moneyline,
                closing_spread AS spread,
                closing_ou AS over_under,
                closing_ou_sportsbook AS sportsbook
            FROM mlb.betting_lines_consolidated
            WHERE has_verified_ou = true
              AND closing_home_ml IS NOT NULL
              AND closing_away_ml IS NOT NULL
        """))
        lines = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])

    log(f"  Games: {len(games)} ({games.year.min()}-{games.year.max()})")
    log(f"  Lines with ML: {len(lines)}")

    games = games.rename(columns={"id": "game_id"})
    df = games.merge(lines, on="game_id", how="inner")
    log(f"  Merged: {len(df)} rows with ML data")

    # Moneyline implied probabilities (computed, not stored in DB)
    df["home_implied_probability"] = df["home_moneyline"].apply(_ml_implied)
    df["away_implied_probability"] = df["away_moneyline"].apply(_ml_implied)

    # ── Load pitcher game stats ──
    log("Loading pitcher game stats...")
    async with engine.connect() as conn:
        r = await conn.execute(text("""
            SELECT pgs.*, s.year, g.date::date as game_date
            FROM mlb.pitcher_game_stats pgs
            JOIN mlb.games g ON g.id = pgs.game_id
            JOIN mlb.seasons s ON s.id = g.season_id
            ORDER BY s.year, g.date, pgs.game_id
        """))
        pitcher_df = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])
    log(f"  Pitcher lines: {len(pitcher_df)}")

    return df, pitcher_df


def build_features(df: pd.DataFrame, pitcher_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Build ML-optimized features for binary win prediction.

    Strategy:
    1. Flatten to team-game view and compute rolling stats
    2. Add pitcher quality + bullpen features
    3. Add win percentage + recent form features
    4. Add market features (implied probabilities + movement)
    5. Add situational features
    """
    log("Building team-game table...")

    # ── 1. Flatten to team-game view ──
    rows = []
    for _, g in df.iterrows():
        rows.append({
            "game_id": g["game_id"], "year": g["year"], "game_date": g["game_date"],
            "team": g["ha"], "opp": g["aa"],
            "pf": g["home_score"], "pa": g["away_score"],
            "total": g["home_score"] + g["away_score"],
            "is_home": 1, "margin": g["margin"],
            "roof": g["roof_type"], "temp": g["temperature"], "wind": g["wind_speed"],
            "division": g["hdiv"], "venue": g["venue"],
        })
        rows.append({
            "game_id": g["game_id"], "year": g["year"], "game_date": g["game_date"],
            "team": g["aa"], "opp": g["ha"],
            "pf": g["away_score"], "pa": g["home_score"],
            "total": g["home_score"] + g["away_score"],
            "is_home": 0, "margin": -g["margin"],
            "roof": g["roof_type"], "temp": g["temperature"], "wind": g["wind_speed"],
            "division": g["adiv"], "venue": g["venue"],
        })
    tg = pd.DataFrame(rows).sort_values(["team", "game_date", "game_id"]).reset_index(drop=True)
    tg["game_date_dt"] = pd.to_datetime(tg["game_date"])

    # ── 2. Rolling stats per team ──
    log("  Rolling scoring stats (5, 10, 20 game windows)...")
    for window in [5, 10, 20]:
        tg[f"rf{window}"] = (
            tg.groupby("team")["pf"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )
        tg[f"ra{window}"] = (
            tg.groupby("team")["pa"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )

    # ── Win percentage (rolling + season) ──
    log("  Win percentage features...")
    tg["is_win"] = (tg["pf"] > tg["pa"]).astype(int)

    # Season-to-date win %
    tg["winpct"] = (
        tg.groupby("team")["is_win"]
        .transform(lambda x: x.shift(1).expanding().mean())
    )

    # Last 10 games win %
    tg["winpct_l10"] = (
        tg.groupby("team")["is_win"]
        .transform(lambda x: x.shift(1).rolling(10, min_periods=1).mean())
    )

    # ── Prior-season averages for early-game smoothing ──
    log("  Computing prior-season averages for early-season smoothing...")
    team_season_avg = tg.groupby(["team", "year"]).agg(
        prior_pf=("pf", "mean"),
        prior_pa=("pa", "mean"),
        prior_winpct=("is_win", "mean"),
    ).reset_index()
    prior_lookup = {}
    for _, row in team_season_avg.iterrows():
        prior_lookup[(row["team"], row["year"] - 1)] = (
            row["prior_pf"], row["prior_pa"], row["prior_winpct"]
        )

    tg["season_game_no"] = tg.groupby(["team", "year"]).cumcount() + 1

    for window in [5, 10, 20]:
        early_mask = tg["season_game_no"] <= window
        for team in tg.loc[early_mask, "team"].unique():
            team_year = tg.loc[tg["team"] == team, "year"].iloc[0]
            prior = prior_lookup.get((team, team_year - 1))
            if prior is None:
                continue
            prior_pf, prior_pa, prior_wp = prior
            team_early = early_mask & (tg["team"] == team)
            cnt = tg.loc[team_early, "season_game_no"]
            blend_w = (cnt - 1) / window
            tg.loc[team_early, f"rf{window}"] = (
                blend_w * tg.loc[team_early, f"rf{window}"] + (1 - blend_w) * prior_pf
            )
            tg.loc[team_early, f"ra{window}"] = (
                blend_w * tg.loc[team_early, f"ra{window}"] + (1 - blend_w) * prior_pa
            )

    # Blend winpct for first games too
    early_mask = tg["season_game_no"] <= 10
    for team in tg.loc[early_mask, "team"].unique():
        team_year = tg.loc[tg["team"] == team, "year"].iloc[0]
        prior = prior_lookup.get((team, team_year - 1))
        if prior is None:
            continue
        _, _, prior_wp = prior
        team_early = early_mask & (tg["team"] == team)
        cnt = tg.loc[team_early, "season_game_no"]
        blend_w = (cnt - 1) / 10
        tg.loc[team_early, "winpct"] = (
            blend_w * tg.loc[team_early, "winpct"] + (1 - blend_w) * prior_wp
        )

    # ── Rest days ──
    log("  Rest days...")
    tg["prev_date"] = tg.groupby("team")["game_date_dt"].shift(1)
    tg["rest"] = (tg["game_date_dt"] - tg["prev_date"]).dt.days.fillna(1).clip(0, 30)

    # ── Home/away splits ──
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

    # ── 3. Rejoin to game level ──
    log("  Rejoining to game level...")
    h = tg[tg["is_home"] == 1][
        ["game_id", "team", "rf5", "rf10", "rf20", "ra5", "ra10", "ra20",
         "rest", "winpct", "winpct_l10",
         "h_home_rf", "h_home_ra",
         "division", "season_game_no"]
    ].rename(columns={
        "team": "ha",
        "rf5": "h_rf5", "rf10": "h_rf10", "rf20": "h_rf20",
        "ra5": "h_ra5", "ra10": "h_ra10", "ra20": "h_ra20",
        "rest": "rest_h", "winpct": "h_winpct", "winpct_l10": "h_winpct_l10",
        "division": "hdiv",
    })

    a = tg[tg["is_home"] == 0][
        ["game_id", "team", "rf5", "rf10", "rf20", "ra5", "ra10", "ra20",
         "rest", "winpct", "winpct_l10",
         "a_home_rf", "a_home_ra",
         "division"]
    ].rename(columns={
        "team": "aa",
        "rf5": "a_rf5", "rf10": "a_rf10", "rf20": "a_rf20",
        "ra5": "a_ra5", "ra10": "a_ra10", "ra20": "a_ra20",
        "rest": "rest_a", "winpct": "a_winpct", "winpct_l10": "a_winpct_l10",
        "division": "adiv",
    })

    feats = h.merge(a, on="game_id")
    feats["rest_diff"] = feats["rest_h"] - feats["rest_a"]
    feats["winpct_diff"] = feats["h_winpct"] - feats["a_winpct"]
    feats["winpct_l10_diff"] = feats["h_winpct_l10"] - feats["a_winpct_l10"]
    feats["is_div"] = (feats["hdiv"] == feats["adiv"]).astype(int)

    # Recent form (wins last 10)
    feats["h_form_l10"] = (feats["h_winpct_l10"] * 10).fillna(5)
    feats["a_form_l10"] = (feats["a_winpct_l10"] * 10).fillna(5)

    # ── 4. Add original game data ──
    orig_cols = df[["game_id", "year", "game_date", "margin", "home_score", "away_score",
                     "home_moneyline", "away_moneyline",
                     "opening_home_moneyline", "opening_away_moneyline",
                     "home_implied_probability", "away_implied_probability",
                     "spread", "over_under",
                     "roof_type", "temperature", "wind_speed", "day_night", "venue"]].copy()
    feats = feats.merge(orig_cols, on="game_id")

    # Target: home team wins
    feats["home_wins"] = (feats["margin"] > 0).astype(int)

    # ── Market features ──
    feats["home_implied"] = feats["home_implied_probability"].fillna(0.5)
    feats["away_implied"] = feats["away_implied_probability"].fillna(0.5)

    # Opening implied probabilities (convert from moneyline)
    def ml_to_implied(ml):
        if pd.isna(ml) or ml == 0:
            return 0.5
        if ml > 0:
            return 100 / (ml + 100)
        else:
            return abs(ml) / (abs(ml) + 100)

    feats["opening_home_implied"] = feats["opening_home_moneyline"].apply(ml_to_implied)
    feats["opening_away_implied"] = feats["opening_away_moneyline"].apply(ml_to_implied)

    # Market movement: closing implied - opening implied (positive = sharp money on home)
    feats["ml_implied_movement"] = feats["home_implied"] - feats["opening_home_implied"]

    # ── 5. Situational features ──
    feats["game_date_dt"] = pd.to_datetime(feats["game_date"])
    feats["month"] = feats["game_date_dt"].dt.month
    feats["is_dome"] = (feats["roof_type"] == "dome").astype(int)

    # Travel distance
    feats["travel_miles"] = feats.apply(
        lambda r: haversine(
            *COORDS.get(r["aa"], (0, 0)), *COORDS.get(r["ha"], (0, 0))
        ),
        axis=1,
    )
    feats.loc[feats["travel_miles"] < 50, "travel_miles"] = 0

    # ── 6. Pitcher quality features ──
    if pitcher_df is not None and len(pitcher_df) > 0:
        log("  Computing pitcher quality features...")
        feats = add_pitcher_features(feats, pitcher_df)
    else:
        log("  ⚠ No pitcher stats available, filling with defaults")
        feats["h_pitcher_era_l5"] = 4.0
        feats["a_pitcher_era_l5"] = 4.0
        feats["h_pitcher_era_l20"] = 4.0
        feats["a_pitcher_era_l20"] = 4.0
        feats["h_bullpen_era_l5"] = 4.0
        feats["a_bullpen_era_l5"] = 4.0
        feats["h_bullpen_ip_l5"] = 8.0
        feats["a_bullpen_ip_l5"] = 8.0

    # ── 7. Fill remaining NaNs ──
    feats["a_away_rf"] = feats.get("a_away_rf", feats["a_rf10"])
    feats["h_home_rf"] = feats.get("h_home_rf", feats["h_rf10"])

    return feats


def add_pitcher_features(feats: pd.DataFrame, pitcher_df: pd.DataFrame) -> pd.DataFrame:
    """
    Add pitcher quality features focused on win prediction:
    - Starter ERA L5 (recent form)
    - Starter ERA L20 (talent baseline)
    - Bullpen ERA L5 (recent reliever quality)
    - Bullpen IP L5 (fatigue proxy)
    """
    ps = pitcher_df.copy()
    ps["game_date_dt"] = pd.to_datetime(ps["game_date"])
    ps["era"] = np.where(ps["ip"].notna() & (ps["ip"] > 0), ps["er"].fillna(0) / ps["ip"] * 9, np.nan)

    # ── Prior-season averages ──
    # Pitcher prior
    pitcher_season = ps[ps["is_starter"] & (ps["ip"] > 0)].groupby(["pitcher_mlb_id", "year"]).agg(
        avg_era=("era", "mean"),
    ).reset_index()
    pitcher_prior = {}
    for _, row in pitcher_season.iterrows():
        pitcher_prior[(row["pitcher_mlb_id"], row["year"] - 1)] = row["avg_era"]

    # Bullpen prior (per team, per season)
    bullpen_season = ps[~ps["is_starter"] & (ps["ip"] > 0)].groupby(["team_abbr", "year"]).agg(
        avg_bp_era=("era", "mean"),
        avg_bp_ip=("ip", "mean"),
    ).reset_index()
    bullpen_prior_era = {}
    bullpen_prior_ip = {}
    for _, row in bullpen_season.iterrows():
        bullpen_prior_era[(row["team_abbr"], row["year"] - 1)] = row["avg_bp_era"]
        bullpen_prior_ip[(row["team_abbr"], row["year"] - 1)] = row["avg_bp_ip"] * 5

    # Defaults
    feats["h_pitcher_era_l5"] = 4.0
    feats["a_pitcher_era_l5"] = 4.0
    feats["h_pitcher_era_l20"] = 4.0
    feats["a_pitcher_era_l20"] = 4.0
    feats["h_bullpen_era_l5"] = 4.0
    feats["a_bullpen_era_l5"] = 4.0
    feats["h_bullpen_ip_l5"] = 8.0
    feats["a_bullpen_ip_l5"] = 8.0

    # ── Starter ERA L5 + L20 ──
    starters = ps[ps["is_starter"] & (ps["ip"] > 0) & ps["pitcher_mlb_id"].notna()].copy()
    if len(starters) > 0:
        starters = starters.sort_values(["pitcher_mlb_id", "game_date_dt", "game_id"])
        starters["era_l5"] = (
            starters.groupby("pitcher_mlb_id")["era"]
            .transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
        )
        starters["era_l20"] = (
            starters.groupby("pitcher_mlb_id")["era"]
            .transform(lambda x: x.shift(1).rolling(20, min_periods=1).mean())
        )

        starter_map = starters[(
            starters["era_l5"].notna() | starters["era_l20"].notna()
        )].set_index("game_id")[["era_l5", "era_l20", "team_abbr", "pitcher_mlb_id"]].copy()

        starter_with_year = starter_map.reset_index().merge(
            feats[["game_id", "year"]], on="game_id", how="left"
        )
        starter_with_year = starter_with_year[
            starter_with_year["pitcher_mlb_id"].notna()
            & starter_with_year["year"].notna()
        ].copy()
        starter_with_year["year"] = starter_with_year["year"].astype(int)
        starter_with_year["prior_era"] = starter_with_year.apply(
            lambda r: pitcher_prior.get(
                (r["pitcher_mlb_id"], int(r["year"]) - 1)
            ) or np.nan,
            axis=1
        )

        for team_col, prefix in [("ha", "h"), ("aa", "a")]:
            team_games = feats[["game_id", team_col]].copy()
            merged = team_games.merge(
                starter_with_year[["game_id", "era_l5", "era_l20", "team_abbr", "prior_era"]],
                on="game_id", how="left"
            )
            mask = merged["team_abbr"] == merged[team_col]
            l5 = merged.loc[mask, "era_l5"].copy()
            l20 = merged.loc[mask, "era_l20"].copy()
            prior = merged.loc[mask, "prior_era"].fillna(4.0)
            feats[f"{prefix}_pitcher_era_l5"] = l5.fillna(prior)
            feats[f"{prefix}_pitcher_era_l20"] = l20.fillna(prior)

    # ── Bullpen ERA L5 + IP L5 ──
    bullpen = ps[~ps["is_starter"] & (ps["ip"] > 0)].copy()
    # Filter NaN team_abbr (shouldn't happen but be safe)
    bullpen = bullpen[bullpen["team_abbr"].notna()].copy()
    if len(bullpen) > 0:
        bp_per_game = bullpen.groupby(["game_id", "team_abbr"]).agg(
            bp_era_group=("era", lambda x: np.nanmean(x)),
            bp_ip=("ip", "sum"),
            game_date=("game_date_dt", "first"),
        ).reset_index()

        bp_per_game = bp_per_game.sort_values(["team_abbr", "game_date", "game_id"])
        bp_per_game["bp_era_l5_raw"] = (
            bp_per_game.groupby("team_abbr")["bp_era_group"]
            .transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
        )
        bp_per_game["bp_ip_l5_raw"] = (
            bp_per_game.groupby("team_abbr")["bp_ip"]
            .transform(lambda x: x.shift(1).rolling(5, min_periods=1).sum())
        )

        bp_df = bp_per_game[bp_per_game["bp_era_l5_raw"].notna()].set_index("game_id")[
            ["bp_era_l5_raw", "bp_ip_l5_raw", "team_abbr"]
        ]

        bp_with_year = bp_df.reset_index().merge(
            feats[["game_id", "year"]], on="game_id", how="left"
        )
        bp_with_year = bp_with_year[
            bp_with_year["year"].notna()
        ].copy()
        bp_with_year["year"] = bp_with_year["year"].astype(int)
        bp_with_year["prior_bp_era"] = bp_with_year.apply(
            lambda r: bullpen_prior_era.get(
                (r["team_abbr"], int(r["year"]) - 1)
            ) or np.nan,
            axis=1
        )
        bp_with_year["prior_bp_ip"] = bp_with_year.apply(
            lambda r: bullpen_prior_ip.get(
                (r["team_abbr"], int(r["year"]) - 1)
            ) or np.nan,
            axis=1
        )

        for team_col, prefix in [("ha", "h"), ("aa", "a")]:
            team_games = feats[["game_id", team_col]].copy()
            merged = team_games.merge(
                bp_with_year[["game_id", "bp_era_l5_raw", "bp_ip_l5_raw", "team_abbr",
                              "prior_bp_era", "prior_bp_ip"]],
                on="game_id", how="left"
            )
            mask = merged["team_abbr"] == merged[team_col]
            era5 = merged.loc[mask, "bp_era_l5_raw"].copy()
            ip5 = merged.loc[mask, "bp_ip_l5_raw"].copy()
            prior_era = merged.loc[mask, "prior_bp_era"].fillna(4.0)
            prior_ip = merged.loc[mask, "prior_bp_ip"].fillna(8.0)
            feats[f"{prefix}_bullpen_era_l5"] = era5.fillna(prior_era)
            feats[f"{prefix}_bullpen_ip_l5"] = ip5.fillna(prior_ip)

    return feats


def evaluate_ml(
    y_true: np.ndarray,
    y_pred_prob: np.ndarray,
    closing_implied: np.ndarray = None,
    opening_implied: np.ndarray = None,
) -> dict:
    """
    Evaluate ML predictions against both market baselines.

    Metrics:
    - Accuracy (vs our model)
    - AUC, Brier, Log Loss
    - Closing market baseline (most efficient)
    - Opening market baseline (what we'd bet against live)
    """
    n = len(y_true)
    y_pred = (y_pred_prob > 0.5).astype(int)

    accuracy = float(round(accuracy_score(y_true, y_pred), 4))
    auc = float(round(roc_auc_score(y_true, y_pred_prob), 4))
    brier = float(round(brier_score_loss(y_true, y_pred_prob), 4))
    ll = float(round(log_loss(y_true, y_pred_prob), 4))

    result = {
        "n": int(n),
        "accuracy": accuracy,
        "correct": int((y_pred == y_true).sum()),
        "incorrect": int((y_pred != y_true).sum()),
        "auc": auc,
        "brier": brier,
        "log_loss": ll,
    }

    # Baseline: closing market (maximally efficient)
    if closing_implied is not None:
        bl_pred = (closing_implied > 0.5).astype(int)
        bl_correct = int((bl_pred == y_true).sum())
        result["closing_baseline"] = {
            "accuracy": float(round(bl_correct / n, 4)),
            "correct": bl_correct,
            "incorrect": n - bl_correct,
        }

    # Baseline: opening market (what we bet against live)
    if opening_implied is not None:
        bl_pred = (opening_implied > 0.5).astype(int)
        bl_correct = int((bl_pred == y_true).sum())
        result["opening_baseline"] = {
            "accuracy": float(round(bl_correct / n, 4)),
            "correct": bl_correct,
            "incorrect": n - bl_correct,
        }

    return result


def _compute_threshold_roi(y_true, y_pred_prob, threshold, home_moneylines=None):
    """
    Compute ROI betting when model probability > threshold.
    Accounts for actual moneyline odds (-150 = risk 150 to win 100).
    Flat 1 unit per bet.
    """
    bets = []
    for i in range(len(y_true)):
        prob = y_pred_prob[i]
        ml = None
        if home_moneylines is not None and not np.isnan(home_moneylines[i]):
            ml = home_moneylines[i]

        if prob > threshold:
            # Bet home — payout depends on odds
            won = y_true[i] == 1
            if ml is not None and not np.isnan(ml):
                if ml < 0:
                    payout = 100 / abs(ml)  # risk 1 to win payout
                else:
                    payout = ml / 100  # risk 1 to win payout
            else:
                payout = 1.0  # even money fallback
            bets.append(payout if won else -1.0)
        elif (1 - prob) > threshold:
            # Bet away
            won = y_true[i] == 0
            bets.append(1.0 if won else -1.0)

    if not bets:
        return {"bets": 0, "roi": 0.0, "wins": 0, "losses": 0}

    total_return = sum(bets)
    total_stake = len(bets)
    roi = round(100 * total_return / total_stake, 1)
    wins = sum(1 for b in bets if b > 0)
    losses = len(bets) - wins

    return {
        "bets": len(bets),
        "wins": wins,
        "losses": losses,
        "pct": round(100 * wins / len(bets), 1),
        "roi": roi,
    }


async def run_backtest(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    test_year: int = 2025,
    train_years: list[int] = None,
    xgb_params: dict = None,
) -> dict:
    """Run XGBoost ML backtest for a single year."""
    if train_years is None:
        train_years = [y for y in range(2021, test_year)]

    if xgb_params is None:
        xgb_params = {
            "n_estimators": 600,
            "max_depth": 5,
            "learning_rate": 0.03,
            "subsample": 0.7,
            "colsample_bytree": 0.7,
            "reg_alpha": 1.0,
            "reg_lambda": 2.0,
            "min_child_weight": 2,
            "random_state": 42,
            "n_jobs": -1,
            "verbosity": 0,
        }

    # ── Split ──
    tr_mask = feats["year"].isin(train_years)
    te_mask = feats["year"] == test_year
    tr = feats[tr_mask].sort_values(["game_date"]).reset_index(drop=True)
    te = feats[te_mask].sort_values(["month", "game_date"]).reset_index(drop=True)

    log(f"  Train: {len(tr)} games ({train_years})")
    log(f"  Test:  {len(te)} games ({test_year})")

    if len(tr) < 50 or len(te) < 10:
        log("  ⚠ Not enough data, skipping")
        return {"error": "insufficient_data"}

    # ── Residual Training: predict edge vs OPENING line (not closing) ──
    # The opening line is less efficient — more edge to find
    # Target: home_wins - opening_home_implied
    # Positive = opening line underpriced home team
    opening_home_implied_te = te["opening_home_implied"].values
    opening_home_implied_tr = tr["opening_home_implied"].values
    home_implied_te = te["home_implied"].values

    X_tr = tr[FEATURES_TRAINING].fillna(0).astype(np.float32)
    y_tr_residual = tr["home_wins"].values - opening_home_implied_tr

    X_te = te[FEATURES_TRAINING].fillna(0).astype(np.float32)
    y_te = te["home_wins"].values

    # ── Time-weighted training ──
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

    # ── Train residual regressor ──
    residual_model = xgb.XGBRegressor(
        objective="reg:squarederror",
        **xgb_params,
    )
    residual_model.fit(X_tr, y_tr_residual, sample_weight=w, verbose=False)

    # ── Save model for live prediction ──
    import pickle as _pickle
    # ── Predict: opening line baseline + model's estimated edge ──
    pred_residual = residual_model.predict(X_te)
    # Combine with OPENING line for final probability
    pred_prob = np.clip(opening_home_implied_te + pred_residual, 0.01, 0.99)
    pred_class = (pred_prob > 0.5).astype(int)

    # ── Evaluate ──
    # Compare to BOTH baselines: opening (used in live) and closing (market efficiency)
    opening_home_implied_te = te["opening_home_implied"].values
    eval_result = evaluate_ml(
        y_te, pred_prob,
        closing_implied=home_implied_te,
        opening_implied=opening_home_implied_te,
    )

    # ── ROI at various thresholds (with actual moneyline odds) ──
    home_moneylines = te["home_moneyline"].values
    thresholds = [0.5, 0.55, 0.6, 0.65]
    roi_results = {}
    for thresh in thresholds:
        roi_results[str(thresh)] = _compute_threshold_roi(
            y_te, pred_prob, thresh, home_moneylines=home_moneylines
        )

    # ── Monthly breakdown ──
    monthly = []
    for m in sorted(te["month"].unique()):
        sub = te[te["month"] == m]
        if len(sub) < 5:
            continue
        sub_opening = sub["opening_home_implied"].values
        sub_residual = residual_model.predict(
            sub[FEATURES_TRAINING].fillna(0).astype(np.float32)
        )
        sub_prob = np.clip(sub_opening + sub_residual, 0.01, 0.99)
        sub_y = sub["home_wins"].values
        sub_acc = float(round(accuracy_score(sub_y, (sub_prob > 0.5).astype(int)), 4))
        monthly.append({
            "month": int(m),
            "games": int(len(sub)),
            "accuracy": sub_acc,
        })

    # ── Feature importance ──
    all_feats = list(FEATURES_TRAINING) + ["home_implied"]
    all_imps = list(residual_model.feature_importances_) + [float(np.mean(residual_model.feature_importances_))]
    imp = pd.DataFrame({
        "feature": all_feats,
        "importance": all_imps,
    }).sort_values("importance", ascending=False)

    # ── Results ──
    result = {
        "test_year": test_year,
        "train_years": train_years,
        "feature_set": "mlb_ml",
        "total_games": eval_result["n"],
        "accuracy": eval_result["accuracy"],
        "correct": eval_result["correct"],
        "incorrect": eval_result["incorrect"],
        "auc": eval_result["auc"],
        "brier": eval_result["brier"],
        "log_loss": eval_result["log_loss"],
        "opening_baseline": eval_result.get("opening_baseline", {}),
        "closing_baseline": eval_result.get("closing_baseline", {}),
        "roi_by_threshold": roi_results,
        "monthly": monthly,
        "feature_importance": [
            {"feature": str(r["feature"]), "importance": float(round(float(r["importance"]), 4))}
            for _, r in imp.iterrows()
        ],
    }

    print_ml_summary(result, te)

    return result


def print_ml_summary(result: dict, te: pd.DataFrame):
    """Pretty-print ML backtest results."""
    print(f"\n{'='*62}")
    print(f"MLB MONEYLINE BACKTEST — {result['test_year']} Season")
    print(f"Features: {result['feature_set']} ({len(result['feature_importance'])} feats)")
    print(f"Train: {result['train_years']}")
    print(f"{'='*62}")

    print(f"\n🏆 WINNER PREDICTION")
    print(f"  Games:     {result['total_games']}")
    print(f"  Accuracy:  {result['accuracy']:.4f} ({result['correct']}-{result['incorrect']})")
    print(f"  AUC:       {result['auc']:.4f}")
    print(f"  Log Loss:  {result['log_loss']:.4f}")
    print(f"  Brier:     {result['brier']:.4f}")

    if "closing_baseline" in result:
        cl = result["closing_baseline"]
        print(f"\n📊 VS CLOSING MARKET (most efficient)")
        print(f"  Closing:   {cl['accuracy']:.4f} ({cl['correct']}-{cl['incorrect']})")
        diff = (result['accuracy'] - cl['accuracy']) * 100
        if diff < 0:
            print(f"  ⚠ Closing line beats XGBoost by {abs(diff):.1f}%")
        else:
            print(f"  ✅ XGBoost beats closing line by {diff:+.1f}%")

    if "opening_baseline" in result:
        ol = result["opening_baseline"]
        print(f"  Opening:   {ol['accuracy']:.4f} ({ol['correct']}-{ol['incorrect']})")
        diff = (result['accuracy'] - ol['accuracy']) * 100
        if diff < 0:
            print(f"  ⚠ Opening line beats XGBoost by {abs(diff):.1f}%")
        else:
            print(f"  ✅ XGBoost beats opening line by {diff:+.1f}% — LIVE EDGE!")

    print(f"\n💰 ROI BY CONFIDENCE THRESHOLD")
    print(f"  {'Threshold':>10s}  {'Bets':>5s}  {'W-L':>10s}  {'Win%':>6s}  {'ROI':>6s}")
    for thresh_str, roi_info in sorted(result["roi_by_threshold"].items()):
        if roi_info["bets"] == 0:
            continue
        wl = f"{roi_info['wins']}-{roi_info['losses']}"
        print(f"  {thresh_str:>10s}  {roi_info['bets']:>5d}  {wl:>10s}  {roi_info['pct']:>5.1f}%  {roi_info['roi']:>5.1f}%")

    if result["monthly"]:
        print(f"\n📅 MONTHLY BREAKDOWN")
        print(f"  {'Month':>6s}  {'Games':>5s}  {'Acc':>6s}")
        for m in result["monthly"]:
            print(f"  {m['month']:>6d}  {m['games']:>5d}  {m['accuracy']:.4f}")

    print(f"\n🔑 TOP FEATURES")
    for i, fi in enumerate(result["feature_importance"][:15]):
        bar = "█" * int(fi["importance"] * 100)
        print(f"  {i+1:2d}. {fi['feature']:>20s}: {fi['importance']:.4f} {bar}")

    print()


async def run_all_years(
    test_years: list[int] = None,
    train_from: int = 2021,
):
    """Run ML backtests across multiple years."""
    if test_years is None:
        test_years = [2025, 2026]

    t0 = datetime.now()
    engine = create_async_engine(DB)

    df, pitcher_df = await load_data(engine)
    log(f"\nBuilding features...")
    feats = build_features(df, pitcher_df)
    log(f"Feature table: {len(feats)} rows, {len(feats.columns)} columns")

    all_results = []

    for year in test_years:
        log(f"\n{'─'*62}")
        log(f"Testing year={year}")
        log(f"{'─'*62}")
        train = [y for y in range(train_from, year)]
        result = await run_backtest(df, feats, test_year=year, train_years=train)
        if "error" not in result:
            all_results.append(result)

    await engine.dispose()

    # Summary
    print(f"\n{'='*62}")
    print("MLB MONEYLINE BACKTEST — ALL YEARS")
    print(f"{'='*62}")
    print(f"\n{'Year':>4s}  {'Games':>5s}  {'Acc':>6s}  {'AUC':>5s}  {'Brier':>6s}  {'LogL':>6s}  {'W/L':>10s}  {'VsOpen':>8s}  {'VsClose':>8s}")
    print("─" * 76)
    total_c = 0
    total_i = 0
    for r in sorted(all_results, key=lambda x: x["test_year"]):
        co = r.get("closing_baseline", {})
        oo = r.get("opening_baseline", {})
        vs_close = (r["accuracy"] - co.get("accuracy", 0)) * 100
        vs_open = (r["accuracy"] - oo.get("accuracy", 0)) * 100
        vs_str = f"{vs_open:+.1f}%/{vs_close:+.1f}%"
        wl = f"{r['correct']}-{r['incorrect']}"
        print(f"  {r['test_year']:>4d}  {r['total_games']:>5d}  {r['accuracy']:.4f}  {r['auc']:.4f}  {r['brier']:.4f}  {r['log_loss']:.4f}  {wl:>10s}  {vs_open:>+7.1f}%  {vs_close:>+7.1f}%")
        total_c += r["correct"]
        total_i += r["incorrect"]

    tp = total_c + total_i
    print(f"  {'─'*70}")
    print(f"  {'TOTAL':>4s}  {tp:>5d}  {round(100*total_c/max(tp,1),1):>5.1f}%  ({total_c:>4d}-{total_i:>4d})")

    # Save results to DB (+ unique PKL)
    if _DB_HELPERS_AVAILABLE:
        pkl_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
        pkl_dir.mkdir(parents=True, exist_ok=True)

        combined_results = {
            "run_time": str(datetime.now() - t0),
            "results": all_results,
        }

        training_id = save_training_run(
            sport="mlb",
            model_type="ml",
            results_json=combined_results,
            pkl_filename="",
            algorithm="xgboost",
            description="MLB ML year-by-year backtest",
            test_year=max(all_results, key=lambda r: r["test_year"])["test_year"] if all_results else None,
            train_years=test_years,
        )

        pkl_name = f"{training_id}.pkl"
        pkl_path = pkl_dir / pkl_name

        latest_year = max(all_results, key=lambda r: r["test_year"])["test_year"]
        src_pkl = pkl_dir / f"mlb_ml_{latest_year}.pkl"
        if src_pkl.exists():
            shutil.copy2(str(src_pkl), str(pkl_path))

        update_pkl_filename("mlb", training_id, pkl_name)
        log(f"\nResults saved to DB (training_id={training_id}, pkl={pkl_name})")
    else:
        out = {
            "run_time": str(datetime.now() - t0),
            "results": all_results,
        }
        out_path = "/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb_ml_backtest_results.json"
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2, default=str)
        log(f"\nResults saved to {out_path}")
    log(f"Total time: {datetime.now() - t0}")


async def run_single(test_year: int = 2025, train_years: list[int] = None):
    """Run a single ML backtest for quick iteration."""
    engine = create_async_engine(DB)
    df, pitcher_df = await load_data(engine)
    feats = build_features(df, pitcher_df)
    await run_backtest(df, feats, test_year=test_year, train_years=train_years)
    await engine.dispose()
async def train_production_model(save_path: str = "/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb_ml_residual_model_prod.pkl"):
    """
    Train the final production ML model on all available data.
    Saves model + feature names + config for use in live predictions.
    """
    engine = create_async_engine(DB)
    df, pitcher_df = await load_data(engine)
    feats = build_features(df, pitcher_df)
    await engine.dispose()

    # Use all data for training
    X = feats[FEATURES_TRAINING].fillna(0).astype(np.float32)
    opening_implied = feats["opening_home_implied"].values
    y_residual = feats["home_wins"].values - opening_implied

    # Train on all years with recency weighting
    n = len(feats)
    w = np.ones(n)
    latest_year = feats["year"].max()
    for i in range(n):
        y = feats.at[feats.index[i], "year"]
        years_back = latest_year - y
        if years_back <= 1:
            w[i] = 4.0
        elif years_back <= 2:
            w[i] = 3.0
        elif years_back <= 3:
            w[i] = 2.0
        elif years_back <= 5:
            w[i] = 1.5

    params = {
        "n_estimators": 600,
        "max_depth": 5,
        "learning_rate": 0.03,
        "subsample": 0.7,
        "colsample_bytree": 0.7,
        "reg_alpha": 1.0,
        "reg_lambda": 2.0,
        "min_child_weight": 2,
        "random_state": 42,
        "n_jobs": -1,
        "verbosity": 0,
    }

    model = xgb.XGBRegressor(objective="reg:squarederror", **params)
    model.fit(X, y_residual, sample_weight=w, verbose=False)

    # Save model + metadata
    import pickle as _pickle
    payload = {
        "model": model,
        "features": FEATURES_TRAINING,
        "train_years": sorted(feats["year"].unique()),
        "total_games": len(feats),
        "trained_at": str(datetime.now()),
    }
    with open(save_path, "wb") as f:
        _pickle.dump(payload, f)

    log(f"Production model saved to {save_path}")
    log(f"  Trained on {len(feats)} games ({min(feats['year'])}-{max(feats['year'])})")
    log(f"  Features: {len(FEATURES_TRAINING)}")

    # Quick evaluation on training data (in-sample — just to verify it learned)
    pred_residual = model.predict(X)
    pred_prob = np.clip(opening_implied + pred_residual, 0.01, 0.99)
    y_true = feats["home_wins"].values
    from sklearn.metrics import accuracy_score, roc_auc_score as _roc_auc
    acc = accuracy_score(y_true, (pred_prob > 0.5).astype(int))
    auc = _roc_auc(y_true, pred_prob)
    log(f"  In-sample accuracy: {acc:.4f}, AUC: {auc:.4f}")




# ── Model management & inference (imported by mlb_engine.py) ──────────

ML_MODEL_PATH = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb_ml_residual_model_prod.pkl")
_ml_model = None


def set_model_path(path: str):
    global ML_MODEL_PATH, _ml_model
    ML_MODEL_PATH = Path(path)
    _ml_model = None


def _load_ml_model():
    global _ml_model
    if _ml_model is not None:
        return _ml_model
    if not ML_MODEL_PATH.exists():
        raise FileNotFoundError(f"ML model not found at {ML_MODEL_PATH}")
    with open(ML_MODEL_PATH, "rb") as f:
        payload = pickle.load(f)
    model = payload["model"] if isinstance(payload, dict) else payload
    _ml_model = model
    log(f"Loaded ML model ({len(FEATURES_TRAINING)} features)")
    return _ml_model


def _ml_implied(v):
    if v is None or v == 0: return 0.5
    return abs(v) / (abs(v) + 100) if v < 0 else 100.0 / (v + 100)


async def predict_ml(game_id: int, home_abbr: str, away_abbr: str,
                      yr: int, game_date: str,
                      home_stats, away_stats,
                      line_obj,
                      conn: Optional[asyncpg.Connection] = None) -> tuple[Optional[float], float, float]:
    """
    Predict home win probability for one MLB game.

    Uses a residual approach: opening implied + model-predicted residual.
    Returns (home_win_prob, confidence, edge_over_market) or (None, 0.0, 0.0).
    """
    try:
        model = _load_ml_model()
    except FileNotFoundError:
        logger.warning("ML model not found at %s", ML_MODEL_PATH)
        return None, 0.0, 0.0

    gd_obj = date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
    _close_conn = False
    if conn is None:
        conn = await asyncpg.connect(DSN)
        _close_conn = True
    try:

        # Market
        h_ml = getattr(line_obj, 'home_moneyline', None)
        hi = _ml_implied(h_ml)

        r = await conn.fetchrow(
            "SELECT opening_home_moneyline FROM mlb.betting_lines_consolidated WHERE game_id=$1 LIMIT 1", game_id)
        ohi = _ml_implied(r['opening_home_moneyline']) if (r and r['opening_home_moneyline'] is not None) else hi
        mm = hi - ohi

        # Pitcher
        async def _p(abbr, lim):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.era) FROM (
                    SELECT pgs.er * 9.0 / NULLIF(pgs.ip, 0) as era FROM mlb.pitcher_game_stats pgs
                    JOIN mlb.games g ON g.id=pgs.game_id JOIN mlb.seasons s ON s.id=g.season_id
                    WHERE pgs.team_abbr=$1 AND pgs.is_starter=true AND pgs.ip > 0
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                    ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 4.50

        async def _bp_era(abbr, lim):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.era) FROM (
                    SELECT pgs.er * 9.0 / NULLIF(pgs.ip, 0) as era FROM mlb.pitcher_game_stats pgs
                    JOIN mlb.games g ON g.id=pgs.game_id JOIN mlb.seasons s ON s.id=g.season_id
                    WHERE pgs.team_abbr=$1 AND pgs.is_starter=false AND pgs.ip > 0
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                    ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 4.50

        async def _bp_ip(abbr, lim):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.game_ip) FROM (
                    SELECT g.id, SUM(pgs.ip) as game_ip FROM mlb.pitcher_game_stats pgs
                    JOIN mlb.games g ON g.id=pgs.game_id JOIN mlb.seasons s ON s.id=g.season_id
                    WHERE pgs.team_abbr=$1 AND pgs.is_starter=false AND pgs.ip IS NOT NULL
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                    GROUP BY g.id
                    ORDER BY MAX(g.date) DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 3.0

        h_pl5, a_pl5 = await _p(home_abbr, 5), await _p(away_abbr, 5)
        h_pl20, a_pl20 = await _p(home_abbr, 20), await _p(away_abbr, 20)
        h_be, a_be = await _bp_era(home_abbr, 5), await _bp_era(away_abbr, 5)
        h_bi, a_bi = await _bp_ip(home_abbr, 5), await _bp_ip(away_abbr, 5)

        # Rolling stats
        async def _avg(abbr, sel, lim):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.v) FROM (
                    SELECT {sel} as v FROM mlb.games g
                    JOIN mlb.seasons s ON s.id=g.season_id
                    JOIN mlb.teams ht ON ht.id=g.home_team_id JOIN mlb.teams at ON at.id=g.away_team_id
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL
                    ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.0

        h_rf10 = await _avg(home_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END", 10)
        a_rf10 = await _avg(away_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END", 10)
        h_ra10 = await _avg(home_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END", 10)
        a_ra10 = await _avg(away_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END", 10)

        async def _wp(abbr, lim=999):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.w) FROM (
                    SELECT CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int
                           ELSE (g.away_score>g.home_score)::int END as w
                    FROM mlb.games g JOIN mlb.seasons s ON s.id=g.season_id
                    JOIN mlb.teams ht ON ht.id=g.home_team_id JOIN mlb.teams at ON at.id=g.away_team_id
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL
                    ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.5

        h_wp, a_wp = await _wp(home_abbr, 999), await _wp(away_abbr, 999)
        h_f10, a_f10 = await _wp(home_abbr, 10), await _wp(away_abbr, 10)

        # Home/road splits
        async def _hrf(abbr, lim=999):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.v) FROM (
                    SELECT g.home_score as v FROM mlb.games g
                    JOIN mlb.seasons s ON s.id=g.season_id JOIN mlb.teams ht ON ht.id=g.home_team_id
                    WHERE ht.abbreviation=$1
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL
                    ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.0

        async def _arf(abbr, lim=999):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.v) FROM (
                    SELECT g.away_score as v FROM mlb.games g
                    JOIN mlb.seasons s ON s.id=g.season_id JOIN mlb.teams at ON at.id=g.away_team_id
                    WHERE at.abbreviation=$1
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL
                    ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.0

        hh_rf = await _hrf(home_abbr)
        aa_rf = await _arf(away_abbr)

        # Rest
        async def _last(abbr):
            r = await conn.fetchrow("""
                SELECT MAX(g.date::date) FROM mlb.games g JOIN mlb.seasons s ON s.id=g.season_id
                WHERE (g.home_team_id=(SELECT id FROM mlb.teams WHERE abbreviation=$1)
                    OR g.away_team_id=(SELECT id FROM mlb.teams WHERE abbreviation=$1))
                  AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                  AND g.home_score IS NOT NULL
            """, abbr, yr, gd_obj)
            return r[0] if r and r[0] else None

        hl, al = await _last(home_abbr), await _last(away_abbr)
        gd = date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
        rh = (gd - hl).days if (gd and hl) else 3
        ra = (gd - al).days if (gd and al) else 3
        rd = rh - ra

        # Travel
        hc, ac = COORDS.get(home_abbr, (0, 0)), COORDS.get(away_abbr, (0, 0))
        R = 3958.8; dl = math.radians(ac[0]-hc[0]); dn = math.radians(ac[1]-hc[1])
        a = math.sin(dl/2)**2 + math.cos(math.radians(hc[0]))*math.cos(math.radians(ac[0]))*math.sin(dn/2)**2
        tm = R*2*math.atan2(math.sqrt(a), math.sqrt(1-a))
        if tm < 50: tm = 0.0

        # Division
        rows = await conn.fetch("SELECT abbreviation, division FROM mlb.teams")
        divs = {r['abbreviation']: r['division'] for r in rows}
        idiv = 1 if (divs.get(home_abbr) and divs.get(away_abbr) and divs[home_abbr] == divs[away_abbr]) else 0
        gr = await conn.fetchrow("SELECT roof_type FROM mlb.games WHERE id=$1", game_id)
        idome = 1 if (gr and gr['roof_type'] in ('dome', 'indoor')) else 0

        vals = {
            "ml_implied_movement": mm,
            "h_pitcher_era_l5": h_pl5, "a_pitcher_era_l5": a_pl5,
            "h_pitcher_era_l20": h_pl20, "a_pitcher_era_l20": a_pl20,
            "h_bullpen_era_l5": h_be, "a_bullpen_era_l5": a_be,
            "h_bullpen_ip_l5": h_bi, "a_bullpen_ip_l5": a_bi,
            "h_rf10": h_rf10, "a_rf10": a_rf10,
            "h_ra10": h_ra10, "a_ra10": a_ra10,
            "h_winpct": h_wp, "a_winpct": a_wp,
            "h_home_rf": hh_rf, "a_away_rf": aa_rf,
            "h_form_l10": h_f10*10, "a_form_l10": a_f10*10,
            "rest_diff": rd, "travel_miles": tm,
            "is_div": idiv, "is_dome": idome,
        }
        x = np.array([[vals.get(f, 0.0) for f in FEATURES_TRAINING]], dtype=np.float32)
        resid = float(model.predict(x)[0])
        prob = float(np.clip(ohi + resid, 0.01, 0.99))
        edge = prob - hi
        conf = min(0.5 + abs(prob - 0.5) + abs(edge) * 0.5, 0.95)
        return round(prob, 4), round(conf, 2), round(edge, 4)

    except Exception as e:
        logger.error(f"ML pred failed [{game_id} {home_abbr}@{away_abbr}]: {e}")
        return None, 0.50, 0.0
    finally:
        if _close_conn and conn: await conn.close()




async def train_model(year: int, train_years: list[int]) -> object:
    """Train ML model from scratch on given years. Returns fitted CalibratedClassifierCV."""
    features = FEATURES_TRAINING
    engine = create_async_engine(DB)
    df, pitcher_df = await load_data(engine)
    feats = build_features(df, pitcher_df)
    await engine.dispose()
    
    tr_all = feats[feats["year"].isin(train_years)].reset_index(drop=True)
    log(f"Training ML on {len(tr_all)} games ({train_years})")
    
    opening_implied = tr_all["opening_home_implied"].values
    y_residual = tr_all["home_wins"].values - opening_implied
    q25, q75 = np.percentile(y_residual, [1, 99])
    valid = (y_residual >= q25) & (y_residual <= q75)
    tr_all = tr_all[valid].reset_index(drop=True)
    y_residual = y_residual[valid]
    
    X_tr = tr_all[features].fillna(0).astype(np.float32)
    
    w = np.ones(len(tr_all))
    for i in range(len(tr_all)):
        s = tr_all.at[tr_all.index[i], "year"]
        years_back = year - s
        if years_back <= 1: w[i] = 4.0
        elif years_back <= 2: w[i] = 3.0
        elif years_back <= 3: w[i] = 2.0
        elif years_back <= 5: w[i] = 1.5
    
    model = xgb.XGBRegressor(
        n_estimators=600, max_depth=5, learning_rate=0.03,
        subsample=0.7, colsample_bytree=0.7,
        reg_alpha=1.0, reg_lambda=2.0,
        min_child_weight=2,
        random_state=42, n_jobs=-1, verbosity=0,
    )
    model.fit(X_tr, y_residual, sample_weight=w, verbose=False)
    log(f"ML model trained ({len(features)} features)")
    return model

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MLB XGBoost Moneyline Backtester")
    parser.add_argument("--test-year", type=int, default=2025, help="Year to test on")
    parser.add_argument("--train-from", type=int, default=2021,
                        help="Earliest training year")
    parser.add_argument("--mode", type=str, default="single",
                        choices=["single", "all", "train-production"],
                        help="Single year, all years, or train production model")
    args = parser.parse_args()

    if args.mode == "all":
        asyncio.run(run_all_years(train_from=args.train_from))
    elif args.mode == "train-production":
        asyncio.run(train_production_model())
    else:
        train_years = [y for y in range(args.train_from, args.test_year)]
        asyncio.run(run_single(test_year=args.test_year, train_years=train_years))
