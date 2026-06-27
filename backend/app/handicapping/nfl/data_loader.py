"""
NFL Data Loader & Feature Engineering
======================================
Mirrors the MLB data_loader.py architecture (MLBDataLoader → NFLDataLoader,
mlb.games → nfl.games, mlb.betting_lines_consolidated → nfl.betting_lines_consolidated).

Provides the build_features() transformation and the NFLDataLoader class for
loading game data from the NFL database, computing rolling/situational features,
and returning DataFrames ready for XGBoost training or inference.
"""

import os
import re
import math
import socket
import warnings
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text as sa_text

warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)

# ════════════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════════════

DEFAULT_DB_URL = os.getenv("DATABASE_URL", "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DEFAULT_TRAIN_FROM = 2021
CURRENT_YEAR = datetime.now().year

# ── Game query ──────────────────────────────────────────────────────────
# Pulls from nfl.games, joining betting_lines_consolidated on game_id.
# This query collects all raw columns needed for feature engineering.
GAME_QUERY = """
SELECT
    g.id               AS game_id,
    s.year                AS season_year,
    g.week,
    g.date                AS game_date,
    ht.abbreviation       AS home_abbr,
    at.abbreviation       AS away_abbr,
    g.home_score,
    g.away_score,
    blc.opening_spread  AS spread,
    blc.opening_ou       AS over_under,
    g.roof_type           AS roof,
    g.surface,
    g.temperature         AS temp,
    g.wind_speed          AS wind,
    blc.opening_spread,
    blc.opening_ou        AS opening_total,
    blc.opening_home_ml   AS home_ml,
    blc.opening_away_ml   AS away_ml,
    blc.opening_spread    AS line_opening_spread,
    blc.opening_ou        AS line_opening_total,
    blc.closing_spread    AS line_closing_spread,
    blc.closing_ou        AS line_closing_total
FROM nfl.games g
JOIN nfl.seasons s ON s.id = g.season_id
JOIN nfl.teams ht ON ht.id = g.home_team_id
JOIN nfl.teams at ON at.id = g.away_team_id
LEFT JOIN nfl.betting_lines_consolidated blc ON blc.game_id = g.id
WHERE s.year BETWEEN :min_year AND :max_year
  AND g.home_score IS NOT NULL
  AND g.away_score IS NOT NULL
ORDER BY g.date, g.id
"""

# ── Feature catalog (display names) ─────────────────────────────────────
DISPLAY_NAMES = {
    "home_win_pct": "Home Win %",
    "away_win_pct": "Away Win %",
    "home_ats_pct": "Home ATS %",
    "away_ats_pct": "Away ATS %",
    "home_ppg": "Home PPG",
    "away_ppg": "Away PPG",
    "home_oppg": "Home OPPG",
    "away_oppg": "Away OPPG",
    "home_pt_diff": "Home Pt Diff",
    "away_pt_diff": "Away Pt Diff",
    "home_rolling_ppg": "Home Rolling PPG",
    "away_rolling_ppg": "Away Rolling PPG",
    "home_rolling_oppg": "Home Rolling OPPG",
    "away_rolling_oppg": "Away Rolling OPPG",
    "home_ema_win_pct": "Home EMA Win %",
    "away_ema_win_pct": "Away EMA Win %",
    "rest_diff": "Rest Days Diff",
    "travel_distance_miles": "Travel Distance (mi)",
    "home_timezone_advantage": "Home TZ Advantage",
    "is_dome": "Is Dome",
    "is_division": "Division Game",
    "temp_f": "Temperature (°F)",
    "wind_mph": "Wind (mph)",
    "home_prev_szn_pts": "Home Prev Szn PPG",
    "away_prev_szn_pts": "Away Prev Szn PPG",
    "home_prev_szn_pt_diff": "Home Prev Szn Pt Diff",
    "away_prev_szn_pt_diff": "Away Prev Szn Pt Diff",
    "line_move_spread": "Spread Line Move",
    "line_move_total": "Total Line Move",
    "opening_spread_value": "Opening Spread Value",
    "opening_total_value": "Opening Total Value",
    "home_rest_days": "Home Rest Days",
    "away_rest_days": "Away Rest Days",
    "home_ats_margin": "Home ATS Margin",
    "away_ats_margin": "Away ATS Margin",
}

# ── ATS feature catalog ─────────────────────────────────────────────────
ATS_CATALOG = {
    "home_win_pct": "Home team's rolling win percentage (expanding: prior games)",
    "away_win_pct": "Away team's rolling win percentage (expanding: prior games)",
    "home_ats_pct": "Home team's rolling ATS win percentage",
    "away_ats_pct": "Away team's rolling ATS win percentage",
    "home_pt_diff": "Home team's rolling average point differential",
    "away_pt_diff": "Away team's rolling average point differential",
    "home_rolling_ppg": "Home team's rolling average points scored (home games only)",
    "away_rolling_ppg": "Away team's rolling average points scored (away games only)",
    "home_rolling_oppg": "Home team's rolling average points allowed (home games only)",
    "away_rolling_oppg": "Away team's rolling average points allowed (away games only)",
    "home_ema_win_pct": "Home team's exponential moving average win % (alpha=0.3)",
    "away_ema_win_pct": "Away team's exponential moving average win % (alpha=0.3)",
    "rest_diff": "Difference in rest days between home and away teams",
    "is_dome": "Binary flag: 1 if game is in a domed/indoor stadium",
    "is_division": "Binary flag: 1 if game is a division matchup",
    "temp_f": "Game-time temperature in Fahrenheit (outdoor games)",
    "wind_mph": "Game-time wind speed in mph (outdoor games)",
    "home_prev_szn_pts": "Prior-season PPG for home team (smoothed for expansion years)",
    "away_prev_szn_pts": "Prior-season PPG for away team (smoothed for expansion years)",
    "home_prev_szn_pt_diff": "Prior-season avg point differential for home team",
    "away_prev_szn_pt_diff": "Prior-season avg point differential for away team",
    "line_move_spread": "Change from opening spread to closing spread (opening-closing)",
    "opening_spread_value": "The opening spread value (positive = home underdog)",
    "home_rest_days": "Home team's days of rest since their last game",
    "away_rest_days": "Away team's days of rest since their last game",
    "home_ats_margin": "Home team's rolling ATS margin (actual score minus spread)",
    "away_ats_margin": "Away team's rolling ATS margin (actual score minus spread)",
    "travel_distance_miles": "Great-circle distance in miles between home and away team cities",
    "home_timezone_advantage": "Time zone offset difference (home minus away, positive = home advantage)",
}

# ── OU feature catalog (additional features for OU predictions) ─────────
OU_CATALOG = {
    "home_ppg": "Home team's rolling points per game (all games, all-time)",
    "away_ppg": "Away team's rolling points per game (all games, all-time)",
    "home_oppg": "Home team's rolling opponent points per game (all games, all-time)",
    "away_oppg": "Away team's rolling opponent points per game (all games, all-time)",
    "home_rolling_ppg": "Home team's rolling points per game (home games)",
    "away_rolling_ppg": "Away team's rolling points per game (away games)",
    "home_rolling_oppg": "Home team's rolling opponent points per game (home games)",
    "away_rolling_oppg": "Away team's rolling opponent points per game (away games)",
    "line_move_total": "Change from opening total to closing total (opening-closing)",
    "opening_total_value": "The opening over/under value",
}

# ── Computed features catalog ───────────────────────────────────────────
COMPUTED_FEATURES_CATALOG = {
    "travel_distance_miles": "Great-circle distance in miles between home & away team cities (computed via lookup)",
    "home_timezone_advantage": "Time zone offset difference (home offset minus away offset, positive = home advantage)",
    "home_rest_level": "Categorical rest bucket: 0=short (<6 days), 1=normal (6-7 days), 2=long (10+ days), 3=bye (>13 days)",
    "away_rest_level": "Same as home_rest_level but for the away team",
    "home_stadium_capacity": "Home stadium capacity (static lookup from venues table)",
    "surface_type_grass": "1 if playing surface is natural grass, 0 otherwise",
    "time_of_day_night": "1 if game time is after 5 PM local, 0 otherwise",
    "overtime_flag": "1 if game went to overtime (informational / not for training)",
}

# ── ATS features list ───────────────────────────────────────────────────
ATS_FEATURES = sorted(ATS_CATALOG.keys())

# ── OU features list ────────────────────────────────────────────────────
OU_FEATURES = sorted(
    set(k for k in ATS_CATALOG.keys())
    .union(k for k in OU_CATALOG.keys())
    .difference({"home_ats_pct", "away_ats_pct", "home_ats_margin", "away_ats_margin",
                  "line_move_spread", "opening_spread_value"})
)


def _log(log_fn, msg: str, level: str = "INFO"):
    """Helper to write to a callable log function or print."""
    if log_fn:
        log_fn(f"[{level}] {msg}")
    else:
        print(f"[{level}] {msg}")


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Team stub data (abbreviation → city, timezone, lat/lon) ────────────
# Full set for all active NFL franchises.
_TEAM_META: dict[str, dict] | None = None


def _get_team_meta() -> dict[str, dict]:
    """Return NFL team metadata dictionary (lazy-loaded)."""
    global _TEAM_META
    if _TEAM_META is not None:
        return _TEAM_META
    _TEAM_META = {
        "ARI": {"city": "Glendale", "state": "AZ", "tz": "America/Phoenix", "lat": 33.5273, "lon": -112.2625, "dome": True, "stadium": "State Farm Stadium", "capacity": 63400},
        "ATL": {"city": "Atlanta", "state": "GA", "tz": "America/New_York", "lat": 33.7550, "lon": -84.4009, "dome": True, "stadium": "Mercedes-Benz Stadium", "capacity": 71000},
        "BAL": {"city": "Baltimore", "state": "MD", "tz": "America/New_York", "lat": 39.2780, "lon": -76.6225, "dome": False, "stadium": "M&T Bank Stadium", "capacity": 71008},
        "BUF": {"city": "Orchard Park", "state": "NY", "tz": "America/New_York", "lat": 42.7737, "lon": -78.7870, "dome": False, "stadium": "Highmark Stadium", "capacity": 71608},
        "CAR": {"city": "Charlotte", "state": "NC", "tz": "America/New_York", "lat": 35.2258, "lon": -80.8528, "dome": False, "stadium": "Bank of America Stadium", "capacity": 75523},
        "CHI": {"city": "Chicago", "state": "IL", "tz": "America/Chicago", "lat": 41.8623, "lon": -87.6167, "dome": False, "stadium": "Soldier Field", "capacity": 61500},
        "CIN": {"city": "Cincinnati", "state": "OH", "tz": "America/New_York", "lat": 39.0954, "lon": -84.5161, "dome": False, "stadium": "Paycor Stadium", "capacity": 65515},
        "CLE": {"city": "Cleveland", "state": "OH", "tz": "America/New_York", "lat": 41.5061, "lon": -81.6995, "dome": False, "stadium": "Huntington Bank Field", "capacity": 67895},
        "DAL": {"city": "Arlington", "state": "TX", "tz": "America/Chicago", "lat": 32.7473, "lon": -97.0929, "dome": True, "stadium": "AT&T Stadium", "capacity": 80000},
        "DEN": {"city": "Denver", "state": "CO", "tz": "America/Denver", "lat": 39.7439, "lon": -105.0201, "dome": False, "stadium": "Empower Field at Mile High", "capacity": 76125},
        "DET": {"city": "Detroit", "state": "MI", "tz": "America/New_York", "lat": 42.3400, "lon": -83.0455, "dome": True, "stadium": "Ford Field", "capacity": 65000},
        "GB":  {"city": "Green Bay", "state": "WI", "tz": "America/Chicago", "lat": 44.5013, "lon": -88.0622, "dome": False, "stadium": "Lambeau Field", "capacity": 81441},
        "HOU": {"city": "Houston", "state": "TX", "tz": "America/Chicago", "lat": 29.6847, "lon": -95.4107, "dome": True, "stadium": "NRG Stadium", "capacity": 72000},
        "IND": {"city": "Indianapolis", "state": "IN", "tz": "America/Indiana/Indianapolis", "lat": 39.7601, "lon": -86.1639, "dome": True, "stadium": "Lucas Oil Stadium", "capacity": 67000},
        "JAX": {"city": "Jacksonville", "state": "FL", "tz": "America/New_York", "lat": 30.3240, "lon": -81.6373, "dome": False, "stadium": "EverBank Stadium", "capacity": 67164},
        "KC":  {"city": "Kansas City", "state": "MO", "tz": "America/Chicago", "lat": 39.0489, "lon": -94.4839, "dome": False, "stadium": "GEHA Field at Arrowhead Stadium", "capacity": 76416},
        "LAC": {"city": "Inglewood", "state": "CA", "tz": "America/Los_Angeles", "lat": 33.9531, "lon": -118.3392, "dome": False, "stadium": "SoFi Stadium", "capacity": 70240},
        "LAR": {"city": "Inglewood", "state": "CA", "tz": "America/Los_Angeles", "lat": 33.9531, "lon": -118.3392, "dome": False, "stadium": "SoFi Stadium", "capacity": 70240},
        "LV":  {"city": "Las Vegas", "state": "NV", "tz": "America/Los_Angeles", "lat": 36.0906, "lon": -115.1833, "dome": True, "stadium": "Allegiant Stadium", "capacity": 65000},
        "MIA": {"city": "Miami Gardens", "state": "FL", "tz": "America/New_York", "lat": 25.9580, "lon": -80.2389, "dome": False, "stadium": "Hard Rock Stadium", "capacity": 65326},
        "MIN": {"city": "Minneapolis", "state": "MN", "tz": "America/Chicago", "lat": 44.9739, "lon": -93.2581, "dome": True, "stadium": "U.S. Bank Stadium", "capacity": 66655},
        "NE":  {"city": "Foxborough", "state": "MA", "tz": "America/New_York", "lat": 42.0909, "lon": -71.2643, "dome": False, "stadium": "Gillette Stadium", "capacity": 65878},
        "NO":  {"city": "New Orleans", "state": "LA", "tz": "America/Chicago", "lat": 29.9509, "lon": -90.0814, "dome": True, "stadium": "Caesars Superdome", "capacity": 73208},
        "NYG": {"city": "East Rutherford", "state": "NJ", "tz": "America/New_York", "lat": 40.8128, "lon": -74.0746, "dome": False, "stadium": "MetLife Stadium", "capacity": 82500},
        "NYJ": {"city": "East Rutherford", "state": "NJ", "tz": "America/New_York", "lat": 40.8128, "lon": -74.0746, "dome": False, "stadium": "MetLife Stadium", "capacity": 82500},
        "PHI": {"city": "Philadelphia", "state": "PA", "tz": "America/New_York", "lat": 39.9009, "lon": -75.1675, "dome": False, "stadium": "Lincoln Financial Field", "capacity": 69796},
        "PIT": {"city": "Pittsburgh", "state": "PA", "tz": "America/New_York", "lat": 40.4467, "lon": -80.0157, "dome": False, "stadium": "Acrisure Stadium", "capacity": 68400},
        "SEA": {"city": "Seattle", "state": "WA", "tz": "America/Los_Angeles", "lat": 47.5952, "lon": -122.3316, "dome": True, "stadium": "Lumen Field", "capacity": 68740},
        "SF":  {"city": "Santa Clara", "state": "CA", "tz": "America/Los_Angeles", "lat": 37.4030, "lon": -121.9696, "dome": False, "stadium": "Levi's Stadium", "capacity": 68500},
        "TB":  {"city": "Tampa", "state": "FL", "tz": "America/New_York", "lat": 27.9759, "lon": -82.5033, "dome": False, "stadium": "Raymond James Stadium", "capacity": 65618},
        "TEN": {"city": "Nashville", "state": "TN", "tz": "America/Chicago", "lat": 36.1664, "lon": -86.7713, "dome": False, "stadium": "Nissan Stadium", "capacity": 69143},
        "WAS": {"city": "Landover", "state": "MD", "tz": "America/New_York", "lat": 38.9077, "lon": -76.8645, "dome": False, "stadium": "Northwest Stadium", "capacity": 82000},
    }
    return _TEAM_META


def _prev_szn_smoothing(current_df: pd.DataFrame,
                        team_col: str = "home_abbr",
                        value_col: str = "home_score",
                        suffix: str = "") -> pd.DataFrame:
    """Compute prior-season average for a metric, with smoothing for new teams.

    Groups by (team, season_year-1) to get last year's mean, then forward-fills
    for teams without a prior-year record.
    """
    prev = current_df.groupby([team_col, "season_year"])[value_col].mean().reset_index()
    prev["season_year"] += 1  # shift to apply as prior-year
    prev.rename(columns={value_col: f"prev_szn_mean_{value_col}{suffix}"}, inplace=True)
    return prev


# ════════════════════════════════════════════════════════════════════════
# build_features() — NFL feature engineering
# ════════════════════════════════════════════════════════════════════════

def build_features(df: pd.DataFrame, log_fn: Optional[callable] = None) -> pd.DataFrame:
    """Compute all NFL features on a raw game DataFrame, returning a copy.

    Parameters
    ----------
    df : pd.DataFrame
        Raw game data loaded from the DB (from GAME_QUERY).
    log_fn : callable, optional
        A callable for logging (e.g., print or logger.info).

    Returns
    -------
    pd.DataFrame
        Original DataFrame plus new feature columns.
    """
    df = df.copy()
    n_orig = len(df.columns)

    _log(log_fn, f"build_features: starting with {len(df)} rows, {n_orig} base columns")

    # ── 0. Ensure proper sorts for rolling computations ──────────────
    df = df.sort_values(["home_abbr", "season_year", "week", "game_date"]).reset_index(drop=True)
    df = df.sort_values(["away_abbr", "season_year", "week", "game_date"]).reset_index(drop=True)

    # ── 1. Labels (margin, total, ATS result) ────────────────────────
    df["margin"] = df["home_score"] - df["away_score"]
    df["total"] = df["home_score"] + df["away_score"]
    # Drop games without betting lines (can't compute ATS result)
    before = len(df)
    df = df.dropna(subset=["spread", "over_under"])
    if len(df) < before:
        _log(log_fn, f"[WARN] build_features: dropped {before - len(df)} games with missing betting lines")

    df["home_ats_result"] = (df["margin"] > df["spread"]).astype(int)

    # ── 2. Simple direct features from DB columns ────────────────────
    df["is_dome"] = df["roof"].str.lower().isin({"dome", "closed", "retractable", "roof", "indoor"}).astype(int)
    df["temp_f"] = df["temp"].fillna(70.0)
    df["wind_mph"] = df["wind"].fillna(0.0)

    # Rest days — compute from schedule if not provided
    if "home_rest" in df.columns:
        df["home_rest_days"] = df["home_rest"].fillna(7)
        df["away_rest_days"] = df["away_rest"].fillna(7)
    else:
        # Compute rest from game schedule
        df = df.sort_values(["home_abbr", "game_date"]).copy()
        df["home_rest_days"] = df.groupby("home_abbr")["game_date"].diff().dt.days.fillna(7).clip(0, 14)
        df = df.sort_values(["away_abbr", "game_date"]).copy()
        df["away_rest_days"] = df.groupby("away_abbr")["game_date"].diff().dt.days.fillna(7).clip(0, 14)
    df["rest_diff"] = df["home_rest_days"] - df["away_rest_days"]

    # Division game — compute from teams table
    if "division_game" in df.columns:
        df["is_division"] = df["division_game"].fillna(0).astype(int)
    # division_game will be computed from team_meta below if missing

    # ── 3. Travel & timezone features ────────────────────────────────
    team_meta = _get_team_meta()

    def _abbr_to_tz_offset(abbr: str) -> int:
        meta = team_meta.get(abbr.upper(), {})
        tz_str = meta.get("tz", "America/New_York")
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(tz_str)
            utc_offset = tz.utcoffset(datetime.now())
            return utc_offset.total_seconds() / 3600 if utc_offset else -5
        except Exception:
            return -5

    df["home_tz_offset"] = df["home_abbr"].apply(_abbr_to_tz_offset)
    df["away_tz_offset"] = df["away_abbr"].apply(_abbr_to_tz_offset)
    df["home_timezone_advantage"] = df["home_tz_offset"] - df["away_tz_offset"]

    # Travel distance
    def _get_latlon(abbr: str):
        m = team_meta.get(abbr.upper(), {})
        return m.get("lat", 40.0), m.get("lon", -95.0)

    df["home_lat"] = df["home_abbr"].apply(lambda x: _get_latlon(x)[0])
    df["home_lon"] = df["home_abbr"].apply(lambda x: _get_latlon(x)[1])
    df["away_lat"] = df["away_abbr"].apply(lambda x: _get_latlon(x)[0])
    df["away_lon"] = df["away_abbr"].apply(lambda x: _get_latlon(x)[1])
    df["travel_distance_miles"] = df.apply(
        lambda r: _haversine(r["away_lat"], r["away_lon"], r["home_lat"], r["home_lon"]),
        axis=1,
    )

    # ── 4. Rolling team quality features ──────────────────────────────
    # We need a team-level view across all teams. Build a unified game log.
    games_exploded = []  # rows with team, opponent, is_home, etc.

    for team_side, opp_side in [("home", "away"), ("away", "home")]:
        chunk = pd.DataFrame()
        chunk["game_id"] = df["game_id"]
        chunk["season_year"] = df["season_year"]
        chunk["week"] = df["week"]
        chunk["game_date"] = df["game_date"]
        chunk["team"] = df[f"{team_side}_abbr"]
        chunk["opponent"] = df[f"{opp_side}_abbr"]
        chunk["is_home"] = 1 if team_side == "home" else 0
        chunk["pts_for"] = df[f"{team_side}_score"]
        chunk["pts_against"] = df[f"{opp_side}_score"]
        chunk["margin"] = chunk["pts_for"] - chunk["pts_against"]
        chunk["ats_result"] = (
            (chunk["margin"] > df["spread"]) if team_side == "home"
            else (chunk["margin"] < df["spread"])
        ).astype(int)
        chunk["ats_margin"] = (
            chunk["margin"] - df["spread"] if team_side == "home"
            else df["spread"] - chunk["margin"]
        )
        games_exploded.append(chunk)

    games_all = pd.concat(games_exploded, ignore_index=True)
    games_all = games_all.sort_values(["team", "season_year", "week", "game_date"]).reset_index(drop=True)

    # ── Expanding window features per team ─────────────────────────
    team_rolling = {}
    for team, grp in games_all.groupby("team"):
        grp = grp.sort_values(["season_year", "week", "game_date"]).reset_index(drop=True)

        grp["roll_win_pct"] = grp["margin"].apply(lambda x: 1 if x > 0 else 0).expanding().mean().shift(1)
        grp["roll_ats_pct"] = grp["ats_result"].expanding().mean().shift(1)
        grp["roll_pt_diff"] = grp["margin"].expanding().mean().shift(1)
        grp["roll_ats_margin"] = grp["ats_margin"].expanding().mean().shift(1)
        grp["roll_ppg"] = grp["pts_for"].expanding().mean().shift(1)
        grp["roll_oppg"] = grp["pts_against"].expanding().mean().shift(1)

        # EMA win %
        win_series = grp["margin"].apply(lambda x: 1 if x > 0 else 0)
        ema_vals = win_series.ewm(alpha=0.3, adjust=False).mean().shift(1)
        grp["roll_ema_win_pct"] = ema_vals

        team_rolling[team] = grp[["game_id", "team",
                                   "roll_win_pct", "roll_ats_pct", "roll_pt_diff",
                                   "roll_ats_margin", "roll_ppg", "roll_oppg",
                                   "roll_ema_win_pct"]]

    rolling_all = pd.concat(team_rolling.values(), ignore_index=True)

    # ── 5. Home/Away scoring splits (expanding mean, shift(1)) ──────
    # Rolling by team for home games only and away games only
    teams = games_all["team"].unique()

    home_off_rows = []
    home_def_rows = []
    away_off_rows = []
    away_def_rows = []

    for team in teams:
        team_games = games_all[games_all["team"] == team].sort_values(["season_year", "week", "game_date"])
        home_games = team_games[team_games["is_home"] == 1].copy()
        away_games = team_games[team_games["is_home"] == 0].copy()

        if len(home_games) > 0:
            home_games["home_rolling_ppg"] = home_games["pts_for"].expanding().mean().shift(1)
            home_games["home_rolling_oppg"] = home_games["pts_against"].expanding().mean().shift(1)
            home_off_rows.append(home_games[["game_id", "team", "home_rolling_ppg"]])
            home_def_rows.append(home_games[["game_id", "team", "home_rolling_oppg"]])

        if len(away_games) > 0:
            away_games["away_rolling_ppg"] = away_games["pts_for"].expanding().mean().shift(1)
            away_games["away_rolling_oppg"] = away_games["pts_against"].expanding().mean().shift(1)
            away_off_rows.append(away_games[["game_id", "team", "away_rolling_ppg"]])
            away_def_rows.append(away_games[["game_id", "team", "away_rolling_oppg"]])

    home_off = pd.concat(home_off_rows, ignore_index=True) if home_off_rows else pd.DataFrame(columns=["game_id", "team", "home_rolling_ppg"])
    home_def = pd.concat(home_def_rows, ignore_index=True) if home_def_rows else pd.DataFrame(columns=["game_id", "team", "home_rolling_oppg"])
    away_off = pd.concat(away_off_rows, ignore_index=True) if away_off_rows else pd.DataFrame(columns=["game_id", "team", "away_rolling_ppg"])
    away_def = pd.concat(away_def_rows, ignore_index=True) if away_def_rows else pd.DataFrame(columns=["game_id", "team", "away_rolling_oppg"])

    # Merge rolling features into main df
    # Home side
    home_map = df[["game_id", "home_abbr"]].copy()
    away_map = df[["game_id", "away_abbr"]].copy()

    for col, src in [("home_win_pct", rolling_all), ("home_ats_pct", rolling_all),
                     ("home_pt_diff", rolling_all), ("home_ats_margin", rolling_all),
                     ("home_ppg", rolling_all), ("home_oppg", rolling_all),
                     ("home_ema_win_pct", rolling_all)]:
        suffix = col.replace("home_", "roll_")
        if suffix not in src.columns:
            continue
        src_col = src[["game_id", "team", suffix]].copy()
        src_col = src_col.rename(columns={suffix: col})
        df = df.merge(src_col, left_on=["game_id", "home_abbr"], right_on=["game_id", "team"], how="left")
        df = df.drop(columns=["team"], errors="ignore")

    # Away side
    for col, src in [("away_win_pct", rolling_all), ("away_ats_pct", rolling_all),
                     ("away_pt_diff", rolling_all), ("away_ats_margin", rolling_all),
                     ("away_ppg", rolling_all), ("away_oppg", rolling_all),
                     ("away_ema_win_pct", rolling_all)]:
        suffix = col.replace("away_", "roll_")
        if suffix not in src.columns:
            continue
        src_col = src[["game_id", "team", suffix]].copy()
        src_col = src_col.rename(columns={suffix: col})
        df = df.merge(src_col, left_on=["game_id", "away_abbr"], right_on=["game_id", "team"], how="left")
        df = df.drop(columns=["team"], errors="ignore")

    # Merge home/away split rolling stats
    home_off_renamed = home_off.rename(columns={"team": "home_abbr"})
    df = df.merge(home_off_renamed[["game_id", "home_abbr", "home_rolling_ppg"]],
                  on=["game_id", "home_abbr"], how="left")
    home_def_renamed = home_def.rename(columns={"team": "home_abbr"})
    df = df.merge(home_def_renamed[["game_id", "home_abbr", "home_rolling_oppg"]],
                  on=["game_id", "home_abbr"], how="left")
    away_off_renamed = away_off.rename(columns={"team": "away_abbr"})
    df = df.merge(away_off_renamed[["game_id", "away_abbr", "away_rolling_ppg"]],
                  on=["game_id", "away_abbr"], how="left")
    away_def_renamed = away_def.rename(columns={"team": "away_abbr"})
    df = df.merge(away_def_renamed[["game_id", "away_abbr", "away_rolling_oppg"]],
                  on=["game_id", "away_abbr"], how="left")

    # ── 6. Prior-season smoothing ───────────────────────────────────
    for team_col, score_col, suffix in [
        ("home_abbr", "home_score", "_home"),
        ("away_abbr", "away_score", "_away"),
    ]:
        prev = _prev_szn_smoothing(df, team_col=team_col, value_col=score_col, suffix=suffix)
        # Merge on team + (season_year+1)
        target = f"prev_szn_mean_{score_col}{suffix}"
        df = df.merge(
            prev.rename(columns={target: f"temp_{target}"}),
            left_on=[team_col, "season_year"],
            right_on=[team_col, "season_year"],
            how="left",
        )
        # Shift so prev_szn value applies to current season
        df[f"temp_{target}"] = df.groupby(team_col)[f"temp_{target}"].transform(lambda x: x.shift(1))
        # Store as clean name
        if suffix == "_home":
            df["home_prev_szn_pts"] = df[f"temp_{target}"]
        else:
            df["away_prev_szn_pts"] = df[f"temp_{target}"]

    # Prior-season point differential
    for team_col, margin_col, suffix in [
        ("home_abbr", "margin", "_home"),
        ("away_abbr", "margin", "_away"),
    ]:
        prev_margin = _prev_szn_smoothing(df, team_col=team_col, value_col=margin_col, suffix=suffix)
        target = f"prev_szn_mean_{margin_col}{suffix}"
        df = df.merge(
            prev_margin.rename(columns={target: f"temp_{target}"}),
            left_on=[team_col, "season_year"],
            right_on=[team_col, "season_year"],
            how="left",
        )
        df[f"temp_{target}"] = df.groupby(team_col)[f"temp_{target}"].transform(lambda x: x.shift(1))
        if suffix == "_home":
            df["home_prev_szn_pt_diff"] = df[f"temp_{target}"]
        else:
            df["away_prev_szn_pt_diff"] = df[f"temp_{target}"]

    # Clean up temp columns
    temp_cols = [c for c in df.columns if c.startswith("temp_")]
    df.drop(columns=temp_cols, inplace=True, errors="ignore")

    # ── 7. Line movement features ────────────────────────────────────
    df["line_move_spread"] = df["line_opening_spread"] - df["line_closing_spread"]
    df["line_move_total"] = df["line_opening_total"] - df["line_closing_total"]
    df["opening_spread_value"] = df["opening_spread"].fillna(df["line_opening_spread"])
    df["opening_total_value"] = df["opening_total"].fillna(df["line_opening_total"])
    # Fill any NaN line moves
    df["line_move_spread"] = df["line_move_spread"].fillna(0.0)
    df["line_move_total"] = df["line_move_total"].fillna(0.0)
    df["opening_spread_value"] = df["opening_spread_value"].fillna(0.0)
    df["opening_total_value"] = df["opening_total_value"].fillna(0.0)

    # ── 8. Drop intermediate columns not used for training ──────────
    drop_cols = [
        "home_lat", "home_lon", "away_lat", "away_lon",
        "home_tz_offset", "away_tz_offset",
    ]
    for c in drop_cols:
        if c in df.columns:
            df.drop(columns=[c], inplace=True)

    # ── 9. Fill remaining NaN values ────────────────────────────────
    float_cols = df.select_dtypes(include=["float64", "float32"]).columns
    df[float_cols] = df[float_cols].fillna(0.0)

    int_cols = df.select_dtypes(include=["int64", "int32"]).columns
    df[int_cols] = df[int_cols].fillna(0)

    n_new = len(df.columns) - n_orig
    _log(log_fn, f"build_features: built {n_new} features, result {len(df)} rows")

    return df


# ════════════════════════════════════════════════════════════════════════
# NFLDataLoader class
# ════════════════════════════════════════════════════════════════════════

class NFLDataLoader:
    """NFL-specific DataLoader mirroring MLBDataLoader's interface.

    Handles DB connection, game data loading, feature computation, and
    feature-catalog maintenance in the nfl.features table.
    """

    def __init__(self, db_url: str = None):
        self.db_url = db_url or DEFAULT_DB_URL
        self.engine = None
        self._features: dict[str, dict] = {}

        # Load feature metadata from catalogs (mirrors MLB's _features dict)
        for name, desc in ATS_CATALOG.items():
            self._features[name] = {
                "description": desc,
                "display_name": DISPLAY_NAMES.get(name, name.replace("_", " ").title()),
                "current_ats": True,
                "current_ou": name in OU_CATALOG,
                "is_trainable": True,
            }
        for name, desc in OU_CATALOG.items():
            if name not in self._features:
                self._features[name] = {
                    "description": desc,
                    "display_name": DISPLAY_NAMES.get(name, name.replace("_", " ").title()),
                    "current_ats": False,
                    "current_ou": True,
                    "is_trainable": True,
                }
        for name, desc in COMPUTED_FEATURES_CATALOG.items():
            self._features[name] = {
                "description": desc,
                "display_name": DISPLAY_NAMES.get(name, name.replace("_", " ").title()),
                "current_ats": name in ATS_CATALOG,
                "current_ou": name in OU_CATALOG,
                "is_trainable": True,
            }

    # ── Properties ──────────────────────────────────────────────────
    @property
    def features(self) -> dict[str, dict]:
        return dict(self._features)

    @property
    def ats_features(self) -> list[str]:
        return sorted(k for k, v in self._features.items() if v["current_ats"])

    @property
    def ou_features(self) -> list[str]:
        return sorted(k for k, v in self._features.items() if v["current_ou"])

    # ── DB connection ────────────────────────────────────────────────
    def _ensure_engine(self):
        if self.engine is None:
            self.engine = create_engine(self.db_url, pool_pre_ping=True, pool_size=5)

    async def _ensure_engine_async(self):
        """Async-compatible engine init (internal; mirrors MLB pattern)."""
        self._ensure_engine()

    # ── load_games() ─────────────────────────────────────────────────
    async def load_games(self,
                         min_year: int = DEFAULT_TRAIN_FROM,
                         max_year: int = CURRENT_YEAR,
                         engine=None,
                         log_fn: Optional[callable] = None) -> pd.DataFrame:
        """Load NFL game data from nfl.games for the given year range.

        Parameters
        ----------
        min_year : int
            Earliest season year (default DEFAULT_TRAIN_FROM).
        max_year : int
            Latest season year (default CURRENT_YEAR).
        engine : sqlalchemy Engine, optional
            Reusable DB engine. If None, creates one.
        log_fn : callable, optional
            Logging callback.

        Returns
        -------
        pd.DataFrame
            Raw game data with all columns from GAME_QUERY.
        """
        if engine is not None:
            self.engine = engine
        self._ensure_engine()

        _log(log_fn, f"NFLDataLoader.load_games: {min_year}–{max_year}")

        query = sa_text(GAME_QUERY).bindparams(min_year=min_year, max_year=max_year)

        with self.engine.connect() as conn:
            df = pd.read_sql_query(query, conn)

        _log(log_fn, f"NFLDataLoader.load_games: loaded {len(df)} games")
        return df

    # ── load_training_data() ─────────────────────────────────────────
    async def load_training_data(self,
                                 min_year: int = DEFAULT_TRAIN_FROM,
                                 max_year: int = CURRENT_YEAR,
                                 engine=None,
                                 log_fn: Optional[callable] = None) -> pd.DataFrame:
        """Load game data and apply build_features().

        Returns a DataFrame ready for XGBoost training (all feature columns).
        """
        df = await self.load_games(min_year=min_year, max_year=max_year,
                                    engine=engine, log_fn=log_fn)
        df = build_features(df, log_fn=log_fn)
        return df

    # ── build_inference_data() ──────────────────────────────────────
    async def build_inference_data(self, year: int, week: int = None,
                                   engine=None,
                                   log_fn: Optional[callable] = None) -> pd.DataFrame:
        """Load and featurize data for inference (future/unplayed games).

        This loads games with NULL scores (scheduled but not yet played)
        for the given year/week.
        """
        if engine is not None:
            self.engine = engine
        self._ensure_engine()

        _log(log_fn, f"NFLDataLoader.build_inference_data: year={year}, week={week}")

        # Build an inference query similar to GAME_QUERY but for unplayed games
        inference_query = """
        WITH lines_dedup AS (
            SELECT DISTINCT ON (blc.game_id)
                blc.game_id,
                blc.spread           AS opening_spread,
                blc.over_under       AS opening_total,
                blc.home_moneyline,
                blc.away_moneyline,
                blc.opening_spread   AS line_opening_spread,
                blc.opening_over_under AS line_opening_total,
                blc.spread           AS line_closing_spread,
                blc.over_under       AS line_closing_total
            FROM nfl.betting_lines_consolidated blc
            ORDER BY blc.game_id, blc.updated_at DESC NULLS LAST
        )
        SELECT
            g.game_id,
            g.season_year,
            g.week,
            g.game_date,
            g.ha  AS home_abbr,
            g.aa  AS away_abbr,
            g.home_score,
            g.away_score,
            g.spread,
            g.over_under,
            g.neutral_site,
            g.roof,
            g.surface,
            g.temp,
            g.wind,
            g.venue,
            g.home_rest,
            g.away_rest,
            g.home_division,
            g.away_division,
            g.division_game,
            g.game_time,
            g.tv_network,
            g.overtime,
            ld.opening_spread,
            ld.opening_total,
            ld.home_moneyline,
            ld.away_moneyline,
            ld.line_opening_spread,
            ld.line_opening_total,
            ld.line_closing_spread,
            ld.line_closing_total
        FROM nfl.games g
        LEFT JOIN lines_dedup ld ON ld.game_id = g.game_id
        WHERE g.season_year = :year
          AND g.home_score IS NULL
          AND g.away_score IS NULL
        """

        params = {"year": year}
        if week is not None:
            inference_query += "  AND g.week = :week\n"
            params["week"] = week

        inference_query += "ORDER BY g.game_date, g.game_id"

        query = sa_text(inference_query).bindparams(**params)

        with self.engine.connect() as conn:
            df = pd.read_sql_query(query, conn)

        if len(df) == 0:
            _log(log_fn, f"No unplayed games found for {year} week {week}")
            return df

        df = build_features(df, log_fn=log_fn)
        return df

    # ── save features to DB ─────────────────────────────────────────
    async def save_features_to_db(self, engine=None, log_fn: Optional[callable] = None):
        """Upsert the current feature catalog into nfl.features table."""
        if engine is not None:
            self.engine = engine
        self._ensure_engine()

        import json

        features_data = []
        for name, meta in self._features.items():
            features_data.append({
                "feature_name": name,
                "display_name": meta["display_name"],
                "description": meta["description"],
                "is_trainable": meta.get("is_trainable", True),
                "sport": "nfl",
                "metadata": json.dumps({
                    "current_ats": meta.get("current_ats", True),
                    "current_ou": meta.get("current_ou", False),
                    "category": "rolling" if "rolling" in name else "situational"
                    if name in ("is_dome", "is_division", "rest_diff", "travel_distance_miles")
                    else "team_quality" if "win_pct" in name or "pt_diff" in name
                    else "line" if "line_" in name or "opening_" in name
                    else "prior_season" if "prev_szn" in name
                    else "other",
                }),
            })

        if not features_data:
            _log(log_fn, "No features to save", "WARN")
            return

        with self.engine.begin() as conn:
            for rec in features_data:
                conn.execute(
                    sa_text("""
                        INSERT INTO nfl.features (feature_name, display_name, description, is_trainable, sport, metadata)
                        VALUES (:feature_name, :display_name, :description, :is_trainable, :sport, :metadata::jsonb)
                        ON CONFLICT (feature_name) DO UPDATE SET
                            display_name = EXCLUDED.display_name,
                            description = EXCLUDED.description,
                            is_trainable = EXCLUDED.is_trainable,
                            metadata = EXCLUDED.metadata::jsonb,
                            updated_at = NOW()
                    """),
                    rec
                )
        _log(log_fn, f"Saved {len(features_data)} features to nfl.features")


# ════════════════════════════════════════════════════════════════════════
# _ADDITIONAL_FEATURES_CATALOG – computed feature descriptions for API
# ════════════════════════════════════════════════════════════════════════

_ADDITIONAL_FEATURES_CATALOG = dict(COMPUTED_FEATURES_CATALOG)
_ADDITIONAL_FEATURES_CATALOG.update({
    "margin": "Home score minus away score (label for ATS model)",
    "total": "Home score plus away score (label for OU model)",
    "home_ats_result": "Binary: 1 if home team covered the spread",
})


# ════════════════════════════════════════════════════════════════════════
# Standalone entry point
# ════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """Quick test: load data and verify feature columns."""
    import asyncio

    async def _test():
        loader = NFLDataLoader()
        df = await loader.load_training_data(min_year=2024, max_year=2024)
        print(f"Loaded {len(df)} rows, {len(df.columns)} columns")
        print("Columns:", sorted(df.columns.tolist()))
        feats = loader.ats_features
        print(f"ATS features ({len(feats)}): {feats}")

    asyncio.run(_test())
