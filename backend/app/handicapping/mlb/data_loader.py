"""
data_loader.py — single source of truth for MLB data loading

Loads ALL game-level, team-level, pitcher-level, betting, and weather data
into pandas DataFrames for:
  • Training (XGBoost models — ATS / OU / ML)
  • Backtesting (walk-forward simulation)
  • Inference (predicting upcoming games)
  • Pick-card display (features the customer sees)

Everything feeds from the same base query so feature definitions stay consistent
across every use case. Downstream code should NEVER write its own SQL to load
MLB game data — use this module.

Usage (sync):
    from app.handicapping.mlb.data_loader import get_data_loader
    dl = get_data_loader()
    df = dl.load_games(seasons=[2024, 2025])

Usage (async):
    dl = get_data_loader()
    await dl.load_games_async(engine, seasons=[2024, 2025])
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import math

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

# ── Default connection ───────────────────────────────────────────────────────

DEFAULT_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)


# ── MLB team → stadium location mapping (lat, lon, timezone offset) ──────────
# Timezone offset is UTC hour offset (e.g. -5 for Eastern, -8 for Pacific)
# Stadium coordinates from official ballpark locations

TEAM_LOCATIONS = {
    "ARI": {"lat": 33.4457, "lon": -112.0667, "tz": -7},   # Chase Field
    "ATL": {"lat": 33.8908, "lon": -84.4676, "tz": -5},   # Truist Park
    "BAL": {"lat": 39.2838, "lon": -76.6217, "tz": -5},   # Oriole Park at Camden Yards
    "BOS": {"lat": 42.3467, "lon": -71.0972, "tz": -5},   # Fenway Park
    "CHC": {"lat": 41.9484, "lon": -87.6553, "tz": -6},   # Wrigley Field
    "CIN": {"lat": 39.0972, "lon": -84.5066, "tz": -5},   # Great American Ball Park
    "CLE": {"lat": 41.4962, "lon": -81.6852, "tz": -5},   # Progressive Field
    "COL": {"lat": 39.7559, "lon": -104.9942, "tz": -7},  # Coors Field
    "CWS": {"lat": 41.8300, "lon": -87.6339, "tz": -6},   # Rate Field (formerly Guaranteed Rate)
    "DET": {"lat": 42.3390, "lon": -83.0485, "tz": -5},   # Comerica Park
    "HOU": {"lat": 29.7570, "lon": -95.3554, "tz": -6},   # Daikin Park (former Minute Maid)
    "KC":  {"lat": 39.0517, "lon": -94.4804, "tz": -6},   # Kauffman Stadium
    "LAA": {"lat": 33.8003, "lon": -117.8827, "tz": -8},  # Angel Stadium
    "LAD": {"lat": 34.0740, "lon": -118.2400, "tz": -8},  # Dodger Stadium
    "MIA": {"lat": 25.7781, "lon": -80.2198, "tz": -5},   # LoanDepot Park
    "MIL": {"lat": 43.0279, "lon": -87.9715, "tz": -6},   # American Family Field
    "MIN": {"lat": 44.9817, "lon": -93.2777, "tz": -6},   # Target Field
    "NYM": {"lat": 40.7571, "lon": -73.8458, "tz": -5},   # Citi Field
    "NYY": {"lat": 40.8296, "lon": -73.9262, "tz": -5},   # Yankee Stadium
    "OAK": {"lat": 37.7516, "lon": -122.2006, "tz": -8},  # Oakland Coliseum
    "PHI": {"lat": 39.9057, "lon": -75.1666, "tz": -5},   # Citizens Bank Park
    "PIT": {"lat": 40.4469, "lon": -79.9891, "tz": -5},   # PNC Park
    "SD":  {"lat": 32.7076, "lon": -117.1570, "tz": -8},   # Petco Park
    "SEA": {"lat": 47.5914, "lon": -122.3326, "tz": -8},  # T-Mobile Park
    "SF":  {"lat": 37.7786, "lon": -122.3893, "tz": -8},   # Oracle Park
    "STL": {"lat": 38.6226, "lon": -90.1928, "tz": -6},   # Busch Stadium
    "TB":  {"lat": 27.7682, "lon": -82.6534, "tz": -5},   # Tropicana Field (dome, St. Pete)
    "TEX": {"lat": 32.7479, "lon": -97.0834, "tz": -6},   # Globe Life Field
    "TOR": {"lat": 43.6414, "lon": -79.3894, "tz": -5},   # Rogers Centre
    "WSH": {"lat": 38.8730, "lon": -77.0074, "tz": -5},   # Nationals Park
}


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between lat/lon points.  Accepts scalars or arrays."""
    import numpy as np
    R = 3958.8  # Earth radius in miles
    dlat = np.radians(np.asarray(lat2, dtype=float) - np.asarray(lat1, dtype=float))
    dlon = np.radians(np.asarray(lon2, dtype=float) - np.asarray(lon1, dtype=float))
    lat1_r = np.radians(np.asarray(lat1, dtype=float))
    lat2_r = np.radians(np.asarray(lat2, dtype=float))
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


# ── Master game-level SQL query ──────────────────────────────────────────────

GAME_QUERY = """
SELECT
    -- Game identity
    g.id               AS game_id,
    g.season_id,
    s.year             AS season_year,
    g.date       AS game_date,
    g.status,
    g.game_type,
    g.day_night,
    g.roof_type,
    g.surface,
    g.venue,
    g.home_wins,
    g.home_losses,
    g.away_wins,
    g.away_losses,
    g.home_pitcher_name,
    g.away_pitcher_name,
    g.game_number,
    g.attendance,
    g.actual_innings,
    g.scheduled_innings,
    g.duration_minutes,
    g.mlb_game_id,
    g.venue_id,

    -- Teams
    ht.id              AS home_team_id,
    ht.name       AS home_team,
    ht.name       AS home_team_name,
    ht.abbreviation    AS home_abbr,
    ht.logo_url        AS home_logo,
    at.id              AS away_team_id,
    at.name       AS away_team,
    at.name       AS away_team_name,
    at.abbreviation    AS away_abbr,
    at.logo_url        AS away_logo,
    ht.division        AS hdiv,
    at.division        AS adiv,

    -- Score
    g.home_score,
    g.away_score,
    (g.home_score - g.away_score) AS actual_margin,
    (g.home_score + g.away_score) AS actual_total,
    (g.home_score - g.away_score) AS margin,

    -- Venue / environment
    v.name             AS venue_name,
    v.surface          AS venue_surface,
    v.roof_type        AS venue_roof,
    v.capacity         AS venue_capacity,
    v.latitude         AS venue_latitude,
    v.longitude        AS venue_longitude,
    

    g.weather_condition AS weather_condition,
    g.wind_speed,
    g.wind_direction,
    g.temperature,
    

    -- Rest days
    0 AS h_rest,  -- TODO: compute from schedule
    0 AS a_rest,  -- TODO: compute from schedule

    -- ──────────────────────────────────────────────────────────────────────
    -- CUMULATIVE STATS (season-to-date entering this game)
    -- From: mlb.cumulative_game_stats
    -- ──────────────────────────────────────────────────────────────────────
    cgs_h.bat_runs         AS h_cum_runs,
    cgs_h.bat_hits         AS h_cum_hits,
    cgs_h.bat_at_bats      AS h_cum_at_bats,
    cgs_h.cum_avg          AS h_cum_avg,
    cgs_h.cum_obp          AS h_cum_obp,
    cgs_h.cum_slg          AS h_cum_slg,
    cgs_h.cum_ops          AS h_cum_ops,
    cgs_h.cum_babip        AS h_cum_babip,
    cgs_h.cum_k_rate       AS h_cum_k_rate,
    cgs_h.cum_bb_rate      AS h_cum_bb_rate,
    cgs_h.cum_era          AS h_cum_era,
    cgs_h.cum_whip         AS h_cum_whip,
    cgs_h.cum_k9           AS h_cum_k9,
    cgs_h.cum_bb9          AS h_cum_bb9,

    cgs_a.bat_runs         AS a_cum_runs,
    cgs_a.bat_hits         AS a_cum_hits,
    cgs_a.bat_at_bats      AS a_cum_at_bats,
    cgs_a.cum_avg          AS a_cum_avg,
    cgs_a.cum_obp          AS a_cum_obp,
    cgs_a.cum_slg          AS a_cum_slg,
    cgs_a.cum_ops          AS a_cum_ops,
    cgs_a.cum_babip        AS a_cum_babip,
    cgs_a.cum_k_rate       AS a_cum_k_rate,
    cgs_a.cum_bb_rate      AS a_cum_bb_rate,
    cgs_a.cum_era          AS a_cum_era,
    cgs_a.cum_whip         AS a_cum_whip,
    cgs_a.cum_k9           AS a_cum_k9,
    cgs_a.cum_bb9          AS a_cum_bb9,

    -- Home/away game counts & venue win% (pre-computed in mlb.team_rolling_stats)
    COALESCE(trs_h.home_games_sofar, 0)       AS h_home_games,
    COALESCE(trs_a.away_games_sofar, 0)       AS a_away_games,
    COALESCE(trs_a.game_away_venue_pct, 0.5)   AS a_team_venue_winpct,

    -- Bullpen
    bg_h.bullpen_er        AS h_bullpen_er,
    bg_h.bullpen_ip_outs   AS h_bullpen_ip,
    bg_h.num_pitchers      AS h_bullpen_num_pitchers,
    bg_a.bullpen_er        AS a_bullpen_er,
    bg_a.bullpen_ip_outs   AS a_bullpen_ip,
    bg_a.num_pitchers      AS a_bullpen_num_pitchers,

    -- ──────────────────────────────────────────────────────────────────────
    -- TEAM ROLLING STATS (rolling windows)
    -- From: mlb.team_rolling_stats
    -- ──────────────────────────────────────────────────────────────────────
    trs_h.rf              AS h_rf,
    trs_h.ra              AS h_ra,
    trs_h.rf5             AS h_rf5,
    trs_h.ra5             AS h_ra5,
    trs_h.rf10            AS h_rf10,
    trs_h.ra10            AS h_ra10,
    trs_h.rf15            AS h_rf15,
    trs_h.ra15            AS h_ra15,
    trs_h.avg5            AS h_avg_5,
    trs_h.avg10           AS h_avg_10,
    trs_h.avg15           AS h_avg_15,
    trs_h.obp5            AS h_obp_5,
    trs_h.obp10           AS h_obp_10,
    trs_h.ops5            AS h_ops_5,
    trs_h.ops10           AS h_ops_10,
    trs_h.ops15           AS h_ops_15,
    trs_h.era5            AS h_era_5,
    trs_h.era10           AS h_era_10,
    trs_h.era15           AS h_era_15,
    trs_h.whip5           AS h_whip_5,
    trs_h.whip10          AS h_whip_10,
    trs_h.whip15          AS h_whip_15,
    trs_h.k9_5            AS h_k9_5,
    trs_h.k9_10           AS h_k9_10,
    trs_h.bb9_5           AS h_bb9_5,
    trs_h.bb9_10          AS h_bb9_10,
    trs_h.win_pct         AS h_win_pct,
    trs_h.over_pct        AS h_over_pct,
    trs_h.over_pct5       AS h_over_pct5,
    trs_h.over_pct10      AS h_over_pct10,
    trs_h.over_pct15      AS h_over_pct15,
    trs_h.spread_pct      AS h_spread_pct,
    trs_h.rf20            AS h_rf20,
    trs_h.ra20            AS h_ra20,
    trs_h.slg10           AS h_slg_l10,
    trs_h.slg20           AS h_slg_l20,
    trs_h.ops20           AS h_ops_l20,

    trs_a.rf              AS a_rf,
    trs_a.ra              AS a_ra,
    trs_a.rf5             AS a_rf5,
    trs_a.ra5             AS a_ra5,
    trs_a.rf10            AS a_rf10,
    trs_a.ra10            AS a_ra10,
    trs_a.rf15            AS a_rf15,
    trs_a.ra15            AS a_ra15,
    trs_a.avg5            AS a_avg_5,
    trs_a.avg10           AS a_avg_10,
    trs_a.avg15           AS a_avg_15,
    trs_a.obp5            AS a_obp_5,
    trs_a.obp10           AS a_obp_10,
    trs_a.ops5            AS a_ops_5,
    trs_a.ops10           AS a_ops_10,
    trs_a.ops15           AS a_ops_15,
    trs_a.era5            AS a_era_5,
    trs_a.era10           AS a_era_10,
    trs_a.era15           AS a_era_15,
    trs_a.whip5           AS a_whip_5,
    trs_a.whip10          AS a_whip_10,
    trs_a.whip15          AS a_whip_15,
    trs_a.k9_5            AS a_k9_5,
    trs_a.k9_10           AS a_k9_10,
    trs_a.bb9_5           AS a_bb9_5,
    trs_a.bb9_10          AS a_bb9_10,
    trs_a.win_pct         AS a_win_pct,
    trs_a.over_pct        AS a_over_pct,
    trs_a.over_pct5       AS a_over_pct5,
    trs_a.over_pct10      AS a_over_pct10,
    trs_a.over_pct15      AS a_over_pct15,
    trs_a.spread_pct      AS a_spread_pct,
    trs_a.rf20            AS a_rf20,
    trs_a.ra20            AS a_ra20,
    trs_a.slg10           AS a_slg_l10,
    trs_a.slg20           AS a_slg_l20,
    trs_a.ops20           AS a_ops_l20,

    -- ──────────────────────────────────────────────────────────────────────
    -- PRIOR SEASON STATS (for early-season blending)
    -- From: mlb.prior_team_stats
    -- ──────────────────────────────────────────────────────────────────────


    -- ──────────────────────────────────────────────────────────────────────
    -- PITCHER STATS (from pitcher_rolling_stats + PTY info from current game)
    -- Rolling stats for home starter and away starter
    -- ──────────────────────────────────────────────────────────────────────
    prs_h.era_ytd         AS h_p_era_ytd,
    prs_h.whip_ytd        AS h_p_whip_ytd,
    prs_h.k9_ytd          AS h_p_k9_ytd,
    prs_h.bb9_ytd         AS h_p_bb9_ytd,
    prs_h.kbb_ytd         AS h_p_kbb_ytd,
    prs_h.fip_ytd         AS h_p_fip_ytd,
    prs_h.qs_rate_ytd     AS h_p_qs_rate_ytd,
    prs_h.starts_ytd      AS h_p_starts_ytd,
    prs_h.era_5           AS h_p_era_5,
    prs_h.whip_5          AS h_p_whip_5,
    prs_h.k9_5            AS h_p_k9_5,
    prs_h.bb9_5           AS h_p_bb9_5,
    prs_h.era_10          AS h_p_era_10,
    prs_h.whip_10         AS h_p_whip_10,
    prs_h.k9_10           AS h_p_k9_10,
    prs_h.bb9_10          AS h_p_bb9_10,
    prs_h.era_15          AS h_p_era_15,
    prs_h.whip_15         AS h_p_whip_15,
    prs_h.k9_15           AS h_p_k9_15,
    prs_h.bb9_15          AS h_p_bb9_15,
    prs_h.era_20          AS h_p_era_20,
    prs_h.whip_20         AS h_p_whip_20,
    prs_h.k9_20           AS h_p_k9_20,
    prs_h.bb9_20          AS h_p_bb9_20,
    prs_h.rest_days        AS h_p_rest,
    prs_h.home_era_ytd     AS h_p_home_era_ytd,
    prs_h.road_era_ytd     AS h_p_road_era_ytd,
    prs_h.day_era_ytd      AS h_p_day_era_ytd,
    prs_h.night_era_ytd    AS h_p_night_era_ytd,
    prs_h.is_quality_start AS h_p_quality_start,

    prs_a.era_ytd         AS a_p_era_ytd,
    prs_a.whip_ytd        AS a_p_whip_ytd,
    prs_a.k9_ytd          AS a_p_k9_ytd,
    prs_a.bb9_ytd         AS a_p_bb9_ytd,
    prs_a.kbb_ytd         AS a_p_kbb_ytd,
    prs_a.fip_ytd         AS a_p_fip_ytd,
    prs_a.qs_rate_ytd     AS a_p_qs_rate_ytd,
    prs_a.starts_ytd      AS a_p_starts_ytd,
    prs_a.era_5           AS a_p_era_5,
    prs_a.whip_5          AS a_p_whip_5,
    prs_a.k9_5            AS a_p_k9_5,
    prs_a.bb9_5           AS a_p_bb9_5,
    prs_a.era_10          AS a_p_era_10,
    prs_a.whip_10         AS a_p_whip_10,
    prs_a.k9_10           AS a_p_k9_10,
    prs_a.bb9_10          AS a_p_bb9_10,
    prs_a.era_15          AS a_p_era_15,
    prs_a.whip_15         AS a_p_whip_15,
    prs_a.k9_15           AS a_p_k9_15,
    prs_a.bb9_15          AS a_p_bb9_15,
    prs_a.era_20          AS a_p_era_20,
    prs_a.whip_20         AS a_p_whip_20,
    prs_a.k9_20           AS a_p_k9_20,
    prs_a.bb9_20          AS a_p_bb9_20,
    prs_a.rest_days        AS a_p_rest,
    prs_a.home_era_ytd     AS a_p_home_era_ytd,
    prs_a.road_era_ytd     AS a_p_road_era_ytd,
    prs_a.day_era_ytd      AS a_p_day_era_ytd,
    prs_a.night_era_ytd    AS a_p_night_era_ytd,
    prs_a.is_quality_start AS a_p_quality_start,

    -- Current-game pitcher names (from pitcher_game_stats)
    pgs_h.pitcher_name    AS home_starter_name,
    pgs_a.pitcher_name    AS away_starter_name,

    -- ──────────────────────────────────────────────────────────────────────
    -- BETTING LINES (consolidated)
    -- ──────────────────────────────────────────────────────────────────────
    blc.closing_spread,
    blc.closing_spread_home_odds,
    blc.closing_spread_away_odds,
    blc.closing_ou,
    blc.closing_over_odds,
    blc.closing_under_odds,
    blc.closing_home_ml,
    blc.closing_away_ml,
    blc.closing_home_implied_probability,
    blc.closing_away_implied_probability,
    blc.opening_spread,
    blc.opening_spread_home_odds,
    blc.opening_spread_away_odds,
    blc.opening_ou,
    blc.opening_over_odds,
    blc.opening_under_odds,
    blc.opening_home_ml,
    blc.opening_away_ml,
    blc.opening_home_implied_probability,
    blc.opening_away_implied_probability,
    blc.has_verified_ou,
    (blc.closing_ou - blc.opening_ou) AS ou_movement,
    (blc.closing_home_ml - blc.opening_home_ml) AS ml_movement,

    -- Group 2 betting line aliases for mlb.features
    blc.closing_spread AS spread,
    blc.closing_ou AS over_under,
    blc.closing_ou AS ou_line,
    blc.closing_home_ml AS home_moneyline,
    blc.closing_away_ml AS away_moneyline,
    blc.opening_ou AS opening_total,
    blc.opening_home_implied_probability AS opening_home_implied,
    blc.opening_away_implied_probability AS opening_away_implied

    -- ──────────────────────────────────────────────────────────────────────





FROM mlb.games g
JOIN mlb.seasons s ON s.id = g.season_id
JOIN mlb.teams ht ON ht.id = g.home_team_id
JOIN mlb.teams at ON at.id = g.away_team_id
LEFT JOIN mlb.venues v ON v.id = g.venue_id

-- Cumulative stats (home / away)
LEFT JOIN mlb.cumulative_game_stats cgs_h
    ON cgs_h.game_id = g.id AND cgs_h.team_side = 'home'
LEFT JOIN mlb.cumulative_game_stats cgs_a
    ON cgs_a.game_id = g.id AND cgs_a.team_side = 'away'

-- Team rolling stats (home / away)
LEFT JOIN mlb.team_rolling_stats trs_h
    ON trs_h.game_id = g.id AND trs_h.team_side = 'home'
LEFT JOIN mlb.team_rolling_stats trs_a
    ON trs_a.game_id = g.id AND trs_a.team_side = 'away'

-- Prior season stats (for early-season blending)
LEFT JOIN mlb.prior_team_stats pts_h
    ON pts_h.team_abbr = ht.abbreviation
    AND pts_h.year = s.year - 1
LEFT JOIN mlb.prior_team_stats pts_a
    ON pts_a.team_abbr = at.abbreviation
    AND pts_a.year = s.year - 1

-- Pitcher rolling stats (home / away starters via pitcher_game_stats)
LEFT JOIN mlb.pitcher_game_stats pgs_h
    ON pgs_h.game_id = g.id
    AND pgs_h.team_abbr = ht.abbreviation
    AND pgs_h.is_starter = TRUE
LEFT JOIN mlb.pitcher_game_stats pgs_a
    ON pgs_a.game_id = g.id
    AND pgs_a.team_abbr = at.abbreviation
    AND pgs_a.is_starter = TRUE

-- Pitcher rolling stats tables (home / away starters)
LEFT JOIN mlb.pitcher_rolling_stats prs_h
    ON prs_h.game_id = g.id
    AND prs_h.team_abbr = ht.abbreviation
    AND prs_h.is_starter = TRUE
LEFT JOIN mlb.pitcher_rolling_stats prs_a
    ON prs_a.game_id = g.id
    AND prs_a.team_abbr = at.abbreviation
    AND prs_a.is_starter = TRUE

-- Bullpen game stats (home / away)
LEFT JOIN mlb.bullpen_game_stats bg_h
    ON bg_h.game_id = g.id AND bg_h.team_id = ht.id
LEFT JOIN mlb.bullpen_game_stats bg_a
    ON bg_a.game_id = g.id AND bg_a.team_id = at.id

-- Betting lines
LEFT JOIN mlb.betting_lines_consolidated blc
    ON blc.game_id = g.id 



-- FD season win totals
LEFT JOIN mlb.player_season_props fdp_h
    ON fdp_h.team_id = ht.id
    AND fdp_h.season_year = s.year
    AND fdp_h.prop_type = 'season_win_total'
LEFT JOIN mlb.player_season_props fdp_a
    ON fdp_a.team_id = at.id
    AND fdp_a.season_year = s.year
    AND fdp_a.prop_type = 'season_win_total'


ORDER BY g.date DESC
"""


# ── Known MLB features (mirrors the mlb.features table) ─────────────────────

# This list is the code-side source of truth.  If you add a new feature, add it
# here AND insert a row into mlb.features.  The dictionary maps slug → human-
# readable description for the pick-card layer.

FEATURES_CATALOG: Dict[str, str] = {
    # ── Raw game fields ──
    "game_id": "Internal game ID (mlb.games.id)",
    "game_date": "Date of the game (timestamp with time zone)",
    "season_year": "Calendar year this game belongs to",
    "game_type": "Type of game (Regular Season, Spring Training, etc.)",
    "status": "Game status (FINAL, PREGAME, etc.)",
    "venue": "Venue/ballpark name",
    "roof_type": "Roof type: dome / outdoor / retractable",
    "surface": "Playing surface (grass / turf)",
    "temperature": "Game-time temperature (°F)",
    "wind_speed": "Wind speed (mph)",
    "wind_direction": "Wind direction",
    "weather_condition": "General weather description",
    "day_night": "Day or night game",
    "attendance": "Number of attendees",
    "scheduled_innings": "Scheduled innings (usually 9)",
    "duration_minutes": "Duration of game (minutes)",
    # ── Pre-game records ──
    "home_wins": "Home team wins prior to this game",
    "home_losses": "Home team losses prior to this game",
    "away_wins": "Away team wins prior to this game",
    "away_losses": "Away team losses prior to this game",
    # ── Pitcher identities ──
    "home_pitcher_name": "Home starting pitcher name",
    "away_pitcher_name": "Away starting pitcher name",
    # ── Betting lines ──
    "spread": "Closing run-line spread (negative = favorite giving runs)",
    "home_moneyline": "Closing home moneyline (American odds)",
    "away_moneyline": "Closing away moneyline (American odds)",
    "over_under": "Closing over/under total",
    "opening_total": "Opening over/under total",
    "opening_spread": "Opening run-line spread",
    "opening_home_ml": "Opening home moneyline",
    "opening_away_ml": "Opening away moneyline",
    "has_verified_ou": "Closing OU came from a verified betting source",
    "sportsbook": "Sportsbook that supplied the closing OU line",
    # ── Team info ──
    "ha": "Home team abbreviation",
    "aa": "Away team abbreviation",
    "hdiv": "Home team division",
    "adiv": "Away team division",
    "home_team_id": "Home team internal ID",
    "away_team_id": "Away team internal ID",
    "home_team_name": "Home team full name",
    "away_team_name": "Away team full name",
    "margin": "Actual run differential (home_score - away_score); FINAL only",
    # ── Player IDs (not yet enriched) ──
    "mlb_game_id": "External MLB game ID (from ESPN/MLB.com)",
    # ── Cumulative season-to-date stats ──
    "h_cum_avg": "Home cumulative AVG",
    "a_cum_avg": "Away cumulative AVG",
    "h_cum_obp": "Home cumulative OBP",
    "a_cum_obp": "Away cumulative OBP",
    "h_cum_slg": "Home cumulative SLG",
    "a_cum_slg": "Away cumulative SLG",
    "h_cum_ops": "Home cumulative OPS",
    "a_cum_ops": "Away cumulative OPS",
    "h_cum_babip": "Home cumulative BABIP",
    "a_cum_babip": "Away cumulative BABIP",
    "h_cum_k_rate": "Home cumulative K rate",
    "a_cum_k_rate": "Away cumulative K rate",
    "h_cum_bb_rate": "Home cumulative BB rate",
    "a_cum_bb_rate": "Away cumulative BB rate",
    "h_cum_era": "Home cumulative ERA",
    "a_cum_era": "Away cumulative ERA",
    "h_cum_whip": "Home cumulative WHIP",
    "a_cum_whip": "Away cumulative WHIP",
    "h_cum_k9": "Home cumulative K/9",
    "a_cum_k9": "Away cumulative K/9",
    "h_cum_bb9": "Home cumulative BB/9",
    "a_cum_bb9": "Away cumulative BB/9",

    # -- Game context additions
    "game_number": "Game number in season",
    "venue_id": "Venue ID",
    "season_id": "Season ID",
    "actual_innings": "Actual innings played",
    "scheduled_innings": "Scheduled innings",
    "duration_minutes": "Game duration in minutes",
    "mlb_game_id": "MLB game ID",

    # -- Betting additions
    "closing_over_odds": "Closing over odds",
    "closing_under_odds": "Closing under odds",
    "closing_spread_home_odds": "Closing run line home odds",
    "closing_spread_away_odds": "Closing run line away odds",
    "opening_ou": "Opening total (O/U)",
    "opening_home_implied_probability": "Opening home implied probability",
    "opening_away_implied_probability": "Opening away implied probability",
    "opening_spread": "Opening run line",
    "opening_home_ml": "Opening home ML",
    "opening_away_ml": "Opening away ML",

    # -- Rolling team stats (produced by GAME_QUERY)
    "h_rf5": "Home runs for (rolling avg last 5)",
    "a_rf5": "Away runs for (rolling avg last 5)",
    "h_ra5": "Home runs against (rolling avg last 5)",
    "a_ra5": "Away runs against (rolling avg last 5)",
    "h_rf10": "Home runs for (rolling avg last 10)",
    "a_rf10": "Away runs for (rolling avg last 10)",
    "h_ra10": "Home runs against (rolling avg last 10)",
    "a_ra10": "Away runs against (rolling avg last 10)",
    "h_rf15": "Home runs for (rolling avg last 15)",
    "a_rf15": "Away runs for (rolling avg last 15)",
    "h_ra15": "Home runs against (rolling avg last 15)",
    "a_ra15": "Away runs against (rolling avg last 15)",
    "h_rf20": "Home runs for (rolling avg last 20)",
    "a_rf20": "Away runs for (rolling avg last 20)",
    "h_ra20": "Home runs against (rolling avg last 20)",
    "a_ra20": "Away runs against (rolling avg last 20)",

    # -- Rolling hitting stats (from GAME_QUERY)
    "h_avg_5": "Home AVG (rolling avg last 5)",
    "a_avg_5": "Away AVG (rolling avg last 5)",
    "h_avg_10": "Home AVG (rolling avg last 10)",
    "a_avg_10": "Away AVG (rolling avg last 10)",
    "h_avg_15": "Home AVG (rolling avg last 15)",
    "a_avg_15": "Away AVG (rolling avg last 15)",
    "h_obp_5": "Home OBP (rolling avg last 5)",
    "a_obp_5": "Away OBP (rolling avg last 5)",
    "h_obp_10": "Home OBP (rolling avg last 10)",
    "a_obp_10": "Away OBP (rolling avg last 10)",
    "h_ops_5": "Home OPS (rolling avg last 5)",
    "a_ops_5": "Away OPS (rolling avg last 5)",
    "h_ops_10": "Home OPS (rolling avg last 10)",
    "a_ops_10": "Away OPS (rolling avg last 10)",
    "h_ops_15": "Home OPS (rolling avg last 15)",
    "a_ops_15": "Away OPS (rolling avg last 15)",

    # -- Rolling pitching stats (from GAME_QUERY)
    "h_era_5": "Home ERA (rolling avg last 5)",
    "a_era_5": "Away ERA (rolling avg last 5)",
    "h_era_10": "Home ERA (rolling avg last 10)",
    "a_era_10": "Away ERA (rolling avg last 10)",
    "h_era_15": "Home ERA (rolling avg last 15)",
    "a_era_15": "Away ERA (rolling avg last 15)",
    "h_whip_5": "Home WHIP (rolling avg last 5)",
    "a_whip_5": "Away WHIP (rolling avg last 5)",
    "h_whip_10": "Home WHIP (rolling avg last 10)",
    "a_whip_10": "Away WHIP (rolling avg last 10)",
    "h_whip_15": "Home WHIP (rolling avg last 15)",
    "a_whip_15": "Away WHIP (rolling avg last 15)",
    "h_k9_5": "Home K/9 (rolling avg last 5)",
    "a_k9_5": "Away K/9 (rolling avg last 5)",
    "h_k9_10": "Home K/9 (rolling avg last 10)",
    "a_k9_10": "Away K/9 (rolling avg last 10)",
    "h_bb9_5": "Home BB/9 (rolling avg last 5)",
    "a_bb9_5": "Away BB/9 (rolling avg last 5)",
    "h_bb9_10": "Home BB/9 (rolling avg last 10)",
    "a_bb9_10": "Away BB/9 (rolling avg last 10)",
}

# Features added during featurization (computed by build_features)
# These won't be in the raw query but may appear after feature engineering.

COMPUTED_FEATURES_CATALOG: Dict[str, str] = {
    # ── Situational ──
    "rest_h": "Home team days of rest since last game",
    "rest_a": "Away team days of rest since last game",
    "rest_diff": "Rest differential (rest_h - rest_a); positive = home more rested",
    "rest_h_hours": "Home team hours of rest since last game (time between first pitches)",
    "rest_a_hours": "Away team hours of rest since last game (time between first pitches)",
    "rest_diff_hours": "Rest differential in hours (rest_h_hours - rest_a_hours)",
    "is_div": "1 if both teams are in the same division",
    "month": "Numeric month (1-12) of game_date",
    "is_summer": "1 if month is June, July, or August",
    "is_dome": "1 if roof type is dome or retractable",
    "travel_miles": "Away team estimated travel distance to venue (0 if < 50 miles)",
    "tz_diff": "Time-zone difference in hours between home and away cities",
    # ── Team quality ──
    "is_home_fav": "1 if home team is favored (negative spread)",
    "h_winpct": "Home win percentage entering game (blended with prior-season avg)",
    "a_winpct": "Away win percentage entering game (blended with prior-season avg)",
    "winpct_diff": "Win percentage differential (h_winpct - a_winpct)",
    "winpct_l10_diff": "Last-10-games win% differential (home - away)",
    # ── Team-level run production ──
    "h_home_rf": "Home team avg runs-for at home (expanding mean, shift(1))",
    "a_away_rf": "Away team avg runs-for on the road (expanding mean, shift(1))",
    # ── Implied probabilities ──
    "h_implied": "Home implied win probability from closing moneyline",
    "a_implied": "Away implied win probability from closing moneyline",
    "home_implied_probability": "Same as h_implied",
    "away_implied_probability": "Same as a_implied",
    "implied_total": "Estimated total from home + away implied probabilities",
    "ou_line": "Alias for over_under, used inside modeling code",
    # ── Team hitting stats ──
    "h_ops_l10": "Home OPS over last 10 games",
    "a_ops_l10": "Away OPS over last 10 games",
    "h_ops_l20": "Home OPS over last 20 games",
    "a_ops_l20": "Away OPS over last 20 games",
    "h_slg_l10": "Home slugging pct over last 10 games",
    "a_slg_l10": "Away slugging pct over last 10 games",
    "h_slg_l20": "Home slugging pct over last 20 games",
    "a_slg_l20": "Away slugging pct over last 20 games",
    # ── Pitcher-derived ──
    "h_pitcher_era_l20": "Home pitcher ERA over last 20 appearances",
    "a_pitcher_era_l20": "Away pitcher ERA over last 20 appearances",
    "h_pitcher_era_l5": "Home pitcher ERA over last 5 appearances",
    "a_pitcher_era_l5": "Away pitcher ERA over last 5 appearances",
    "h_pitcher_k9_l20": "Home pitcher K/9 over last 20 appearances",
    "a_pitcher_k9_l20": "Away pitcher K/9 over last 20 appearances",
    "h_pitcher_whip_l20": "Home pitcher WHIP over last 20 appearances",
    "a_pitcher_whip_l20": "Away pitcher WHIP over last 20 appearances",
    "h_pitcher_kbb_l20": "Home pitcher K/BB rate over last 20 appearances",
    "a_pitcher_kbb_l20": "Away pitcher K/BB rate over last 20 appearances",
    "h_pitcher_home_team_l20": "Home pitcher ERA with this team (last 20)",
    "a_pitcher_home_team_l20": "Away pitcher ERA with this team (last 20)",
    # ── Venue-specific & home-split ──
    "a_pitcher_venue_era": "Away pitcher ERA at this venue (expanding mean, shift(1), since 2021)",
    "h_pitcher_home_era": "Home pitcher ERA at home (expanding mean, shift(1), prev + current season)",
    "a_team_venue_winpct": "Away team win pct at this venue (expanding mean, shift(1), prev + current season)",
    # ── Pitcher day/night & rest ──
    "h_pitcher_day_era": "Home pitcher ERA in day games (expanding mean, shift(1))",
    "h_pitcher_night_era": "Home pitcher ERA in night games (expanding mean, shift(1))",
    "a_pitcher_day_era": "Away pitcher ERA in day games (expanding mean, shift(1))",
    "a_pitcher_night_era": "Away pitcher ERA in night games (expanding mean, shift(1))",
    "h_pitcher_day_night_era": "Home pitcher ERA resolved by game time (day_era if day game, night_era if night game)",
    "a_pitcher_day_night_era": "Away pitcher ERA resolved by game time (day_era if day game, night_era if night game)",
    "h_pitcher_rest": "Home pitcher days since last start",
    "a_pitcher_rest": "Away pitcher days since last start",
    "a_pitcher_road_era": "Away pitcher ERA in road starts (expanding mean, shift(1))",
    # ── Bullpen ──
    "h_bullpen_era_l5": "Home bullpen ERA over last 5 appearances",
    "a_bullpen_era_l5": "Away bullpen ERA over last 5 appearances",
    "h_bullpen_ip_l5": "Home bullpen IP over last 5 appearances",
    "a_bullpen_ip_l5": "Away bullpen IP over last 5 appearances",
    # ── Form ──
    "h_form_l10": "Home winning percentage last 10 games (exponential MA, shift(1))",
    "a_form_l10": "Away winning percentage last 10 games (exponential MA, shift(1))",
    "h_pitcher_day_night_era": "Home pitcher ERA resolved by game time (day/night)",
    "a_pitcher_day_night_era": "Away pitcher ERA resolved by game time (day/night)",
    # ── Park & environment ──
    "park_factor": "Estimated venue run multiplier based on rolling historical totals",
    "wind_calculated": "Wind effect: wd * wind_speed where wd=1 for out, -1 for in, 0 otherwise",
    "total_avg_team_r10": "Avg total runs involving this team last 10 games",
    "combo_era_r10": "Combined (home + away) total-team ERA last 10 games",
    "combo_era_r10_diff": "Home minus away component of combo_era_r10",
    # ── Movement ──
    "ou_movement": "Closing OU minus opening OU",
    "ml_implied_movement": "Closing home implied prob minus opening home implied prob",
    "opening_home_implied": "Opening home moneyline as implied probability",
    "opening_away_implied": "Opening away moneyline as implied probability",
    # ── Targets (for analysis only — the model predicts these) ──
    "actual_margin": "Actual run differential (target for ATS model)",
    "actual_total": "Actual total runs (target for OU model)",
    "home_score": "Home team final score",
    "away_score": "Away team final score",
}


# ── Customer-facing display names ──────────────────────────────────────────

# Every feature name in FEATURES_CATALOG / COMPUTED_FEATURES_CATALOG has a
# human-readable label.  Keep this in sync with mlb.features.display_name.

DISPLAY_NAMES: Dict[str, str] = {
    "home_team": "Home Team",
    "away_team": "Away Team",
    "game_date": "Game Date",
    "game_type": "Game Type",
    "season_year": "Season",
    "status": "Status",
    "venue": "Venue",
    "roof_type": "Roof Type",
    "surface": "Surface",
    "temperature": "Temperature",
    "wind_speed": "Wind Speed",
    "wind_direction": "Wind Direction",
    "weather_condition": "Weather",
    "day_night": "Day/Night",
    "scheduled_innings": "Scheduled Innings",
    "attendance": "Attendance",
    "actual_innings": "Actual Innings",
    "duration_minutes": "Duration",
    "home_wins": "Home Wins",
    "home_losses": "Home Losses",
    "away_wins": "Away Wins",
    "away_losses": "Away Losses",
    "home_pitcher_name": "Home Pitcher",
    "away_pitcher_name": "Away Pitcher",
    "spread": "Run Line",
    "home_moneyline": "Home Moneyline",
    "away_moneyline": "Away Moneyline",
    "over_under": "Over/Under",
    "opening_total": "Opening Total",
    "opening_spread": "Opening Spread",
    "opening_home_ml": "Opening Home ML",
    "opening_away_ml": "Opening Away ML",
    "has_verified_ou": "Verified OU",
    "sportsbook": "Sportsbook",
    "ha": "Home Abbreviation",
    "aa": "Away Abbreviation",
    "hdiv": "Home Division",
    "adiv": "Away Division",
    "home_team_id": "Home Team ID",
    "away_team_id": "Away Team ID",
    "home_team_name": "Home Team Name",
    "away_team_name": "Away Team Name",
    "margin": "Margin",
    "mlb_game_id": "MLB Game ID",
    "game_id": "Game ID",
    "rest_h": "Home Rest Days",
    "rest_a": "Away Rest Days",
    "rest_diff": "Rest Differential",
    "is_div": "Same Division",
    "month": "Month",
    "is_summer": "Summer Game",
    "is_dome": "Dome Game",
    "travel_miles": "Travel Miles",
    "tz_diff": "Time Zone Diff",
    "is_home_fav": "Home Favored",
    "h_winpct": "Home Win %",
    "a_winpct": "Away Win %",
    "winpct_diff": "Win % Diff",
    "winpct_l10_diff": "Win % L10 Diff",
    "h_home_rf": "Home Home Runs For",
    "a_away_rf": "Away Away Runs For",
    "pf": "Home Runs Scored",
    "pa": "Home Runs Allowed",
    "home_implied_probability": "Home Implied Prob",
    "away_implied_probability": "Away Implied Prob",
    "implied_total": "Implied Total",
    "h_implied": "Home Implied (Model)",
    "a_implied": "Away Implied (Model)",
    "h_pitcher_home_team_l20": "H. Pitcher Team ERA (L20)",
    "a_pitcher_home_team_l20": "A. Pitcher Team ERA (L20)",
    "h_pitcher_era_l20": "Home Pitcher ERA (L20)",
    "a_pitcher_era_l20": "Away Pitcher ERA (L20)",
    "h_pitcher_k9_l20": "Home Pitcher K/9 (L20)",
    "a_pitcher_k9_l20": "Away Pitcher K/9 (L20)",
    "h_pitcher_whip_l20": "Home Pitcher WHIP (L20)",
    "a_pitcher_whip_l20": "Away Pitcher WHIP (L20)",
    "h_pitcher_kbb_rate_l20": "Home Pitcher K/BB (L20)",
    "a_pitcher_kbb_rate_l20": "Away Pitcher K/BB (L20)",
    "a_pitcher_venue_era": "Away Pitcher Venue ERA",
    "h_pitcher_home_era": "Home Pitcher Home ERA",
    "a_team_venue_winpct": "Away Team Venue Win Pct",
    "a_pitcher_road_era": "Away Pitcher Road ERA",
    "h_pitcher_rest": "Home Pitcher Rest (Days)",
    "a_pitcher_rest": "Away Pitcher Rest (Days)",
    "h_pitcher_day_era": "Home Pitcher Day ERA",
    "h_pitcher_night_era": "Home Pitcher Night ERA",
    "a_pitcher_day_era": "Away Pitcher Day ERA",
    "a_pitcher_night_era": "Away Pitcher Night ERA",
    "park_factor": "Park Factor",
    "total_avg_team_r10": "Team Avg Total (L10)",
    "combo_era_r10": "Combo ERA (L10)",
    "combo_era_r10_diff": "Combo ERA Diff (L10)",
    "h_bullpen_era_l5": "Home Bullpen ERA (L5)",
    "a_bullpen_era_l5": "Away Bullpen ERA (L5)",
    "h_bullpen_ip_l5": "Home Bullpen IP (L5)",
    "a_bullpen_ip_l5": "Away Bullpen IP (L5)",
    "h_form_l10": "Home Form (L10)",
    "a_form_l10": "Away Form (L10)",
    "h_pitcher_day_night_era": "Home Pitcher ERA (Day/Night)",
    "a_pitcher_day_night_era": "Away Pitcher ERA (Day/Night)",
    "h_pitcher_era_l5": "Home Pitcher ERA (L5)",
    "a_pitcher_era_l5": "Away Pitcher ERA (L5)",
    "ou_movement": "OU Movement",
    "ml_implied_movement": "ML Movement (Implied)",
    "opening_home_implied": "Opening Home Implied",
    "opening_away_implied": "Opening Away Implied",
    "home_implied": "Home Implied (Model)",
    "away_implied": "Away Implied (Model)",
    "actual_margin": "Actual Margin",
    "actual_total": "Actual Total",
    "closing_ou": "Closing OU",
    "ou_line": "O/U Line (Model)",
    "home_score": "Home Score",
    "away_score": "Away Score",
}

# ── Feature set definitions (model-specific column lists) ────────────────────

# Each entry groups features by use case so training, backtesting, and inference
# all select from the same stable of columns.


# ── Module-level feature helpers ─────────────────────────────────────────────


def get_model_features(model_type: str, live: bool = False) -> list[str]:
    """Fetch feature names for a model type from mlb.features.

    Args:
        model_type: "ats" or "ou"
        live: If True, queries live_<type> column instead of current_<type>.
    """
    import subprocess
    suffix = "live" if live else "current"
    col = {"ou": f"{suffix}_ou", "ats": f"{suffix}_ats"}.get(model_type.lower())
    if not col:
        raise ValueError(f"Unknown model type: {model_type}. Use 'ou' or 'ats'.")
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", "earl-knows-football-db-1",
             "psql", "-U", "earl", "-d", "earl_knows_football",
             "-t", "-A", "-c",
             f"SELECT name FROM mlb.features WHERE {col} = true ORDER BY name"],
            capture_output=True, text=True, timeout=10
        )
        features = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]
        if not features:
            raise RuntimeError(f"No features found for {model_type} (column {col})")
        return features
    except Exception as e:
        raise RuntimeError(f"Failed to fetch {model_type} features from DB: {e}")


def rolling_mean_safe(series: pd.Series, window: int) -> pd.Series:
    """Expanding mean for early season (first ``window`` games),
    then rolling mean after that, all shift(1) on a per-team basis.

    NOTE: This function is currently unused.  The ``build_features`` function
    uses groupby/transform with lambdas instead.
    """
    expanded = series.expanding(min_periods=1).mean().shift(1)
    rolled = series.rolling(window=window, min_periods=1).mean().shift(1)
    return series  # placeholder — the original was broken (references tg from outer scope)


# ── Feature engineering (consolidated build_features) ────────────────────────


_PARK_HISTORY_CACHE: Optional[pd.DataFrame] = None


def _load_park_history() -> pd.DataFrame:
    """Load all available historical completed MLB games for park factor computation.
    Cached so it only queries once per process.
    """
    global _PARK_HISTORY_CACHE
    if _PARK_HISTORY_CACHE is not None:
        return _PARK_HISTORY_CACHE

    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football",
    )
    engine = create_engine(db_url)
    q = """
        SELECT
            g.id AS game_id,
            g.date AS game_date,
            g.game_type,
            g.venue,
            g.home_score,
            g.away_score
        FROM mlb.games g
        WHERE g.home_score IS NOT NULL
          AND g.away_score IS NOT NULL
          AND g.game_type = 'R'
          AND g.season_id >= 15
        ORDER BY g.date
    """
    _PARK_HISTORY_CACHE = pd.read_sql(q, engine, parse_dates=["game_date"])
    return _PARK_HISTORY_CACHE


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build feature columns from the GAME_QUERY result.

    Unlike the old version (which computed rolling stats in pandas),
    this version simply:
    1. Derives any remaining columns the models expect but aren't in the DB
    2. Blends cumulative/rolling stats with prior-season averages for
       early-season games
    3. Returns the feature DataFrame with the same column names the ML
       models expect

    Parameters
    ----------
    df : pd.DataFrame
        Raw DataFrame from GAME_QUERY.

    Returns
    -------
    pd.DataFrame
        Feature-engineered DataFrame with the same column contract
        as the old build_features().
    """
    if df.empty:
        return df

    result = df.copy()

    # ── 1. Prior-season blend for rolling stats ───────────────────────────
    # Early in the season (first ~15 games), rolling windows may be sparse.
    # Blend with prior-season averages.
    #
    # We use COALESCE: rolling stat where available, otherwise prior-season avg.

    _BLEND_COLS = {
        "h_avg_5": "h_prior_avg",
        "h_avg_10": "h_prior_avg",
        "h_ops_5": "h_prior_ops",
        "h_ops_10": "h_prior_ops",
        "h_era_5": "h_prior_era",
        "h_era_10": "h_prior_era",
        "h_whip_5": "h_prior_whip",
        "h_whip_10": "h_prior_whip",
        "a_avg_5": "a_prior_avg",
        "a_avg_10": "a_prior_avg",
        "a_ops_5": "a_prior_ops",
        "a_ops_10": "a_prior_ops",
        "a_era_5": "a_prior_era",
        "a_era_10": "a_prior_era",
        "a_whip_5": "a_prior_whip",
        "a_whip_10": "a_prior_whip",
    }

    for col, prior_col in _BLEND_COLS.items():
        if col in result.columns and prior_col in result.columns:
            result[col] = result[col].fillna(result[prior_col])

    # ── 2. Cumulative stat blends ─────────────────────────────────────────
    # Same for cumulative stats: fillna with prior-season averages
    _BLEND_CUM = {
        "h_cum_avg": "h_prior_avg",
        "h_cum_ops": "h_prior_ops",
        "h_cum_era": "h_prior_era",
        "h_cum_whip": "h_prior_whip",
        "a_cum_avg": "a_prior_avg",
        "a_cum_ops": "a_prior_ops",
        "a_cum_era": "a_prior_era",
        "a_cum_whip": "a_prior_whip",
    }

    for col, prior_col in _BLEND_CUM.items():
        if col in result.columns and prior_col in result.columns:
            result[col] = result[col].fillna(result[prior_col])

    # ── 3. Cumulative difference from rolling ─────────────────────────────
    # The old build_features computed "cum_avg_vs_l5" type columns which
    # are "last N games average" derived from cumulative. We mostly have
    # these as direct rolling stats now. But for backward compat we can
    # set aliases if needed:
    if "h_avg_10" in result.columns and "h_cum_avg_vs_l10" not in result.columns:
        result["h_cum_avg_vs_l10"] = result["h_avg_10"]
    if "h_avg_5" in result.columns and "h_cum_avg_vs_l5" not in result.columns:
        result["h_cum_avg_vs_l5"] = result["h_avg_5"]
    if "h_ops_10" in result.columns and "h_cum_ops_vs_l10" not in result.columns:
        result["h_cum_ops_vs_l10"] = result["h_ops_10"]
    if "h_ops_5" in result.columns and "h_cum_ops_vs_l5" not in result.columns:
        result["h_cum_ops_vs_l5"] = result["h_ops_5"]
    if "h_era_10" in result.columns and "h_cum_era_vs_l10" not in result.columns:
        result["h_cum_era_vs_l10"] = result["h_era_10"]
    if "h_era_5" in result.columns and "h_cum_era_vs_l5" not in result.columns:
        result["h_cum_era_vs_l5"] = result["h_era_5"]
    if "h_whip_10" in result.columns and "h_cum_whip_vs_l10" not in result.columns:
        result["h_cum_whip_vs_l10"] = result["h_whip_10"]
    if "h_whip_5" in result.columns and "h_cum_whip_vs_l5" not in result.columns:
        result["h_cum_whip_vs_l5"] = result["h_whip_5"]

    # Away versions
    for prefix in ("a",):
        for suffix, stat in [("avg", "avg"), ("ops", "ops"), ("era", "era"), ("whip", "whip")]:
            for window in [5, 10]:
                src = f"{prefix}_{stat}_{window}"
                dst = f"{prefix}_cum_{stat}_vs_l{window}"
                if src in result.columns and dst not in result.columns:
                    result[dst] = result[src]

    # ── 4. Pitcher stat aliases (for backward compat with model features) ─
    _P_ALIASES = {
        "h_p_era_5": "h_pitcher_era_l5",
        "h_p_era_10": "h_pitcher_era_l10",
        "h_p_whip_5": "h_pitcher_whip_l5",
        "h_p_whip_10": "h_pitcher_whip_l10",
        "h_p_k9_5": "h_pitcher_k9_l5",
        "h_p_k9_10": "h_pitcher_k9_l10",
        "h_p_kbb_10": "h_pitcher_kbb_l10",
        "h_p_fip_ytd": "h_pitcher_fip_ytd",
        "h_p_era_ytd": "h_pitcher_era_ytd",
        "h_p_whip_ytd": "h_pitcher_whip_ytd",
        "h_p_k9_ytd": "h_pitcher_k9_ytd",
        "h_p_bb9_ytd": "h_pitcher_bb9_ytd",
        "h_p_qs_rate_ytd": "h_pitcher_qs_rate",
        "a_p_era_5": "a_pitcher_era_l5",
        "a_p_era_10": "a_pitcher_era_l10",
        "a_p_whip_5": "a_pitcher_whip_l5",
        "a_p_whip_10": "a_pitcher_whip_l10",
        "a_p_k9_5": "a_pitcher_k9_l5",
        "a_p_k9_10": "a_pitcher_k9_l10",
        "a_p_kbb_10": "a_pitcher_kbb_l10",
        "a_p_fip_ytd": "a_pitcher_fip_ytd",
        "a_p_era_ytd": "a_pitcher_era_ytd",
        "a_p_whip_ytd": "a_pitcher_whip_ytd",
        "a_p_k9_ytd": "a_pitcher_k9_ytd",
        "a_p_bb9_ytd": "a_pitcher_bb9_ytd",
        "a_p_qs_rate_ytd": "a_pitcher_qs_rate",
    }
    for src, dst in _P_ALIASES.items():
        if src in result.columns and dst not in result.columns:
            result[dst] = result[src]

    # ── 5. Combo features (interactions) ──────────────────────────────────
    # ERA differential
    if "h_era_10" in result.columns and "a_era_10" in result.columns:
        result["era_diff"] = result["h_era_10"] - result["a_era_10"]
    elif "h_p_era_10" in result.columns and "a_p_era_10" in result.columns:
        result["era_diff"] = result["h_p_era_10"] - result["a_p_era_10"]

    # Combined 10-game run totals
    if "h_rf10" in result.columns and "a_rf10" in result.columns:
        result["total_avg_team_r10"] = result["h_rf10"] + result["a_rf10"]

    # ── 6. Backward-compat aliases for handicapping engine ───────────────
    # The engine expects certain column names. Create aliases where needed.

    # Team name/abbrev aliases
    _ALIASES_RENAME = {
        "home_team": "home_team_name",
        "away_team": "away_team_name",
        "home_abbr": "ha",
        "away_abbr": "aa",
        "home_starter_name": "h_starter_name",
        "away_starter_name": "a_starter_name",
        "venue_name": "venue",
        "venue_capacity": "capacity",
        "venue_roof": "roof_type",
    }
    for src, dst in _ALIASES_RENAME.items():
        if src in result.columns and dst not in result.columns:
            result[dst] = result[src]

    # Home/away wins/losses — use season record from team_rolling_stats win_pct
    # If win_pct is available, compute games played from available rolling windows
    if "h_rf10" in result.columns:
        has_10 = result["h_rf10"].notna()
        has_5  = result["h_rf5"].notna() & ~has_10
        rookie = ~has_10 & ~has_5  # very early season
        
        result["home_wins"] = result.get("g.home_wins",
            pd.Series([0]*len(result))).fillna(0).astype(int)
        result["home_losses"] = result.get("g.home_losses",
            pd.Series([0]*len(result))).fillna(0).astype(int)
        result["away_wins"] = result.get("g.away_wins",
            pd.Series([0]*len(result))).fillna(0).astype(int)
        result["away_losses"] = result.get("g.away_losses",
            pd.Series([0]*len(result))).fillna(0).astype(int)
        
        # Fallback: if games table has NULLs, try to use rolling stats
        for side, team_side in [('home', 'home'), ('away', 'away')]:
            w_col = f'{side}_wins'
            l_col = f'{side}_losses'
            if result[w_col].sum() == 0 and result[l_col].sum() == 0:
                # Estimate from cumulative stats: total games from cgs
                # Use 10-game rolling as estimate
                rf_col = f'h_rf10' if side == 'home' else f'a_rf10'
                if rf_col in result.columns:
                    result[w_col] = result[rf_col].notna().astype(int) * 5
                    result[l_col] = result[rf_col].notna().astype(int) * 5

    # Average runs scored/allowed (from cumulative_game_stats or rolling)
    # h_cum_avg is batting avg. We need rf_avg = avg runs per game.
    # Use rolling 10-game avg as proxy for season avg
    for prefix in ("h", "a"):
        rf_col = f"{prefix}_rf10"
        ra_col = f"{prefix}_ra10"
        if rf_col in result.columns:
            result[f"{prefix}_rf_avg"] = result[rf_col]
        if ra_col in result.columns:
            result[f"{prefix}_ra_avg"] = result[ra_col]

    # Rest days — computed in query as h_rest, a_rest (or 0 placeholder)
    if "h_rest" in result.columns:
        result["rest_h"] = result["h_rest"]
        result["rest_a"] = result["a_rest"]
        result["rest_diff"] = result["h_rest"] - result["a_rest"]

    # Division game flag
    if "home_abbr" in result.columns and "away_abbr" in result.columns:
        # Simple AL/NL division check — in MLB same division = teams share first 3 of abbreviation
        # This is a simplification but works for most cases
        _DIVISIONS = {
            "AL East": {"BAL", "BOS", "NYY", "TB", "TOR"},
            "AL Central": {"CWS", "CLE", "DET", "KCR", "MIN"},
            "AL West": {"HOU", "LAA", "OAK", "SEA", "TEX"},
            "NL East": {"ATL", "MIA", "NYM", "PHI", "WSN"},
            "NL Central": {"CHC", "CIN", "MIL", "PIT", "STL"},
            "NL West": {"ARI", "COL", "LAD", "SDP", "SFG"},
        }
        _TEAM_DIV = {}
        for div, teams in _DIVISIONS.items():
            for t in teams:
                _TEAM_DIV[t] = div

        h_abbr = result.get("home_abbr", pd.Series())
        a_abbr = result.get("away_abbr", pd.Series())
        result["is_div"] = [
            1 if _TEAM_DIV.get(h) and _TEAM_DIV.get(a) and _TEAM_DIV[h] == _TEAM_DIV[a]
            else 0
            for h, a in zip(h_abbr, a_abbr)
        ]

    # Day/night game flag
    if "time" in result.columns:
        result["day_night"] = result["time"].apply(
            lambda t: "D" if pd.notna(t) and str(t).startswith("1") else "N"
        )
    elif "date" in result.columns:
        result["day_night"] = "N"  # default night

    # Stadium / park factors
    if "venue_roof" in result.columns:
        result["park_factor"] = 100  # neutral default
        result["home_park_factor"] = 100
        result["away_park_factor"] = 100

    # Park factor from venue (v.park_factor_overall exists in venues table)
    # If not in query, use a lookup based on venue_name

    # Pitcher handiness vs LHP/RHP splits — use rolling stats as proxy
    # h_cum_avg vs same-team splits

    # Combo features for ML models (backward compat)
    if "h_p_k9_5" in result.columns and "h_p_k9_5" not in result.columns:
        result["h_pitcher_k9_l5"] = result["h_p_k9_5"]

    # ── 7. Additional stat aliases for backward compat ───────────────────
    # The engine looks for 'h_era10' but DB has 'h_era_10'
    _STAT_ALIASES = {
        "h_era_10": "h_era_10",
        "a_era_10": "a_era_10",
        "h_whip_10": "h_whip_10",
        "a_whip_10": "a_whip_10",
        "h_k9_10": "h_k9_10",
        "a_k9_10": "a_k9_10",
        "h_avg_10": "h_avg_10",
        "a_avg_10": "a_avg_10",
        "h_ops_10": "h_ops_10",
        "a_ops_10": "a_ops_10",
        "h_era_5": "h_era_5",
        "a_era_5": "a_era_5",
        "h_whip_5": "h_whip_5",
        "a_whip_5": "a_whip_5",
        "h_avg_5": "h_avg_5",
        "a_avg_5": "a_avg_5",
        "h_rf5": "h_rf_5",
        "a_rf5": "a_rf_5",
        "h_ra5": "h_ra_5",
        "a_ra5": "a_ra_5",
    }
    for src, dst in _STAT_ALIASES.items():
        if src in result.columns and dst not in result.columns:
            result[dst] = result[src]

    # ── 8. Group 3 — Win% / Form / Over Freq ──────────────────────────────
    # Season win percentages — prefer team_rolling_stats (pre-computed),
    # fall back to W/L from the games table.
    if "h_win_pct" in result.columns:
        result["h_winpct"] = result["h_win_pct"]
    elif "home_wins" in result.columns and "home_losses" in result.columns:
        denom = (result["home_wins"] + result["home_losses"]).clip(lower=1)
        result["h_winpct"] = result["home_wins"] / denom
    else:
        result["h_winpct"] = 0.5

    if "a_win_pct" in result.columns:
        result["a_winpct"] = result["a_win_pct"]
    elif "away_wins" in result.columns and "away_losses" in result.columns:
        denom = (result["away_wins"] + result["away_losses"]).clip(lower=1)
        result["a_winpct"] = result["away_wins"] / denom
    else:
        result["a_winpct"] = 0.5

    result["winpct_diff"] = result["h_winpct"] - result["a_winpct"]

    # ── Rolling L10 W/L via stacked team-game-log ──────────────────────
    # Build a long-form (stacked) table: one row per (game, team) so we can
    # roll up wins/losses per team over the preceding 10 games.
    if "home_score" in result.columns and "away_score" in result.columns:
        home_wins = (result["home_score"] > result["away_score"]).astype(int)
        away_wins = (result["away_score"] > result["home_score"]).astype(int)

        # Stacked: home-team games
        home_df = result[["game_id", "game_date", "home_team_id", "home_score", "away_score"]].copy()
        home_df.columns = ["game_id", "game_date", "team_id", "rf", "ra"]
        home_df["win"] = home_wins.values
        home_df["loss"] = (1 - home_wins.values)

        # Stacked: away-team games
        away_df = result[["game_id", "game_date", "away_team_id", "away_score", "home_score"]].copy()
        away_df.columns = ["game_id", "game_date", "team_id", "rf", "ra"]
        away_df["win"] = away_wins.values
        away_df["loss"] = (1 - away_wins.values)

        team_games = (
            pd.concat([home_df, away_df], ignore_index=True)
            .sort_values(["team_id", "game_date", "game_id"])
            .reset_index(drop=True)
        )

        # Per-team rolling 10-game W/L, lagged by 1 (exclude current game)
        team_games["wins_l10"] = team_games.groupby("team_id")["win"].transform(
            lambda x: x.shift(1).rolling(10, min_periods=1).sum()
        )
        team_games["losses_l10"] = team_games.groupby("team_id")["loss"].transform(
            lambda x: x.shift(1).rolling(10, min_periods=1).sum()
        )

        # Per-team expanding mean of runs for/against, lagged by 1
        team_games["rf_avg"] = team_games.groupby("team_id")["rf"].transform(
            lambda x: x.shift(1).expanding(min_periods=1).mean()
        )
        team_games["ra_avg"] = team_games.groupby("team_id")["ra"].transform(
            lambda x: x.shift(1).expanding(min_periods=1).mean()
        )

        # Join home-team stats back
        home_l10 = team_games[["game_id", "team_id", "wins_l10", "losses_l10", "rf_avg", "ra_avg"]]\
            .rename(columns={"team_id": "home_team_id", "wins_l10": "h_wins_l10",
                            "losses_l10": "h_losses_l10",
                            "rf_avg": "h_rf_avg", "ra_avg": "h_ra_avg"})
        result = result.merge(home_l10, on=["game_id", "home_team_id"], how="left")

        # Join away-team stats back
        away_l10 = team_games[["game_id", "team_id", "wins_l10", "losses_l10", "rf_avg", "ra_avg"]]\
            .rename(columns={"team_id": "away_team_id", "wins_l10": "a_wins_l10",
                            "losses_l10": "a_losses_l10",
                            "rf_avg": "a_rf_avg", "ra_avg": "a_ra_avg"})
        result = result.merge(away_l10, on=["game_id", "away_team_id"], how="left")

        # Compute L10 win percentages
        denom_h = (result["h_wins_l10"] + result["h_losses_l10"]).clip(lower=1)
        result["h_winpct_l10"] = result["h_wins_l10"] / denom_h

        denom_a = (result["a_wins_l10"] + result["a_losses_l10"]).clip(lower=1)
        result["a_winpct_l10"] = result["a_wins_l10"] / denom_a

        result["winpct_l10_diff"] = result["h_winpct_l10"] - result["a_winpct_l10"]

        # Form = L10 winpct for now (can be refined to EMA later)
        result["h_form_l10"] = result["h_winpct_l10"]
        result["a_form_l10"] = result["a_winpct_l10"]
    else:
        # Fallback if home_score/away_score not available
        result["h_winpct_l10"] = result.get("h_winpct", 0.5)
        result["a_winpct_l10"] = result.get("a_winpct", 0.5)
        result["winpct_l10_diff"] = result.get("winpct_diff", 0.0)
        result["h_form_l10"] = result["h_winpct_l10"]
        result["a_form_l10"] = result["a_winpct_l10"]

    # Over frequency — from team_rolling_stats.over_pct (season-level) / over_pct5 (L5).
    if "h_over_pct" in result.columns and "a_over_pct" in result.columns:
        result["h_over_freq"] = result["h_over_pct"]
        result["a_over_freq"] = result["a_over_pct"]
        # over_freq5 — use L5 rolling over% if available, else fall back to season
        result["h_over_freq5"] = (
            result["h_over_pct5"] if "h_over_pct5" in result.columns else result["h_over_pct"]
        ).fillna(0.5)
        result["a_over_freq5"] = (
            result["a_over_pct5"] if "a_over_pct5" in result.columns else result["a_over_pct"]
        ).fillna(0.5)
    else:
        result["h_over_freq"] = 0.5
        result["a_over_freq"] = 0.5
        result["h_over_freq5"] = 0.5
        result["a_over_freq5"] = 0.5

    # ── 9. Group 4 — Home/Away Split Stats ────────────────────────────────
    # Implied probabilities — directly from the closing moneyline (already in query)
    if "closing_home_implied_probability" in result.columns:
        result["h_implied"] = result["closing_home_implied_probability"]
    else:
        result["h_implied"] = 0.5
    if "closing_away_implied_probability" in result.columns:
        result["a_implied"] = result["closing_away_implied_probability"]
    else:
        result["a_implied"] = 0.5

    # Home/away runs — requires expanding mean from CGS with team_side filter.
    # Not currently available in the query. These will need a new subquery.
    # TODO: Add home/away CGS split for h_home_rf / a_away_rf

    # ── 11. Group 5 — Extended Rolling Team Stats ────────────────────────
    # OPS L10 alias (already in query as h_ops_10 / a_ops_10)
    if "h_ops_10" in result.columns:
        result["h_ops_l10"] = result["h_ops_10"]
    if "a_ops_10" in result.columns:
        result["a_ops_l10"] = result["a_ops_10"]

    # ── 12. Group 6 — Extra Pitcher Stats ────────────────────────────────
    # L20 pitcher windows — prefer real 20-start from PRS, fall back to L15
    for side, prefix in [("h", "h_"), ("a", "a_")]:
        ps = f"{prefix}p_"
        pt = f"{prefix}pitcher_"
        for stat in ["era", "whip", "k9", "bb9"]:
            src20 = f"{ps}{stat}_20"
            src15 = f"{ps}{stat}_15"
            dst20 = f"{pt}{stat}_l20"
            if src20 in result.columns:
                result[dst20] = result[src20]
            elif src15 in result.columns:
                result[dst20] = result[src15]
            else:
                result[dst20] = 0.0
        # kbb_l20 — prefer 20-start, fall back to YTD
        src_kbb20 = f"{ps}kbb_20"
        src_kbb_ytd = f"{ps}kbb_ytd"
        dst_kbb = f"{pt}kbb_l20"
        if src_kbb20 in result.columns:
            result[dst_kbb] = result[src_kbb20]
        elif src_kbb_ytd in result.columns:
            result[dst_kbb] = result[src_kbb_ytd]
        else:
            result[dst_kbb] = 0.0

    # Pitcher rest — from PRS rest_days (days since last start)
    if "h_p_rest" in result.columns:
        result["h_pitcher_rest"] = result["h_p_rest"].fillna(0).astype(int)
    else:
        result["h_pitcher_rest"] = 0
    if "a_p_rest" in result.columns:
        result["a_pitcher_rest"] = result["a_p_rest"].fillna(0).astype(int)
    else:
        result["a_pitcher_rest"] = 0

    # Pitcher split ERA — from PRS split columns
    h_src = [("h_pitcher_home_era", "h_p_home_era_ytd"),
             ("h_pitcher_venue_era", "h_p_home_era_ytd"),  # venue ~ home split proxy
             ("h_pitcher_day_era", "h_p_day_era_ytd"),
             ("a_pitcher_road_era", "a_p_road_era_ytd"),
             ("a_pitcher_night_era", "a_p_night_era_ytd"),
             ("a_pitcher_venue_era", "a_p_road_era_ytd")]  # venue ~ road split proxy
    for dest, src in h_src:
        if src in result.columns:
            result[dest] = result[src].fillna(0)
        else:
            result[dest] = 0.0

    # Day/ERA and Night/ERA for the opposite side (need cross-side data from PRS)
    result["h_pitcher_night_era"] = result.get("h_p_night_era_ytd", result.get("h_pitcher_day_era", 0))
    result["a_pitcher_day_era"] = result.get("a_p_day_era_ytd", result.get("a_pitcher_night_era", 0))

    # Day/Night ERA — use the ERA matching this game's time of day
    # If day_night = 'Day', assign day_era; if 'Night', assign night_era
    day_col = "day_night"
    if day_col in result.columns:
        day_mask = result[day_col].str.lower() == "day"
        result["h_pitcher_day_night_era"] = np.where(
            day_mask, result["h_pitcher_day_era"], result["h_pitcher_night_era"]
        )
        result["a_pitcher_day_night_era"] = np.where(
            day_mask, result["a_pitcher_day_era"], result["a_pitcher_night_era"]
        )
    else:
        result["h_pitcher_day_night_era"] = result.get("h_pitcher_day_era", 0)
        result["a_pitcher_day_night_era"] = result.get("a_pitcher_night_era", 0)

    # ── 10. Derived weather: wind_calculated ──────────────────────────────
    # Wind effect: +speed for out, -speed for in, 0 otherwise
    # This gives the model a numeric signal for park/environment impact.
    if "wind_speed" in result.columns and "wind_direction" in result.columns:
        wind_dir_factor = result["wind_direction"].map({
            "out": 1,
            "in": -1,
        }).fillna(0)
        result["wind_calculated"] = result["wind_speed"].fillna(0) * wind_dir_factor
    else:
        result["wind_calculated"] = 0

    # ── Group 7 — Calendar/Situational features ────────────────────────────────
    if "game_date" in result.columns:
        result["month"] = result["game_date"].dt.month
        result["is_summer"] = result["month"].isin([6, 7, 8]).astype(int)
        result["week_number"] = result["game_date"].dt.isocalendar().week.astype(int)
    else:
        result["month"] = 6
        result["is_summer"] = 1
        result["week_number"] = 0

    # ── Group 8 — Venue features ───────────────────────────────────────────────
    # is_dome: roof_type in (\"Dome\", \"Retractable\")
    roof_col = "roof_type" if "roof_type" in result.columns else "venue_roof"
    if roof_col in result.columns:
        result["is_dome"] = result[roof_col].str.lower().isin([
            "dome", "retractable"
        ]).astype(int)
    else:
        result["is_dome"] = 0

    # ── Group 9 — Line-derived features ────────────────────────────────────────
    # is_home_fav: negative home moneyline means favorite
    if "home_moneyline" in result.columns:
        result["is_home_fav"] = (result["home_moneyline"] < 0).astype(int)
    elif "spread" in result.columns:
        result["is_home_fav"] = (result["spread"] < 0).astype(int)
    else:
        result["is_home_fav"] = 0

    # implied_total: already in SQL as closing_ou/over_under
    if "closing_ou" in result.columns:
        result["implied_total"] = result["closing_ou"]
    elif "over_under" in result.columns:
        result["implied_total"] = result["over_under"]
    else:
        result["implied_total"] = 8.0

    # ml_implied_movement: closing - opening implied probability for home team
    close_implied = "closing_home_implied_probability"
    open_implied = "opening_home_implied" in result.columns and "opening_home_implied" or \
                   ("opening_home_implied_probability" in result.columns and "opening_home_implied_probability" or None)
    open_implied_col = None
    for col in ["opening_home_implied", "opening_home_implied_probability"]:
        if col in result.columns:
            open_implied_col = col
            break
    if close_implied in result.columns and open_implied_col:
        result["ml_implied_movement"] = result[close_implied] - result[open_implied_col]
    else:
        result["ml_implied_movement"] = 0.0

    # home/away_implied_probability alias
    if "closing_home_implied_probability" in result.columns:
        result["home_implied_probability"] = result["closing_home_implied_probability"]
    if "closing_away_implied_probability" in result.columns:
        result["away_implied_probability"] = result["closing_away_implied_probability"]

    # ── Group 10 — Combo ERA (rolling) ─────────────────────────────────────────
    for window in [5, 10]:
        h_key = f"h_era_{window}"
        a_key = f"a_era_{window}"
        combo_key = f"combo_era_r{window}"
        if h_key in result.columns and a_key in result.columns:
            result[combo_key] = (result[h_key] + result[a_key]) / 2.0
        else:
            result[combo_key] = 4.5
    if "h_era_10" in result.columns and "a_era_10" in result.columns and "combo_era_r5" in result.columns:
        result["combo_era_r10_diff"] = result["combo_era_r10"] - result["combo_era_r5"]
    else:
        result["combo_era_r10_diff"] = 0.0
    # h/a_combo_era_r15 = just the individual team's ERA component over L15
    h_key_15 = "h_era_15"
    a_key_15 = "a_era_15"
    if h_key_15 in result.columns:
        result["h_combo_era_r15"] = result[h_key_15]
    else:
        result["h_combo_era_r15"] = 4.5
    if a_key_15 in result.columns:
        result["a_combo_era_r15"] = result[a_key_15]
    else:
        result["a_combo_era_r15"] = 4.5

    # ── Group 11 — Rest hours ──────────────────────────────────────────────────
    if "h_rest" in result.columns:
        result["rest_h_hours"] = result["h_rest"] * 24
    else:
        result["rest_h_hours"] = 0
    if "a_rest" in result.columns:
        result["rest_a_hours"] = result["a_rest"] * 24
    else:
        result["rest_a_hours"] = 0
    if "rest_h_hours" in result.columns and "rest_a_hours" in result.columns:
        result["rest_diff_hours"] = result["rest_h_hours"] - result["rest_a_hours"]
    else:
        result["rest_diff_hours"] = 0

    # ── Season average run aliases ─────────────────────────────────────────────
    # h_rf / a_rf are season averages (per-game), so rf_avg is just an alias
    for src, dst in [("h_rf", "h_rf_avg"), ("a_rf", "a_rf_avg"),
                     ("h_ra", "h_ra_avg"), ("a_ra", "a_ra_avg")]:
        if src in result.columns:
            result[dst] = result[src]

    # ── h_home_rf / a_away_rf (per-game averages from CGS / game count) ──────
    # h_home_rf = home team's avg runs scored when playing at home
    # a_away_rf = away team's avg runs scored when playing on the road
    if "h_cum_runs" in result.columns and "h_home_games" in result.columns:
        safe_g = result["h_home_games"].fillna(0).replace(0, 1)
        result["h_home_rf"] = result["h_cum_runs"].fillna(0) / safe_g
    else:
        result["h_home_rf"] = 0
    if "a_cum_runs" in result.columns and "a_away_games" in result.columns:
        safe_g = result["a_away_games"].fillna(0).replace(0, 1)
        result["a_away_rf"] = result["a_cum_runs"].fillna(0) / safe_g
    else:
        result["a_away_rf"] = 0

    # ── a_team_venue_winpct (comes from SQL subquery, just pass through) ────────
    if "a_team_venue_winpct" not in result.columns:
        result["a_team_venue_winpct"] = 0.5

    # ── ha/aa aliases (team abbreviations) ─────────────────────────────────────
    if "home_abbr" in result.columns:
        result["ha"] = result["home_abbr"]
    if "away_abbr" in result.columns:
        result["aa"] = result["away_abbr"]

    # ── hdiv/adiv (team divisions) ──────────────────────────────────────────────
    if "hdiv" in result.columns:
        result["hdiv"] = result["hdiv"]
    else:
        result["hdiv"] = "Unknown"
    if "adiv" in result.columns:
        result["adiv"] = result["adiv"]
    else:
        result["adiv"] = "Unknown"

    # ── has_verified_ou (fill nulls with False) ────────────────────────────────
    if "has_verified_ou" in result.columns:
        result["has_verified_ou"] = result["has_verified_ou"].fillna(False)
    else:
        result["has_verified_ou"] = False

    # ── travel_miles (away team's travel distance) ───────────────────────────────
    if "ha" in result.columns and "aa" in result.columns:
        h_lats = result["ha"].map(lambda c: TEAM_LOCATIONS.get(c, {}).get("lat", 0))
        h_lons = result["ha"].map(lambda c: TEAM_LOCATIONS.get(c, {}).get("lon", 0))
        a_lats = result["aa"].map(lambda c: TEAM_LOCATIONS.get(c, {}).get("lat", 0))
        a_lons = result["aa"].map(lambda c: TEAM_LOCATIONS.get(c, {}).get("lon", 0))
        import numpy as np
        miles = haversine_miles(a_lats, a_lons, h_lats, h_lons)
        result["travel_miles"] = np.where(miles >= 50, miles, 0)
    else:
        result["travel_miles"] = 0

    # ── tz_diff (home timezone - away timezone in hours) ────────────────────────
    if "ha" in result.columns and "aa" in result.columns:
        h_tz = result["ha"].map(lambda c: TEAM_LOCATIONS.get(c, {}).get("tz", 0))
        a_tz = result["aa"].map(lambda c: TEAM_LOCATIONS.get(c, {}).get("tz", 0))
        result["tz_diff"] = h_tz - a_tz
    else:
        result["tz_diff"] = 0

    # ── Bullpen L5 rolling features ────────────────────────────────────────────
    # Build per-team rolling L5 of bullpen ER and IP outs using grouped rolling
    # on a long-form DataFrame.  shift(1) avoids look-ahead bias.
    bp_ready = all(c in result.columns for c in [
        "h_bullpen_er", "h_bullpen_ip", "a_bullpen_er", "a_bullpen_ip",
        "home_team_id", "away_team_id", "game_id"
    ])
    result["h_bullpen_er_l5"] = 0
    result["h_bullpen_ip_l5"] = 0
    result["a_bullpen_er_l5"] = 0
    result["a_bullpen_ip_l5"] = 0
    
    if bp_ready:
        # Build long-form with side marker: side=0 for home, side=1 for away
        h_bp = result[["game_id", "home_team_id", "h_bullpen_er", "h_bullpen_ip"]].copy()
        h_bp.columns = ["game_id", "team_id", "bp_er", "bp_ip"]
        h_bp["side"] = 0
        a_bp = result[["game_id", "away_team_id", "a_bullpen_er", "a_bullpen_ip"]].copy()
        a_bp.columns = ["game_id", "team_id", "bp_er", "bp_ip"]
        a_bp["side"] = 1
        long_bp = pd.concat([h_bp, a_bp], ignore_index=True)
        long_bp["bp_er"] = long_bp["bp_er"].fillna(0)
        long_bp["bp_ip"] = long_bp["bp_ip"].fillna(0)
        long_bp = long_bp.sort_values(["team_id", "game_id"])
        
        # Rolling L5 per team, shift(1) excludes current game
        long_bp["er_l5"] = (
            long_bp.groupby("team_id")["bp_er"]
            .transform(lambda x: x.shift(1).rolling(5, min_periods=1).sum())
        )
        long_bp["ip_l5"] = (
            long_bp.groupby("team_id")["bp_ip"]
            .transform(lambda x: x.shift(1).rolling(5, min_periods=1).sum())
        )
        
        # Index by (game_id, side) for efficient lookup
        bp_indexed = long_bp.set_index(["game_id", "side"])[["er_l5", "ip_l5"]]
        
        # Map back: home side=0, away side=1
        for side, pfx, l5er, l5ip, side_idx in [
            (0, "h_", "h_bullpen_er_l5", "h_bullpen_ip_l5", 0),
            (1, "a_", "a_bullpen_er_l5", "a_bullpen_ip_l5", 1),
        ]:
            vals_er = result["game_id"].map(
                lambda gid: bp_indexed.loc[(gid, side_idx), "er_l5"]
                if (gid, side_idx) in bp_indexed.index else 0
            )
            vals_ip = result["game_id"].map(
                lambda gid: bp_indexed.loc[(gid, side_idx), "ip_l5"]
                if (gid, side_idx) in bp_indexed.index else 0
            )
            result[l5er] = vals_er
            result[l5ip] = vals_ip
    
    # Convert L5 sums to bullpen ERA rate
    for pfx in ["h_", "a_"]:
        er_l5 = f"{pfx}bullpen_er_l5"
        ip_l5 = f"{pfx}bullpen_ip_l5"
        era = f"{pfx}bullpen_era_l5"
        ip_outs = f"{pfx}bullpen_ip_l5"
        if er_l5 in result.columns:
            safe_ip = result[ip_l5].fillna(0).replace(0, 9)
            result[era] = (9.0 * result[er_l5].fillna(0) / (safe_ip / 3.0)).fillna(4.5)
            result[era] = result[era].clip(lower=0, upper=27)
            result[ip_outs] = result[ip_l5].fillna(0) / 3.0
        else:
            result[era] = 4.5
            result[ip_outs] = 1.5

    # PRIME DIRECTIVE: Every pick card MUST include complete handicapping data.
    # So we keep all the raw columns too for the pick card builder.

    return result


# ── Placeholder: rest of MLBDataLoader class ─────────────────────────────────
# The class methods (load_games, _query, _build_query, get_model_features,
# _save_backtest_prediction, etc.) remain structurally the same.
# Only GAME_QUERY and build_features() are replaced.
#
# Refer to the original data_loader.py for the full class implementation.


class MLBDataLoader:
    """Single source for loading MLB game + line data into pandas.

    The raw data includes everything needed for feature engineering,
    training, inference, and pick-card display.
    """

    def __init__(
        self,
        db_url: str = DEFAULT_DB_URL,
        cache_dir: Optional[Path] = None,
    ):
        self._db_url = db_url
        self._cache_dir = cache_dir or Path.home() / ".cache" / "mlb_data_loader"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ── Public methods ──────────────────────────────────────────────────────

    @staticmethod
    def _auto_munge(name: str) -> str:
        """Turn a snake_case column name into a human label."""
        import re

        # Abbreviations to uppercase after title-casing
        _ABBR_UPPERS = {"Era", "Whip", "Fip", "Obp", "Ops", "Slg", "Babip", "Er", "Bb", "K9", "Bb9", "Rf"}
        # Split the trailing window number from stat names like rf5, era10, ops15
        _NUMBER_TAIL = re.compile(r"(\d+)$")

        # Detect prefix and compute rest
        if name.startswith("h_p_"):
            prefix = "Home Pitcher"
            rest = name[4:]
        elif name.startswith("a_p_"):
            prefix = "Away Pitcher"
            rest = name[4:]
        elif name.startswith("h_"):
            prefix = "Home"
            rest = name[2:]
        elif name.startswith("a_"):
            prefix = "Away"
            rest = name[2:]
        else:
            prefix = ""
            rest = name

        # Replace underscores with spaces
        desc = rest.replace("_", " ")

        # Insert space between a stat abbreviation and trailing window number
        # e.g. rf5 → rf 5, era10 → era 10
        desc = _NUMBER_TAIL.sub(r" \1", desc)

        # Normalize special tokens
        desc = desc.replace("ytd", " YTD ").replace("pct", "%")

        nice = desc.title()

        # Apply abbreviation uppercasing
        for abbr in _ABBR_UPPERS:
            nice = nice.replace(abbr, abbr.upper())
        nice = nice.replace("Ytd", "YTD")
        # Clean up extra spaces from replacements
        nice = " ".join(nice.split())

        # If the prefix matches the start of the rest, omit it
        # e.g. h_home_games → prefix="Home", rest="home_games" → skip prefix
        if prefix and rest.lower().startswith(prefix.lower().split()[0]):
            return nice

        return f"{prefix} {nice}" if prefix else nice

    def _auto_description(self, name: str) -> str:
        return self._auto_munge(name)

    def _auto_display(self, name: str) -> str:
        return self._auto_munge(name)

    def get_features_catalog(self) -> Dict[str, str]:
        """Return the full feature catalog (raw + computed).

        Columns that appear in the GAME_QUERY but lack an explicit
        FEATURES_CATALOG / COMPUTED_FEATURES_CATALOG entry get an
        auto-generated description so nothing comes back blank.
        """
        merged = dict(FEATURES_CATALOG)
        merged.update(COMPUTED_FEATURES_CATALOG)
        # Add auto-generated descriptions for any missing columns
        import re
        seen = set()
        for m in re.finditer(r'\bAS\s+(\w+)', GAME_QUERY):
            col = m.group(1)
            seen.add(col)
            if col not in merged:
                merged[col] = self._auto_description(col)
        # Also capture bare column references (table.col or alias.col without AS)
        # from the SELECT list (everything before ORDER BY)
        select_part = GAME_QUERY.split("ORDER BY")[0] if "ORDER BY" in GAME_QUERY else GAME_QUERY
        for m in re.finditer(r'(?:\w+\.)?(\w+)(?=\s*[,]\s*|\s*$)', select_part):
            col = m.group(1)
            if col and col not in seen and col.isidentifier() and col != col.upper()[:3].lower() and len(col) > 1:
                seen.add(col)
                if col not in merged:
                    merged[col] = self._auto_description(col)
        return merged

    def get_feature_names(self) -> List[str]:
        """Return the list of all known feature names."""
        return list(self.get_features_catalog().keys())

    def get_feature_description(self, name: str) -> Optional[str]:
        """Return the description for a single feature, or None."""
        return self.get_features_catalog().get(name)

    def get_display_name(self, name: str) -> str:
        """Return the customer-facing display name for a feature.

        Falls back to a smart auto-generated label if not in DISPLAY_NAMES.
        """
        return DISPLAY_NAMES.get(name, self._auto_display(name))

    def get_all_with_display(self) -> List[Dict[str, str]]:
        """Return a list of dicts with name, description, display_name for every feature."""
        return [
            {"name": name, "description": desc, "display_name": self.get_display_name(name)}
            for name, desc in self.get_features_catalog().items()
        ]

    # ── Public load methods ─────────────────────────────────────────────────

    def load_games(
        self,
        seasons: Optional[List[int]] = None,
        status: str = "FINAL",
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        game_ids: Optional[List[int]] = None,
    ) -> pd.DataFrame:
        """Load game data as a pandas DataFrame (sync).

        Parameters
        ----------
        seasons :
            List of season years to load (e.g. [2024, 2025]).
            None = all seasons.
        status :
            Game status filter.  Default "FINAL" for historical data.
            Use "PREGAME" for today's games, None for all statuses.
        limit :
            If set, only load this many rows.
        include_upcoming :
            If True, include PREGAME / LIVE games too (for pick-card display).
        game_ids :
            If set, only load games with these DB ids.

        Returns
        -------
        pd.DataFrame
            One row per game, with all columns from GAME_QUERY.
        """
        engine = create_engine(self._db_url)
        try:
            return self._query(engine, seasons=seasons, status=status,
                               limit=limit, include_upcoming=include_upcoming,
                               game_ids=game_ids)
        finally:
            engine.dispose()

    async def load_games_async(
        self,
        engine: AsyncEngine,
        seasons: Optional[List[int]] = None,
        status: str = "FINAL",
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        game_ids: Optional[List[int]] = None,
    ) -> pd.DataFrame:
        """Load game data as a pandas DataFrame (async, using an existing engine)."""
        return await self._query_async(engine, seasons=seasons, status=status,
                                       limit=limit, include_upcoming=include_upcoming,
                                       game_ids=game_ids)

    def load_all_games(
        self,
        seasons: Optional[List[int]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Convenience: load all games regardless of status — for pick cards."""
        return self.load_games(
            seasons=seasons,
            status=None,
            limit=limit,
            include_upcoming=True,
        )

    # ── Internal query methods ──────────────────────────────────────────────

    def _build_query(
        self,
        seasons: Optional[List[int]],
        status: Optional[str],
        limit: Optional[int],
        include_upcoming: bool,
        game_ids: Optional[List[int]] = None,
    ) -> str:
        """Build the SQL query with filters."""
        conditions: List[str] = []

        if seasons:
            placeholders = ", ".join(str(s) for s in seasons)
            conditions.append(f"s.year IN ({placeholders})")

        if status is not None and not include_upcoming:
            conditions.append(f"g.status = '{status}'")
        elif include_upcoming and not game_ids:
            conditions.append("g.status IS NOT NULL")

        if game_ids:
            ids_str = ", ".join(str(i) for i in game_ids)
            conditions.append(f"g.id IN ({ids_str})")

        sql = GAME_QUERY.strip().rstrip(";")

        if conditions:
            sql = sql.replace("ORDER BY g.date DESC",
                              f"WHERE {' AND '.join(conditions)}\nORDER BY g.date DESC")
        if limit:
            sql += f"\nLIMIT {limit}"

        return sql

    def _query(
        self,
        engine: Any,
        seasons: Optional[List[int]],
        status: Optional[str],
        limit: Optional[int],
        include_upcoming: bool,
        game_ids: Optional[List[int]] = None,
    ) -> pd.DataFrame:
        sql = self._build_query(seasons, status, limit, include_upcoming, game_ids=game_ids)
        logger.debug("Executing query:\n%s", sql)
        with engine.connect() as conn:
            df = pd.read_sql(text(sql), conn)
        logger.info("Loaded %d game rows", len(df))
        return df

    async def _query_async(
        self,
        engine: AsyncEngine,
        seasons: Optional[List[int]],
        status: Optional[str],
        limit: Optional[int],
        include_upcoming: bool,
        game_ids: Optional[List[int]] = None,
    ) -> pd.DataFrame:
        sql = self._build_query(seasons, status, limit, include_upcoming, game_ids=game_ids)
        logger.debug("Executing async query:\n%s", sql)
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            rows = result.fetchall()
            cols = result.keys()
        df = pd.DataFrame(rows, columns=cols)
        logger.info("Loaded %d game rows (async)", len(df))
        return df

    # ── Training-run-aware inference data ───────────────────────────────

    def load_inference_data(
        self,
        feature_names: List[str],
        seasons: Optional[List[int]] = None,
        limit: Optional[int] = None,
        build_features_fn=None,
        **build_kwargs,
    ) -> pd.DataFrame:
        """Load and build data for inference using a specific feature set.

        This is the bridge between stored training-run metadata and live
        inference.  You pass the ``feature_names`` from a training run's
        results_json (or a FEATURE_SETS list) and optionally a
        ``build_features_fn`` callback, and this method returns a DataFrame
        whose columns exactly match ``feature_names``.

        Parameters
        ----------
        feature_names :
            Feature column list the model was trained on.  Subset of columns
            that ``build_features_fn`` produces.
        seasons :
            Season years to load.  None = all.
        limit :
            Row limit for the raw data.
        build_features_fn :
            A callable ``fn(df: pd.DataFrame, **kwargs) -> pd.DataFrame`` that
            adds all derived / rolling / pitcher features.  If omitted,
            defaults to the module-level ``build_features()``.
        **build_kwargs :
            Extra keyword arguments forwarded to ``build_features_fn``.

        Returns
        -------
        pd.DataFrame
            DataFrame with only the columns in ``feature_names`` that exist
            in the built data.  Missing columns are filled with NaN and
            logged as a warning.

        Notes
        -----
        The raw query already contains columns named like ``game_id``,
        ``ha``, ``aa``, ``game_date``.  The ``build_features_fn`` adds
        everything else (rolling stats, pitcher metrics, park factors, etc.).
        """
        # 1. Load raw game data
        df = self.load_games(
            seasons=seasons,
            status=None if seasons is None else "FINAL",
            limit=limit,
            include_upcoming=seasons is None or limit is not None,
        )

        # 2. Run feature engineering (defaults to the module-level build_features)
        fn = build_features_fn if build_features_fn is not None else build_features
        df = fn(df, **build_kwargs)

        # 3. Select only the columns the model was trained on
        existing = [c for c in feature_names if c in df.columns]
        missing = [c for c in feature_names if c not in df.columns]
        if missing:
            logger.warning(
                "%d feature(s) not found in built data — filling with NaN: %s",
                len(missing), missing,
            )
            for col in missing:
                df[col] = float("nan")

        return df[feature_names].copy()

    @staticmethod
    def extract_features_from_training_run(
        results_json: Any,
        min_importance: float = 0.0,
    ) -> List[str]:
        """Extract feature names from a training run's results_json.

        Parameters
        ----------
        results_json :
            The parsed ``results_json`` column from ``mlb.training_runs``.
            Expected to be a dict containing ``{"feature_importance": [...]}
            where each entry is ``{"feature": "...", "importance": ...}``
            OR a list of such dicts.
        min_importance :
            Minimum importance threshold to include a feature.
            Use 0.0 to include every feature the model used.

        Returns
        -------
        List of feature name strings (ordered by descending importance).

        Examples
        --------
        >>> row = db.fetchone("SELECT results_json FROM mlb.training_runs ...")
        >>> feats = MLBDataLoader.extract_features_from_training_run(row["results_json"])
        """
        if results_json is None:
            return []

        # ── Step 1: navigate to the feature_importance list ──
        imp_list = []

        # Case A: a dict with a top-level "results" array (training_runs.results_json)
        if isinstance(results_json, dict) and "results" in results_json:
            # Extract from the last result (final trained model, not CV folds)
            for res in reversed(results_json["results"]):
                fi = res.get("feature_importance", [])
                if fi:
                    imp_list = fi
                    break

        # Case B: a flat dict with "feature_importance" key
        elif isinstance(results_json, dict) and "feature_importance" in results_json:
            imp_list = results_json["feature_importance"]

        # Case C: a list of feature dicts directly
        elif isinstance(results_json, list):
            if results_json and isinstance(results_json[0], dict):
                # Check if it looks like feature dicts or results dicts
                if "feature" in results_json[0]:
                    imp_list = results_json
                elif "feature_importance" in results_json[0]:
                    # Last results dict
                    imp_list = results_json[-1].get("feature_importance", [])

        if not imp_list:
            logger.info("No feature_importance found in results_json")
            return []

        # ── Step 2: extract feature names (ordered by importance desc) ──
        raw: List[tuple[float, str]] = []
        for item in imp_list:
            if isinstance(item, dict) and "feature" in item:
                imp = float(item.get("importance", 0.0) or 0.0)
                if imp >= min_importance:
                    raw.append((imp, item["feature"]))

        # Sort descending by importance
        raw.sort(key=lambda x: -x[0])

        # De-duplicate preserving highest-importance occurrence
        seen: set[str] = set()
        ordered: List[str] = []
        for imp, feat in raw:
            if feat not in seen:
                seen.add(feat)
                ordered.append(feat)

        return ordered

    def __repr__(self) -> str:
        return f"MLBDataLoader(db_url={self._db_url!r})"


# ── Singleton / convenience ──────────────────────────────────────────────────

_loader_instance: Optional[MLBDataLoader] = None


def get_data_loader(db_url: str = DEFAULT_DB_URL) -> MLBDataLoader:
    """Return a singleton MLBDataLoader instance."""
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = MLBDataLoader(db_url=db_url)
    return _loader_instance


# ── Quick smoke-test when run directly ───────────────────────────────────────


def _format_catalog(cols: List[str]) -> str:
    """Pretty-print a table of feature names + descriptions."""
    lines = []
    lines.append(f"{'Feature':40s} Description")
    lines.append("-" * 120)
    dl = get_data_loader()
    for c in cols:
        desc = dl.get_feature_description(c) or "(no description registered)"
        lines.append(f"{c:40s} {desc}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser("MLB Data Loader")
    parser.add_argument("--list-features", action="store_true",
                        help="Print all known features and exit")
    parser.add_argument("--seasons", type=str, default=None,
                        help="Comma-separated season years to load")
    parser.add_argument("--limit", type=int, default=None,
                        help="Row limit")
    parser.add_argument("--upcoming", action="store_true",
                        help="Include upcoming/pregame games")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.list_features:
        dl = get_data_loader()
        catalog = dl.get_features_catalog()
        print(f"{'Feature':40s} Description")
        print("-" * 120)
        for name, desc in sorted(catalog.items()):
            print(f"{name:40s} {desc}")
    else:
        seasons = [int(s.strip()) for s in args.seasons.split(",")] if args.seasons else None
        dl = get_data_loader()
        df = dl.load_games(seasons=seasons, limit=args.limit,
                           include_upcoming=args.upcoming)
        print(f"\nDataFrame: {len(df)} rows × {len(df.columns)} cols")
        print(f"Columns: {list(df.columns)}")
        if not df.empty:
            print(f"\nDate range: {df['game_date'].min()} → {df['game_date'].max()}")
            print(f"\nFirst 3 rows:")
            print(df.head(3).to_string())


# ── Cumulative Stats Refresh ───────────────────────────────────────────────

def refresh_cumulative_stats(db_url: str, seasons: list[int] | None = None) -> dict:
    """Populate/update mlb.cumulative_game_stats with pre-computed running totals.

    Incremental — only processes games not yet in the table.
    Safe to call multiple times.

    Parameters
    ----------
    db_url :
        Sync PostgreSQL connection string.
    seasons :
        Season IDs to process.  None = all seasons.

    Returns
    -------
    dict
        Summary of rows inserted.
    """
    from app.handicapping.mlb.cumulative_stats import populate_cumulative_stats

    return populate_cumulative_stats(db_url=db_url, seasons=seasons)
