"""
MLB XGBoost OU Backtester — predicts total runs (home_score + away_score).

Dedicated O/U model with features tuned for run total prediction:
- Rolling run scoring (both teams, recent windows)
- Park factor (venue historical run environment)
- Team total momentum (recent game totals, over frequency)
- Market anchors (closing total line)
- Weather/situational (temperature, dome, month, travel)

Usage:
    docker exec earl-knows-football-api-1 python -m app.handicapping.mlb_backtest_ou --test-year 2023
    docker exec earl-knows-football-api-1 python -m app.handicapping.mlb_backtest_ou --mode all
"""
import asyncio
import os
import pickle
import shutil
import uuid
from typing import Optional
import logging
import warnings
import json
import math
from datetime import datetime, date
from pathlib import Path

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
import numpy as np
import asyncpg
import pandas as pd
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from sklearn.metrics import mean_absolute_error
import xgboost as xgb

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("earl.mlb_xgb_ou")
log = logger.info

import os
DB = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DSN = DB.replace("+asyncpg", "")  # sync DSN for inference

# ── Team timezone map ──
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

# ── OU Feature Set ──
# Tier 1: Core scoring (rolling runs scored/allowed are the foundation)
# Tier 2: Park/weather (MLB-specific — venue and conditions matter for totals)
# Tier 3: Momentum (recent totals and over frequency capture streaks)
# Tier 4: Market anchor (closing line as baseline)
# Tier 5: Rest/travel/situational (fatigue, familiarity, context)

OU_FEATURES = [
    # Tier 1: Core scoring
    "h_rf10", "h_ra10", "a_rf10", "a_ra10",
    "h_rf_avg", "h_ra_avg", "a_rf_avg", "a_ra_avg",
    "h_over_freq", "a_over_freq",
    # Tier 2: Park/weather
    "park_factor", "is_dome", "temp", "wind_speed",
    # Tier 3: Momentum
    "h_total10", "a_total10", "h_over_freq5", "a_over_freq5",
    # Tier 4: Market anchor
    "ou_line",
    # Tier 5: Rest/travel/situational
    "rest_h", "rest_a", "rest_diff", "rest_diff_abs",
    "travel_miles", "tz_diff",
    # Situational
    "month", "is_summer", "is_div",
]

FEATURES_TRAINING = OU_FEATURES.copy()

TOTAL_OU_FEATURES = len(OU_FEATURES)


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
    """Load all completed MLB games with scores, venue, weather, and betting lines."""
    log("Loading games...")
    async with engine.connect() as conn:
        r = await conn.execute(text("""
            SELECT g.id, s.year, g.date::date as game_date,
                   ht.abbreviation as ha, at.abbreviation as aa,
                   g.home_score, g.away_score,
                   (g.home_score + g.away_score) as total,
                   (g.home_score - g.away_score) as margin,
                   g.roof_type, g.temperature, g.wind_speed, g.wind_direction,
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
        rows = r.fetchall(); games = pd.DataFrame([row._asdict() for row in rows])

        log("Loading betting lines from consolidated table...")
        r = await conn.execute(text("""
            SELECT
                game_id,
                closing_ou as over_under,
                opening_ou as opening_total,
                closing_over_odds,
                closing_spread_home_odds,
                closing_spread_away_odds,
                closing_home_implied_probability,
                closing_away_implied_probability,
                closing_home_ml as home_moneyline, closing_away_ml as away_moneyline,
                closing_spread as spread,
                closing_ou_sportsbook as sportsbook
            FROM mlb.betting_lines_consolidated
            WHERE has_verified_ou = true
        """))
        lines = pd.DataFrame([r._asdict() for r in r.fetchall()])

    log(f"  Games: {len(games)} ({games.year.min()}-{games.year.max()})")
    log(f"  Lines: {len(lines)}")
    log(f"  Lines with OU: {lines.over_under.notna().sum()}")

    games = games.rename(columns={"id": "game_id"})
    df = games.merge(lines, on="game_id", how="left")
    log(f"  Merged: {len(df)} rows, {df.over_under.notna().sum()} with O/U line")

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
        pitcher_df = pd.DataFrame([r._asdict() for r in r.fetchall()])
    log(f"  Pitcher lines: {len(pitcher_df)}")
    return df, pitcher_df


def build_features(df: pd.DataFrame, pitcher_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Build OU-optimized features.

    Strategy: flatten to team-game view, compute rolling stats, rebuild to game level,
    then add OU-specific features (park factor, momentum, implied total).
    """
    log("Building team-game table...")

    # ── 1. Flatten to team-game view ──
    rows = []
    for _, g in df.iterrows():
        rows.append({
            "game_id": g["game_id"], "year": g["year"], "game_date": g["game_date"],
            "team": g["ha"], "opp": g["aa"],
            "pf": g["home_score"], "pa": g["away_score"],
            "total": g["total"],
            "is_home": 1, "margin": g["margin"],
            "roof": g["roof_type"], "temp": g["temperature"], "wind": g["wind_speed"],
            "wind_dir": g["wind_direction"],
            "division": g["hdiv"], "venue": g["venue"],
        })
        rows.append({
            "game_id": g["game_id"], "year": g["year"], "game_date": g["game_date"],
            "team": g["aa"], "opp": g["ha"],
            "pf": g["away_score"], "pa": g["home_score"],
            "total": g["total"],
            "is_home": 0, "margin": -g["margin"],
            "roof": g["roof_type"], "temp": g["temperature"], "wind": g["wind_speed"],
            "wind_dir": g["wind_direction"],
            "division": g["adiv"], "venue": g["venue"],
        })
    tg = pd.DataFrame(rows).sort_values(["team", "game_date", "game_id"]).reset_index(drop=True)
    tg["game_date_dt"] = pd.to_datetime(tg["game_date"])

    # ── 2. Rolling stats per team ──
    log("  Rolling scoring stats (5, 10 game windows)...")
    for window in [5, 10, 20]:
        tg[f"rf{window}"] = (
            tg.groupby("team")["pf"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )
        tg[f"ra{window}"] = (
            tg.groupby("team")["pa"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )
        tg[f"total_avg_r{window}"] = (
            tg.groupby("team")["total"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )

    # ── Prior: previous season averages for early-season blending ──
    log("  Computing prior-season averages for early-season smoothing...")
    team_season_avg = tg.groupby(["team", "year"]).agg(
        prior_pf=("pf", "mean"),
        prior_pa=("pa", "mean"),
    ).reset_index()
    # Build (team, prev_year) → (pf, pa) lookup
    prior_lookup = {}
    for _, row in team_season_avg.iterrows():
        prior_lookup[(row["team"], row["year"] - 1)] = (row["prior_pf"], row["prior_pa"])
    
    # Games played counter per team per season
    tg["season_game_no"] = tg.groupby(["team", "year"]).cumcount() + 1
    
    # Blend: for first ~10 games, blend current rolling average with prior
    for window in [5, 10, 20]:
        early_mask = tg["season_game_no"] <= window
        for team in tg.loc[early_mask, "team"].unique():
            team_year = tg.loc[tg["team"] == team, "year"].iloc[0]
            prior = prior_lookup.get((team, team_year - 1))
            if prior is None:
                continue  # no prior data (e.g., first season)
            prior_pf, prior_pa = prior
            team_early = early_mask & (tg["team"] == team)
            cnt = tg.loc[team_early, "season_game_no"]
            # Linear blend: more weight on current data as season progresses
            blend_w = (cnt - 1) / window  # 0 to ~0.9
            tg.loc[team_early, f"rf{window}"] = (
                blend_w * tg.loc[team_early, f"rf{window}"] + (1 - blend_w) * prior_pf
            )
            tg.loc[team_early, f"ra{window}"] = (
                blend_w * tg.loc[team_early, f"ra{window}"] + (1 - blend_w) * prior_pa
            )

    # ── 3. Rest days ──
    log("  Rest days...")
    tg["prev_date"] = tg.groupby("team")["game_date_dt"].shift(1)
    tg["rest"] = (tg["game_date_dt"] - tg["prev_date"]).dt.days.fillna(1).clip(0, 30)

    # Effective ERA proxy from runs allowed (runs per game)
    log("  Rolling ERA proxy...")
    for window in [10]:
        tg[f"era_r{window}"] = (
            tg.groupby("team")["pa"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )

    # ── Win percentage (cumulative season, no look-ahead) ──
    log("  Win percentage...")
    tg["team_win"] = (tg["pf"] > tg["pa"]).astype(int)  # team won this game?
    tg["winpct"] = tg.groupby(["team", "year"])["team_win"].transform(
        lambda x: x.shift(1).expanding().mean()
    ).fillna(0.5)

    # ── Home/road splits (vectorized) ──
    log("  Home/road splits...")
    # Expanding mean of runs scored per (team, is_home) group
    split_rf = tg.groupby(["team", "is_home"])["pf"].transform(
        lambda x: x.shift(1).expanding().mean()
    )
    split_ra = tg.groupby(["team", "is_home"])["pa"].transform(
        lambda x: x.shift(1).expanding().mean()
    )
    # Home team's home scoring (rows where this team is home)
    tg["h_home_rf"] = split_rf.where(tg["is_home"] == 1, 4.5)
    # Away team's away scoring (rows where this team is away)
    tg["a_away_rf"] = split_rf.where(tg["is_home"] == 0, 4.5)
    tg["h_home_ra"] = split_ra.where(tg["is_home"] == 1, 4.5)
    tg["a_away_ra"] = split_ra.where(tg["is_home"] == 0, 4.5)

    # ── 4. Rejoin to game level ──
    log("  Rejoining to game level...")
    h = tg[tg["is_home"] == 1][
        ["game_id", "team", "rf5", "rf10", "rf20", "ra5", "ra10", "ra20",
         "total_avg_r5", "total_avg_r10", "rest", "era_r10",
         "winpct", "h_home_rf", "h_home_ra"]
    ].rename(columns={
        "team": "ha",
        "rf5": "h_rf5", "rf10": "h_rf10", "rf20": "h_rf20",
        "ra5": "h_ra5", "ra10": "h_ra10", "ra20": "h_ra20",
        "rest": "rest_h",
        "era_r10": "h_era_r10",
        "winpct": "h_winpct",
        "h_home_rf": "h_home_rf",
        "h_home_ra": "h_home_ra",
        "total_avg_r5": "h_total_avg_r5",
        "total_avg_r10": "h_total_avg_r10",
    })

    a = tg[tg["is_home"] == 0][
        ["game_id", "team", "rf5", "rf10", "rf20", "ra5", "ra10", "ra20",
         "total_avg_r5", "total_avg_r10", "rest", "era_r10",
         "winpct", "a_away_rf", "a_away_ra"]
    ].rename(columns={
        "team": "aa",
        "rf5": "a_rf5", "rf10": "a_rf10", "rf20": "a_rf20",
        "ra5": "a_ra5", "ra10": "a_ra10", "ra20": "a_ra20",
        "rest": "rest_a",
        "era_r10": "a_era_r10",
        "winpct": "a_winpct",
        "a_away_rf": "a_away_rf",
        "a_away_ra": "a_away_ra",
        "total_avg_r5": "a_total_avg_r5",
        "total_avg_r10": "a_total_avg_r10",
    })

    feats = h.merge(a, on="game_id")
    feats["rest_diff"] = feats["rest_h"] - feats["rest_a"]
    feats["combo_era_r10"] = feats["h_era_r10"] + feats["a_era_r10"]
    feats["combo_era_r10_diff"] = (feats["h_era_r10"] - feats["a_era_r10"]).abs()
    # Division game flag
    div_map = df.set_index("game_id")[["hdiv", "adiv"]].to_dict("index")
    feats["is_div"] = feats["game_id"].apply(
        lambda gid: 1 if gid in div_map and div_map[gid]["hdiv"] == div_map[gid]["adiv"] else 0
    )
    # Timezone difference (home - away)
    feats["tz_diff"] = feats.apply(
        lambda r: TZ.get(r["ha"], -5) - TZ.get(r["aa"], -5), axis=1
    )

    # ── 5. Add original game data ──
    orig_cols = df[["game_id", "year", "game_date", "total", "margin",
                     "over_under", "home_score", "away_score",
                     "opening_total", "roof_type", "temperature", "wind_speed",
                     "wind_direction", "day_night", "venue"]].copy()
    feats = feats.merge(orig_cols, on="game_id")
    feats["actual_total"] = feats["total"]
    # Dome flag from roof_type
    feats["is_dome"] = feats["roof_type"].fillna("").str.lower().isin(
        ["dome", "retractable roof - closed", "retractable roof - dome"]
    ).astype(int)

    # ── 6. Park factor (vectorized — no look-ahead) ──
    # Compute cumulative average total per venue from games played before each game
    log("  Computing park factors...")
    feats = feats.sort_values(["year", "game_date", "game_id"]).reset_index(drop=True)
    # Cumulative mean per venue, shifted by 1 to avoid look-ahead
    feats["park_factor"] = (
        feats.groupby("venue")["actual_total"]
        .transform(lambda x: x.shift(1).expanding().mean())
        .fillna(1.0)
    )
    # Normalize by overall league average (park factor should be 1.0 = neutral)
    league_avg = feats["actual_total"].expanding().mean().shift(1).fillna(8.5)
    feats["park_factor"] = feats["park_factor"] / league_avg



    # ── 7. Over frequency ──
    log("  Computing over frequency...")
    feats["game_date_dt"] = pd.to_datetime(feats["game_date"])
    feats = feats.sort_values(["year", "game_date", "game_id"]).reset_index(drop=True)

    # Compute team-level over pct: for each team-game, what % of their last 10 games went over
    tg["has_ou"] = False  # will fill from game-level
    ou_line_map = df.set_index("game_id")["over_under"].fillna(8.5).to_dict()
    tg["ou_line_game"] = tg["game_id"].map(ou_line_map)

    # Mark over (1) under (0) for each team-game
    tg["game_over"] = (tg["total"] > tg["ou_line_game"]).astype(int)
    tg["game_push"] = (tg["total"] == tg["ou_line_game"]).astype(int)
    # For over pct, pushes count as neither
    tg["over_or_push"] = tg["game_over"]  # 1 = over, 0 = under/push for pct purposes

    for window in [5, 10]:
        tg[f"over_pct_r{window}"] = (
            tg.groupby("team")["game_over"]
            .transform(lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        )

    # Map back to game level
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

    # ── 8. Team total averages (combined across both teams) ──
    feats["total_avg_team_r10"] = (
        feats["h_total_avg_r10"] + feats["a_total_avg_r10"]
    ) / 2

    # ── 9. Implied totals (3 rolling windows) ──
    for window, col in [(5, "implied_total_5"), (10, "implied_total_10"), (20, "implied_total_20")]:
        feats[col] = (
            feats[f"h_rf{window}"].fillna(4.5) + feats[f"a_rf{window}"].fillna(4.5) +
            feats[f"h_ra{window}"].fillna(4.5) + feats[f"a_ra{window}"].fillna(4.5)
        ) / 2
    feats["implied_total"] = feats["implied_total_10"]  # alias for backward compat
    feats["closing_ou"] = feats["over_under"].fillna(8.5)
    feats["opening_ou"] = feats["opening_total"].fillna(feats["closing_ou"])
    feats["ou_movement"] = feats["closing_ou"] - feats["opening_ou"]

    # ── 10. Situational ──
    feats["month"] = feats["game_date_dt"].dt.month
    feats["is_summer"] = feats["month"].isin([6, 7, 8]).astype(int)

    # ── Travel distance ──
    feats["travel_miles"] = feats.apply(
        lambda r: haversine(
            *COORDS.get(r["aa"], (0, 0)), *COORDS.get(r["ha"], (0, 0))
        ),
        axis=1,
    )
    feats.loc[feats["travel_miles"] < 50, "travel_miles"] = 0

    # ── 11. Pitcher quality features ──
    if pitcher_df is not None and len(pitcher_df) > 0:
        log("  Computing pitcher quality features...")
        feats = add_pitcher_features(feats, pitcher_df)
    # ── 12. Team hitting quality (rolling OPS) ──
    log("  Computing team hitting quality features...")
    feats = add_hitting_features(feats)
    # ── 13. Filter to games with complete line data ──
    pre_filter = len(feats)
    feats = feats[feats["over_under"].notna()].copy()
    log(f"  Filtered to {len(feats)} games with O/U line (dropped {pre_filter - len(feats)} without)")

    return feats


def compute_game_park_factor(feats: pd.DataFrame, game_id: int, venue: str) -> float:
    """Compute park factor using only games before this one (no look-ahead)."""
    prior = feats[feats["game_id"] < game_id]
    venue_games = prior[prior["venue"] == venue]
    if len(venue_games) < 10:
        return 1.0
    venue_avg = venue_games["actual_total"].mean()
    league_avg = prior["actual_total"].mean()
    return venue_avg / league_avg if league_avg else 1.0


def add_pitcher_features(feats: pd.DataFrame, pitcher_df: pd.DataFrame) -> pd.DataFrame:
    """
    Add pitcher talent baseline features from pitcher_game_stats.
    Computes rolling ERA, WHIP, K/BB, and FIP-like metrics over last 20 starts.
    Uses shift(1) to prevent look-ahead into the current game.
    """
    ps = pitcher_df.copy()
    ps["game_date_dt"] = pd.to_datetime(ps["game_date"])
    ps["era"] = np.where(ps["ip"] > 0, ps["er"] / ps["ip"] * 9, np.nan)
    
    # Resolve column names — table has BOTH old (k, bb, hr) and new (strikeouts, etc.)
    # columns. Whichever has the data, we unify to canonical names as float.
    for old_n, new_n in [("k", "strikeouts"), ("bb", "base_on_balls"),
                          ("hr", "home_runs")]:
        if old_n not in ps.columns:
            ps[old_n] = 0
        # Use whichever column has the data — both populated for reprocessed rows
        ps[new_n] = np.where(
            ps[new_n].fillna(0).astype(float) > 0,
            ps[new_n].fillna(0).astype(float),
            ps[old_n].fillna(0).astype(float)
        )
    # hits column is 'h' in the DB (old) — no 'hits' column exists
    ps["hits"] = ps["h"].fillna(0).astype(float)
    ps["er"] = ps["er"].fillna(0).astype(float)
    ps["ip"] = ps["ip"].fillna(0).astype(float)
    ps["bb_col"] = ps["base_on_balls"]
    ps["hr_col"] = ps["home_runs"]
    ps["so_col"] = ps["strikeouts"]
    ps["hits_col"] = ps["hits"]
    ps["er_col"] = ps["er"]
    
    # Additional per-game pitcher rates
    ps["whip"] = np.where(
        ps["ip"] > 0,
        (ps["hits_col"] + ps["bb_col"]).astype(float) / ps["ip"],
        np.nan
    )
    ps["k_per_9"] = np.where(ps["ip"] > 0, ps["so_col"] / ps["ip"] * 9, np.nan)
    ps["bb_per_9"] = np.where(ps["ip"] > 0, ps["bb_col"] / ps["ip"] * 9, np.nan)
    ps["hr_per_9"] = np.where(ps["ip"] > 0, ps["hr_col"] / ps["ip"] * 9, np.nan)
    ps["k_per_bb"] = np.where(
        ps["so_col"] + ps["bb_col"] > 0,
        ps["so_col"] / (ps["so_col"] + ps["bb_col"]),
        np.nan
    )
    ps["gb_rate"] = np.where(
        ps.get("ground_outs", 0).fillna(0) + ps.get("air_outs", 0).fillna(0)
        + ps.get("fly_outs", 0).fillna(0) + ps.get("line_outs", 0).fillna(0) > 0,
        ps.get("ground_outs", 0).fillna(0).astype(float) /
        (ps.get("ground_outs", 0).fillna(0) + ps.get("air_outs", 0).fillna(0)
         + ps.get("fly_outs", 0).fillna(0) + ps.get("line_outs", 0).fillna(0)),
        np.nan
    )
    
    # Defaults
    feats["h_pitcher_era_l20"] = 4.0
    feats["a_pitcher_era_l20"] = 4.0
    feats["h_pitcher_whip_l20"] = 1.3
    feats["a_pitcher_whip_l20"] = 1.3
    feats["h_pitcher_k9_l20"] = 8.0
    feats["a_pitcher_k9_l20"] = 8.0
    feats["h_pitcher_kbb_rate_l20"] = 0.6
    feats["a_pitcher_kbb_rate_l20"] = 0.6
    feats["h_pitcher_home_team_l20"] = 1.0
    feats["a_pitcher_home_team_l20"] = 1.0
    
    # ── Starters rolling averages L20 ──
    starters = ps[ps["is_starter"] & (ps["ip"] > 0)].copy()
    if len(starters) > 0:
        starters = starters.sort_values(["pitcher_mlb_id", "game_date_dt", "game_id"])
        
        metric_defs = {
            "era": ("era", 4.0),
            "whip": ("whip", 1.3),
            "k9": ("k_per_9", 8.0),
            "kbb_rate": ("k_per_bb", 0.6),
            "hr_per_9": ("hr_per_9", 1.2),
        }
        
        for suffix_raw, (col, default_val) in metric_defs.items():
            starters[f"{suffix_raw}_l20"] = (
                starters.groupby("pitcher_mlb_id")[col]
                .transform(lambda x: x.shift(1).rolling(20, min_periods=1).mean())
            )
            mapping = starters[starters[f"{suffix_raw}_l20"].notna()][
                ["game_id", "team_abbr", f"{suffix_raw}_l20"]
            ].drop_duplicates(subset=["game_id", "team_abbr"]).set_index(["game_id", "team_abbr"])[f"{suffix_raw}_l20"]
            
            for team_col, prefix in [("ha", "h"), ("aa", "a")]:
                feats_indexed = feats.set_index(["game_id", team_col])
                feats[f"{prefix}_pitcher_{suffix_raw}_l20"] = (
                    mapping.reindex(feats_indexed.index).fillna(default_val).values
                )
    
    return feats


def add_hitting_features(feats: pd.DataFrame) -> pd.DataFrame:
    """
    Add rolling team batting features (OPS, K%, BB%, GB rate, HR rate)
    from batting_game_stats. Uses shift(1) for look-ahead prevention.
    """
    try:
        from sqlalchemy import create_engine as _ce, text as _t
        _e = _ce(DB.replace("+asyncpg", ""))
        
        with _e.connect() as _c:
            team_games = _c.execute(_t("""
                SELECT bgs.game_id,
                       CASE WHEN bgs.team_side = 'home' THEN g.home_team_id ELSE g.away_team_id END as team_id,
                       bgs.team_side,
                       SUM(bgs.plate_appearances) as pa,
                       SUM(bgs.at_bats) as ab,
                       SUM(bgs.hits) as h,
                       SUM(bgs.total_bases) as tb,
                       SUM(bgs.base_on_balls) as bb,
                       SUM(bgs.hit_by_pitch) as hbp,
                       SUM(bgs.home_runs) as hr,
                       SUM(bgs.strikeouts) as so,
                       SUM(bgs.ground_outs) as go,
                       SUM(bgs.air_outs) as ao,
                       SUM(bgs.fly_outs) as fo,
                       SUM(bgs.line_outs) as lo
                FROM mlb.batting_game_stats bgs
                JOIN mlb.games g ON g.id = bgs.game_id
                GROUP BY bgs.game_id, bgs.team_side, g.home_team_id, g.away_team_id
                ORDER BY bgs.game_id
            """)).fetchall()
        _e.dispose()
        
        tg_rows = []
        for r in team_games:
            gid, tid, side, pa, ab, h, tb, bb, hbp, hr, so, go, ao, fo, lo = r
            pa = int(pa or 0); ab = int(ab or 0); h = int(h or 0); tb = int(tb or 0)
            bb = int(bb or 0); hbp = int(hbp or 0); hr = int(hr or 0); so = int(so or 0)
            go = int(go or 0); ao = int(ao or 0); fo = int(fo or 0); lo = int(lo or 0)
            
            denom_obp = ab + bb + hbp
            obp = (h + bb + hbp) / denom_obp if denom_obp > 0 else 0
            slg = tb / ab if ab > 0 else 0
            ops = round(obp + slg, 3)
            k_rate = so / pa if pa > 0 else 0
            bb_rate = bb / pa if pa > 0 else 0
            total_out = go + ao + fo + lo
            gb_rate = go / total_out if total_out > 0 else 0.4
            hr_rate = hr / pa if pa > 0 else 0
            
            tg_rows.append({
                "game_id": gid, "team_id": tid, "side": side,
                "ops": ops, "k_rate": k_rate, "bb_rate": bb_rate,
                "gb_rate": gb_rate, "hr_rate": hr_rate
            })
        
        tdf = pd.DataFrame(tg_rows)
        
        # ── Rolling stats per team ──
        tdf = tdf.sort_values(["team_id", "game_id"])
        rolling_defs = [
            ("ops", 10, "ops_l10"),
            ("ops", 20, "ops_l20"),
        ]
        for col_raw, window, col_name in rolling_defs:
            tdf[col_name] = tdf.groupby("team_id")[col_raw].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).mean()
            )
        
        # Merge into feats by game_id + side
        for side, prefix in [("home", "h"), ("away", "a")]:
            sub = tdf[tdf["side"] == side][["game_id"] + [d[2] for d in rolling_defs]].copy()
            sub = sub.rename(columns={d[2]: f"{prefix}_{d[2]}" for d in rolling_defs})
            feats = feats.merge(sub, on="game_id", how="left")
        
        # Fill missing (pre-2021 games or games without boxscore data)
        fill_defaults = {
            "h_ops_l10": 0.700, "a_ops_l10": 0.700,
            "h_ops_l20": 0.700, "a_ops_l20": 0.700,
            "closing_over_odds": -110,
            "closing_spread_home_odds": -110,
            "closing_spread_away_odds": -110,
            "closing_home_implied_probability": 0.500,
            "closing_away_implied_probability": 0.500,

        }
        for col, default in fill_defaults.items():
            if col not in feats.columns:
                feats[col] = default
            else:
                feats[col] = feats[col].fillna(default)
        
        log(f"  Hitting features added: {len(tdf)} team-game rows")
    except Exception as e:
        log(f"  ⚠ Could not add hitting features: {e}")
        for col in ["h_ops_l10", "a_ops_l10", "h_ops_l20", "a_ops_l20",
                     "closing_over_odds",
                     "closing_spread_home_odds", "closing_spread_away_odds",
                     "closing_home_implied_probability", "closing_away_implied_probability"]:
            if col not in feats.columns:
                feats[col] = 0.0
    
    return feats


def evaluate_ou(
    actual_total: np.ndarray,
    pred_total: np.ndarray,
    closing_ou: np.ndarray,
    spread: np.ndarray = None,
    actual_margin: np.ndarray = None,
) -> dict:
    """
    Evaluate OU predictions vs closing lines.

    Returns: {
        "ou": {correct, incorrect, pushes, pct},
        "ats": {correct, incorrect, total, pct} (if spread available),
        "ml": {correct, incorrect, total, pct} (if margin available),
        "picks": [details per game],
    }
    """
    n = len(actual_total)
    result = {}

    # O/U evaluation: predicted over -> actual over?
    ou_diff = pred_total - closing_ou
    actual_exceeds = actual_total > closing_ou
    actual_push = actual_total == closing_ou
    pred_over = ou_diff > 0

    ou_correct = int(((pred_over == actual_exceeds) & ~actual_push).sum())
    ou_incorrect = int(((pred_over != actual_exceeds) & ~actual_push).sum())
    ou_pushes = int(actual_push.sum())
    ou_total = ou_correct + ou_incorrect

    result["ou"] = {
        "correct": ou_correct,
        "incorrect": ou_incorrect,
        "pushes": ou_pushes,
        "total": ou_total,
        "pct": round(100 * ou_correct / max(ou_total, 1), 1),
    }

    # ATS evaluation (if spread available)
    if spread is not None and len(spread) == n:
        # Predicted margin from the OU model doesn't exist, but we can derive
        # a rough ATS pick from the scoring distribution
        # For now, skip ATS for OU model
        pass

    # ML evaluation (if margin available)
    if actual_margin is not None and len(actual_margin) == n:
        # Our OU model doesn't predict winners, but we can use the scoring
        # distribution to infer home advantage. Skip for now.
        pass

    return result


async def run_backtest(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    training_id: str,
    test_year: int = 2025,
    train_years: list[int] = None,
    xgb_params: dict = None,
) -> dict:
    """Run XGBoost OU backtest for a single year."""
    if train_years is None:
        train_years = [y for y in range(2021, test_year)]

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

    # Split
    tr_mask = feats["year"].isin(train_years)
    te_mask = feats["year"] == test_year
    tr = feats[tr_mask].reset_index(drop=True)
    te = feats[te_mask].sort_values(["month", "game_date"]).reset_index(drop=True)

    log(f"  Train: {len(tr)} games ({train_years})")
    log(f"  Test:  {len(te)} games ({test_year})")

    if len(tr) < 50 or len(te) < 10:
        log("  ⚠ Not enough data, skipping")
        return {"error": "insufficient_data"}

    # ── Direct total prediction: predict actual_total, with implied_total as a feature ──
    target_te = te["actual_total"].values
    X_te = te[FEATURES_TRAINING].fillna(0).astype(np.float32)

    X_tr = tr[FEATURES_TRAINING].fillna(0).astype(np.float32)
    target_tr = tr["actual_total"].values

    # Clip outliers at 1st and 99th percentile
    q01, q99 = np.percentile(target_tr, [1, 99])
    clip_mask = (target_tr >= q01) & (target_tr <= q99)
    clipped = (~clip_mask).sum()
    tr = tr[clip_mask].reset_index(drop=True)
    X_tr = tr[FEATURES_TRAINING].fillna(0).astype(np.float32)
    target_tr = tr["actual_total"].values
    if clipped:
        log(f"  Clipped {clipped} outlier games ({q01:.1f}-{q99:.1f} run range)")

    # Time-weighted training: more recent years get higher weight
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
    # Save model by test year for engine backtest to use
    model_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
    model_dir.mkdir(parents=True, exist_ok=True)
    model_name = f"{training_id}-{test_year}.pkl"
    save_path = model_dir / model_name
    with open(save_path, "wb") as f:
        pickle.dump(model, f)
    log(f"  Saved OU model to {save_path}")

    # Predict total directly (implied_total is just another feature)
    te = te.copy()
    te["pred_total"] = model.predict(X_te)
    te["pred_error"] = te["actual_total"] - te["pred_total"]
    mae = mean_absolute_error(te["actual_total"].values, te["pred_total"].values)
    err_mean = te["pred_error"].mean()
    err_std = te["pred_error"].std()

    # ── O/U evaluation ──
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

        # ── Baseline comparison: implied_total alone on the same test data ──
        baseline_over = ou_df["implied_total"].values > closing_ou
        base_correct = int(((baseline_over == actual_over) & ~actual_push).sum())
        base_incorrect = int(((baseline_over != actual_over) & ~actual_push).sum())
        base_pct = round(100 * base_correct / max(base_correct + base_incorrect, 1), 1)
        xgb_pct = round(100 * ou_correct / max(ou_correct + ou_incorrect, 1), 1)
        if base_pct > xgb_pct:
            log(f"  ⚠ BASELINE BEATS XGBOOST: implied_total {base_pct}% vs XGBoost {xgb_pct}%")
        else:
            log(f"  Baseline implied_total: {base_pct}%  |  XGBoost: {xgb_pct}%")

    ml_correct = 0
    ml_incorrect = 0
    if has_ou.any():
        # Simple heuristic: if home runs scored > away runs scored in prediction
        # Not ideal — OU model doesn't predict margin — so skip ML
        pass

    ats_games = 0
    ats_correct = 0
    ats_incorrect = 0

    # ── Monthly breakdown ──
    monthly = []
    for m in sorted(te["month"].unique()):
        sub = te[te["month"] == m]
        if len(sub) < 5:
            continue
        mae_m = mean_absolute_error(sub["actual_total"], sub["pred_total"])
        sub_has_ou = sub["over_under"].notna()
        if sub_has_ou.any():
            sub_ou = sub[sub_has_ou]
            sub_ou_correct = int(((sub_ou["pred_total"] > sub_ou["over_under"]) == (sub_ou["actual_total"] > sub_ou["over_under"])).sum())
            sub_ou_pct = round(100 * sub_ou_correct / len(sub_ou), 1)
        else:
            sub_ou_pct = 0

        monthly.append({
            "month": int(m),
            "games": int(len(sub)),
            "mae": float(round(mae_m, 2)),
            "ou_pct": sub_ou_pct,
        })

    # ── Feature importance ──
    imp = pd.DataFrame({
        "feature": list(FEATURES_TRAINING),
        "importance": list(model.feature_importances_),
    }).sort_values("importance", ascending=False)

    # ── Results ──
    ou_total = ou_correct + ou_incorrect

    result = {
        "test_year": test_year,
        "train_years": train_years,
        "feature_set": "mlb_ou",
        "total_games": int(len(te)),
        "mae": round(mae, 2),
        "err_mean": round(err_mean, 2),
        "err_std": round(err_std, 2),
        "within_1": float(round((abs(te["pred_error"]) < 1).mean(), 3)),
        "within_2": float(round((abs(te["pred_error"]) < 2).mean(), 3)),
        "ats": {
            "correct": 0,
            "incorrect": 0,
            "total": 0,
            "pct": 0,
        },
        "ou": {
            "correct": ou_correct,
            "incorrect": ou_incorrect,
            "pushes": ou_pushes,
            "total": ou_total,
            "pct": float(round(100 * ou_correct / max(ou_total, 1), 1)),
        },
        "ml": {
            "correct": 0,
            "incorrect": 0,
            "total": 0,
            "pct": 0,
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
    """Pretty-print OU backtest results."""
    print(f"\n{'='*62}")
    print(f"MLB O/U BACKTEST — {result['test_year']} Season")
    print(f"Features: {result['feature_set']} ({len(result['feature_importance'])} feats)")
    print(f"Train: {result['train_years']}")
    print(f"{'='*62}")

    print(f"\n📊 TOTAL RUNS PREDICTION")
    print(f"  MAE:       {result['mae']:.2f} runs")
    print(f"  Bias:      {result['err_mean']:+.2f} runs")
    print(f"  Std Dev:   {result['err_std']:.2f} runs")
    print(f"  ±1 run:    {result['within_1']:.1%}")
    print(f"  ±2 runs:   {result['within_2']:.1%}")

    print(f"\n🎲 OVER/UNDER PERFORMANCE")
    ou = result["ou"]
    print(f"  O/U:       {ou['correct']:4d}-{ou['incorrect']:4d}-{ou['pushes']}  ({ou['pct']:.1f}%)  [{ou['total']} games]")

    if result["monthly"]:
        print(f"\n📅 MONTHLY BREAKDOWN")
        print(f"  {'Month':>6s}  {'Games':>5s}  {'MAE':>5s}  {'OU%':>5s}")
        for m in result["monthly"]:
            print(f"  {m['month']:>6d}  {m['games']:>5d}  {m['mae']:>5.2f}  {m['ou_pct']:>5.1f}%")

    print(f"\n🔑 TOP FEATURES")
    for i, fi in enumerate(result["feature_importance"][:12]):
        bar = "█" * int(fi["importance"] * 100)
        print(f"  {i+1:2d}. {fi['feature']:>20s}: {fi['importance']:.4f} {bar}")

    print()


async def run_all_years(
    test_years: list[int] = None,
    train_from: int = 2021,
):
    """Run OU backtests across multiple years."""
    if test_years is None:
        test_years = [2025, 2026]

    t0 = datetime.now()
    engine = create_async_engine(DB)

    df, pitcher_df = await load_data(engine)
    log(f"\nBuilding features...")
    feats = build_features(df, pitcher_df)
    log(f"Feature table: {len(feats)} rows, {len(feats.columns)} columns")

    all_results = []
    batch_training_id = str(uuid.uuid4())
    log(f"Batch training_id: {batch_training_id}")

    for year in test_years:
        log(f"\n{'─'*62}")
        log(f"Testing year={year}")
        log(f"{'─'*62}")
        train = [y for y in range(train_from, year)]
        result = await run_backtest(df, feats, test_year=year, train_years=train, training_id=batch_training_id)
        if "error" not in result:
            all_results.append(result)

    await engine.dispose()

    # Summary
    print(f"\n{'='*62}")
    print("MLB O/U BACKTEST — ALL YEARS")
    print(f"{'='*62}")
    print(f"\n{'Year':>4s}  {'Games':>5s}  {'MAE':>5s}  {'OU%':>6s}  {'OU W/L':>12s}")
    print("─" * 42)
    total_c = 0
    total_i = 0
    total_g = 0
    for r in sorted(all_results, key=lambda x: x["test_year"]):
        ou = r["ou"]
        total_c += ou["correct"]
        total_i += ou["incorrect"]
        total_g += ou["total"]
        wl = f"{ou['correct']}-{ou['incorrect']}"
        if ou.get("pushes"):
            wl += f"-{ou['pushes']}"
        print(f"  {r['test_year']:>4d}  {r['total_games']:>5d}  {r['mae']:>5.2f}  {ou['pct']:>5.1f}%  {wl:>12s}")

    tp = total_c + total_i
    print(f"  {'─'*38}")
    print(f"  {'TOTAL':>4s}  {total_g:>5d}  {'':>5s}  {round(100*total_c/max(tp,1),1):>5.1f}%  {total_c:>3d}-{total_i:>3d}")

    # Save results to DB
    print(f"\n=== DB_HELPERS_AVAILABLE={_DB_HELPERS_AVAILABLE}, all_results={len(all_results)} years ===", flush=True)
    if _DB_HELPERS_AVAILABLE:
        combined_results = {
            "run_time": str(datetime.now() - t0),
            "results": all_results,
        }

        print(f"Calling save_training_run... batch={batch_training_id}", flush=True)
        try:
            training_id = save_training_run(
                sport="mlb",
                model_type="ou",
                results_json=combined_results,
                pkl_filename=f"{batch_training_id}.pkl",
                algorithm="xgboost",
                description=f"MLB OU year-by-year backtest",
                test_year=max(test_years),
                train_years=test_years,
            )
            print(f"save_training_run returned: {training_id}", flush=True)
        except Exception as e:
            print(f"ERROR in save_training_run: {e}", flush=True)
            import traceback
            traceback.print_exc()
            raise

        # Copy the latest year's model as the combined prod model
        pkl_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
        latest_year = max(all_results, key=lambda r: r["test_year"])["test_year"]
        src_pkl = pkl_dir / f"{batch_training_id}-{latest_year}.pkl"
        prod_pkl = pkl_dir / f"{batch_training_id}.pkl"
        if src_pkl.exists():
            shutil.copy2(str(src_pkl), str(prod_pkl))
            print(f"Copied prod model: {src_pkl.name} -> {prod_pkl.name}", flush=True)
        else:
            print(f"WARNING: src_pkl {src_pkl} not found", flush=True)

        from app.handicapping.db_training import update_pkl_filename
        update_pkl_filename("mlb", training_id, f"{batch_training_id}.pkl")
        log(f"\nResults saved to DB (training_id={training_id})")
        log(f"  Per-year PKLs: {batch_training_id}-{{year}}.pkl")
        log(f"  Combined prod: {batch_training_id}.pkl")
        print(f"Done saving to DB.", flush=True)
    else:
        out = {
            "run_time": str(datetime.now() - t0),
            "results": all_results,
        }
        out_path = "/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb_ou_backtest_results.json"
        with open(out_path, "w") as f:
            json.dump(out, f, indent=2, default=str)
        log(f"\nResults saved to {out_path}")
    log(f"Total time: {datetime.now() - t0}")


async def run_single(test_year: int = 2025, train_years: list[int] = None):
    'Run a single OU backtest for quick iteration.'
    import uuid
    training_id = str(uuid.uuid4())
    print(f"training_id: {training_id}")

    engine = create_async_engine(DB)
    df, pitcher_df = await load_data(engine)
    feats = build_features(df, pitcher_df)
    result, _ = await run_backtest(df, feats, test_year=test_year, train_years=train_years, training_id=training_id)
    await engine.dispose()

    # Save to DB
    if _DB_HELPERS_AVAILABLE:
        pkl_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
        pkl_dir.mkdir(parents=True, exist_ok=True)

        db_training_id = save_training_run(
            sport="mlb",
            model_type="ou",
            results_json=result,
            pkl_filename=f"{training_id}.pkl",
            algorithm="xgboost",
            description=f"MLB OU single backtest: {test_year}",
            test_year=test_year,
            train_years=train_years,
        )

        # Copy the year model as prod
        src_pkl = pkl_dir / f"{training_id}-{test_year}.pkl"
        prod_pkl = pkl_dir / f"{training_id}.pkl"
        if src_pkl.exists():
            shutil.copy2(str(src_pkl), str(prod_pkl))

        update_pkl_filename("mlb", db_training_id, f"{training_id}.pkl")
        print(f"\nResults saved to DB (training_id={training_id})")
        print(f"  Per-year PKL: {training_id}-{test_year}.pkl")
        print(f"  Prod PKL:     {training_id}.pkl")
    else:
        import json
        out_path = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb_ou_backtest_results.json")
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
    if v is None or v == 0: return 0.5
    return abs(v) / (abs(v) + 100) if v < 0 else 100.0 / (v + 100)


# ── Model management & inference (imported by mlb_engine.py) ──────────

MODELS_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
OU_MODEL_PATH = MODELS_DIR / "mlb_ou_model_prod.pkl"
_ou_model = None


def _resolve_ou_model_path() -> Path:
    """Return the path to the current prod OU model.

    First tries the DB (current training run's PKL), falls back to
    the legacy mlb_ou_model_prod.pkl path for backward compat.
    """
    try:
        db_path = get_model_pkl_path("mlb", "ou")
        if db_path and os.path.exists(db_path):
            return Path(db_path)
    except Exception:
        pass
    return OU_MODEL_PATH


def set_model_path(path: str):
    global OU_MODEL_PATH, _ou_model
    OU_MODEL_PATH = Path(path)
    _ou_model = None


def _load_ou_model():
    global _ou_model
    if _ou_model is not None:
        return _ou_model
    model_path = _resolve_ou_model_path()
    if not model_path.exists():
        raise FileNotFoundError(f"OU model not found at {model_path}")
    with open(model_path, "rb") as f:
        payload = pickle.load(f)
    model = payload["model"] if isinstance(payload, dict) else payload
    _ou_model = model
    log(f"Loaded OU model from {model_path.name} ({len(FEATURES_TRAINING)} features)")
    return _ou_model


# ── Cached data for predict_ou to avoid reloading every call ──
_ou_cache = {"feats": None, "year": None}

async def predict_ou(game_id: int, home_abbr: str, away_abbr: str,
                      yr: int, game_date: str,
                      home_stats, away_stats,
                      line_obj,
                      conn: Optional[asyncpg.Connection] = None) -> tuple[Optional[float], float]:
    """
    Predict total runs for one MLB game.

    Uses the research pipeline's build_features() to compute features,
    cached so it only loads once. Guarantees identical features to training.
    Returns (total, confidence) or (None, 0.0) if model unavailable.
    """
    try:
        model = _load_ou_model()
    except FileNotFoundError:
        logger.warning("OU model not found at %s", OU_MODEL_PATH)
        return None, 0.0
    
    gd_obj = date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
    try:
        from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
        
        # Load and cache features using the SAME pipeline as training
        if _ou_cache["feats"] is None or _ou_cache["year"] != yr:
            eng = _create_async_engine(DB)
            df, pitcher_df = await load_data(eng)
            await eng.dispose()
            feats_df = build_features(df, pitcher_df)
            _ou_cache["feats"] = feats_df
            _ou_cache["year"] = yr
            log(f"OU predict cache loaded: {len(feats_df)} games")
        else:
            feats_df = _ou_cache["feats"]
        
        row = feats_df[feats_df["game_id"] == game_id]
        if len(row) == 0:
            logger.warning(f"Game {game_id} not found in OU features")
            return None, 0.50
        
        r = row.iloc[0]
        # Get closing OU directly from the feature DataFrame (available even though not in FEATURES_TRAINING)
        closing_ou_from_row = float(r.get('closing_ou', r.get('over_under', 8.5))) if pd.notna(r.get('closing_ou', r.get('over_under', 8.5))) else 8.5
        opening_ou_from_row = float(r.get('opening_ou', r.get('opening_total', 8.5))) if pd.notna(r.get('opening_ou', r.get('opening_total', 8.5))) else 8.5
        
        vals = {f: float(r[f]) if pd.notna(r[f]) else 0.0
                for f in FEATURES_TRAINING}
        
        # Override lines from line_obj for live inference
        line_ou = getattr(line_obj, 'over_under', None)
        if line_ou is not None:
            closing_ou = float(line_ou)
        else:
            closing_ou = closing_ou_from_row
        
        line_open = getattr(line_obj, 'opening_total', None)
        if line_open is not None:
            vals["opening_ou"] = float(line_open)
        else:
            vals["opening_ou"] = opening_ou_from_row
        
        vals["ou_movement"] = closing_ou - vals["opening_ou"]
        
        x = np.array([[vals.get(f, 0.0) for f in FEATURES_TRAINING]], dtype=np.float32)
        total = float(model.predict(x)[0])
        conf = min(0.50 + abs(total - float(closing_ou)) * 0.08, 0.95)
        return round(total, 1), round(conf, 2)

    except Exception as e:
        logger.error(f"OU pred failed [{game_id} {home_abbr}@{away_abbr}]: {e}")
        return None, 0.50

async def train_model(year: int, train_years: list[int]) -> object:
    """Train OU model from scratch on given years. Returns trained XGBoost model."""
    features = FEATURES_TRAINING
    engine = create_async_engine(DB)
    df, pitcher_df = await load_data(engine)
    feats = build_features(df, pitcher_df)
    await engine.dispose()
    
    tr_all = feats[feats["year"].isin(train_years)].reset_index(drop=True)
    log(f"Training OU on {len(tr_all)} games ({train_years})")
    
    target_tr = tr_all["actual_total"]
    q25, q75 = target_tr.quantile(0.01), target_tr.quantile(0.99)
    valid = (target_tr >= q25) & (target_tr <= q75)
    tr_all = tr_all[valid].reset_index(drop=True)
    target_tr = target_tr[valid].reset_index(drop=True)
    
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
        n_estimators=300, max_depth=5, learning_rate=0.04,
        subsample=0.7, colsample_bytree=0.7,
        reg_alpha=1.0, reg_lambda=2.0,
        min_child_weight=3,
        random_state=42, n_jobs=-1, verbosity=0,
    )
    model.fit(X_tr, target_tr, sample_weight=w, verbose=False)
    log(f"OU model trained ({len(features)} features)")
    # Save by test year so engine backtest can use the same model
    model_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
    model_dir.mkdir(parents=True, exist_ok=True)
    for ty in range(year, year + 2):
        if ty <= 2026:
            p = model_dir / f"mlb_ou_{ty}.pkl"
            with open(p, "wb") as f:
                pickle.dump(model, f)
            log(f"  Saved OU model to {p}")
    return model

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MLB XGBoost O/U Backtester")
    parser.add_argument("--test-year", type=int, default=2025, help="Year to test on")
    parser.add_argument("--train-from", type=int, default=2021,
                        help="Earliest training year")
    parser.add_argument("--mode", type=str, default="single",
                        choices=["single", "all"],
                        help="Single year or all years")
    args = parser.parse_args()

    if args.mode == "all":
        asyncio.run(run_all_years(train_from=args.train_from))
    else:
        train_years = [y for y in range(args.train_from, args.test_year)]
        asyncio.run(run_single(test_year=args.test_year, train_years=train_years))
