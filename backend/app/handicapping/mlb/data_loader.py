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
import re
import time
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

# Import sync DB URL from the single source of truth.
# This avoids picking up +asyncpg from DATABASE_URL in .env.
from app.db_urls import PSYCOPG2_DATABASE_URL

DEFAULT_DB_URL = PSYCOPG2_DATABASE_URL


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
    v.park_factor_overall AS venue_park_factor,
    v.capacity         AS venue_capacity,
    v.latitude         AS venue_latitude,
    v.longitude        AS venue_longitude,
    v.timezone         AS venue_timezone,
    

    g.weather_condition AS weather_condition,
    g.wind_speed,
    g.wind_direction,
    g.temperature,
    

    -- Rest days (calendar days since each team's last game).
    -- Computed on LOCAL calendar dates: each game is converted to its OWN venue's
    -- timezone (target uses the target venue, prev game uses the prev game's venue)
    -- BEFORE casting to date. Fixes off-by-one on evening games whose UTC date rolls
    -- into the next day (e.g. a 7:10pm CDT game stored as 00:10 UTC next day was
    -- previously counted as +1 rest day vs. the true local-calendar gap).
    COALESCE(
        ((g.date AT TIME ZONE COALESCE(v.timezone, 'UTC'))::date
         - (h_last_game.h_prev_game_date AT TIME ZONE COALESCE(h_last_game.h_prev_game_tz, 'UTC'))::date)
        , 90) AS h_rest,
    COALESCE(
        ((g.date AT TIME ZONE COALESCE(v.timezone, 'UTC'))::date
         - (a_last_game.a_prev_game_date AT TIME ZONE COALESCE(a_last_game.a_prev_game_tz, 'UTC'))::date)
        , 90) AS a_rest,
    -- Raw UTC timestamps of each team's most recent prior game (used to compute
    -- true elapsed rest hours in build_features; NOT rest-day values themselves).
    h_last_game.h_prev_game_date AS h_prev_game_date,
    a_last_game.a_prev_game_date AS a_prev_game_date,

    -- ──────────────────────────────────────────────────────────────────────
    -- CUMULATIVE STATS (season-to-date entering this game)
    -- From: mlb.cumulative_game_stats
    -- ──────────────────────────────────────────────────────────────────────
    cgs_h.bat_runs         AS h_cum_runs,
    runfg_h.h_home_runs_per_game AS h_home_runs_per_game,
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
    runfg_a.a_away_runs_per_game AS a_away_runs_per_game,
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

    -- Home/away game counts & venue win pct (pre-computed in mlb.team_rolling_stats)
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
    trs_h.rf_avg          AS h_rf_avg,
    trs_h.ra_avg          AS h_ra_avg,
    trs_h.wins            AS h_wins,
    trs_h.losses          AS h_losses,
    trs_h.wins_l10        AS h_wins_l10,
    trs_h.losses_l10      AS h_losses_l10,

    trs_h.bullpen_ip_l5   AS h_bullpen_ip_l5,
    trs_h.bullpen_er_l5   AS h_bullpen_er_l5,

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
    trs_a.rf_avg          AS a_rf_avg,
    trs_a.ra_avg          AS a_ra_avg,
    trs_a.wins            AS a_wins,
    trs_a.losses          AS a_losses,
    trs_a.wins_l10        AS a_wins_l10,
    trs_a.losses_l10      AS a_losses_l10,

    trs_a.bullpen_ip_l5   AS a_bullpen_ip_l5,
    trs_a.bullpen_er_l5   AS a_bullpen_er_l5,

    -- ── Opponent-adjusted season-cumulative metrics (overall; added 2026-08-19) ──
    -- Difference-based runs + multiplicative SOS wins (Rich's chosen defaults).
    -- Direction is SOS-consistent: facing STRONGER opposition underrates a team,
    -- so adjusted values are RAISED when the opponent is above league-average and
    -- lowered when below. trs_h/trs_a both read strictly-prior FINAL rows (leak-free).
    -- League constants from live data: ~4.47 runs/game/team, 0.500 = .500 win pct.
    -- Registered in mlb.features as trainable + pick_card (current/live = FALSE) until
    -- validated, so the running models are unaffected.
    trs_h.rf_avg + (4.47 - trs_a.ra_avg)            AS h_adj_rf_avg,
    trs_h.win_pct * (trs_a.win_pct / 0.500)         AS h_adj_win_pct,
    trs_a.rf_avg + (4.47 - trs_h.ra_avg)            AS a_adj_rf_avg,
    trs_a.win_pct * (trs_h.win_pct / 0.500)         AS a_adj_win_pct,

    -- Lineup OPS (trainable + pick_card; NOT current/live for ats/ou).
    -- lineup_ops = avg of the 9 starters' season ytd_ops (leak-safe, each at
    -- their most recent prior FINAL game). lineup_ops_n = how many resolved.
    -- lineup_ops_minus_team = starter avg OPS minus the team's season OPS.
    lops_h.lops             AS h_lineup_ops,
    lops_h.lops_n           AS h_lineup_ops_n,
    COALESCE(lops_h.lops - cgs_h.cum_ops, 0)   AS h_lineup_ops_minus_team,
    lops_a.lops             AS a_lineup_ops,
    lops_a.lops_n           AS a_lineup_ops_n,
    COALESCE(lops_a.lops - cgs_a.cum_ops, 0)   AS a_lineup_ops_minus_team,

    -- Lineup production totals + share-of-team runs/RBI
    lpop_h.lpr_runs         AS h_lineup_runs,
    lpop_h.lpr_rbi          AS h_lineup_rbi,
    lpop_h.lpr_pct_runs     AS h_lineup_pct_runs,
    lpop_h.lpr_pct_rbi      AS h_lineup_pct_rbi,
    lpop_a.lpr_runs         AS a_lineup_runs,
    lpop_a.lpr_rbi          AS a_lineup_rbi,
    lpop_a.lpr_pct_runs     AS a_lineup_pct_runs,
    lpop_a.lpr_pct_rbi      AS a_lineup_pct_rbi,

    -- Venue-conditional rolling + season splits (home team at home / away team on road)
    -- Rolling (last 10 at that venue): from trs_hv/trs_av LATERALs.
    -- Season (venue-scoped season win pct): from cgs_hv/cgs_av LATERALs.
    trs_hv.venue_rf_r10       AS h_home_rf_r10,
    trs_hv.venue_win_pct_r10  AS h_home_win_pct_r10,
    trs_av.venue_rf_r10       AS a_away_rf_r10,
    trs_av.venue_win_pct_r10  AS a_away_win_pct_r10,
    cgs_hv.venue_win_pct_season AS h_home_win_pct_season,
    cgs_av.venue_win_pct_season AS a_away_win_pct_season,

    -- ──────────────────────────────────────────────────────────────────────
    -- PRIOR SEASON STATS (for early-season blending)
    -- From: mlb.prior_team_stats
    -- ──────────────────────────────────────────────────────────────────────
    pts_h.avg            AS h_prior_avg,
    pts_h.obp            AS h_prior_obp,
    pts_h.slg            AS h_prior_slg,
    pts_h.ops            AS h_prior_ops,
    pts_h.era            AS h_prior_era,
    pts_h.whip           AS h_prior_whip,
    pts_h.rf             AS h_prior_rf,
    pts_h.rf_home        AS h_prior_rf_home,
    pts_a.avg            AS a_prior_avg,
    pts_a.obp            AS a_prior_obp,
    pts_a.slg            AS a_prior_slg,
    pts_a.ops            AS a_prior_ops,
    pts_a.era            AS a_prior_era,
    pts_a.whip           AS a_prior_whip,
    pts_a.rf             AS a_prior_rf,
    pts_a.rf_away        AS a_prior_rf_away,
    prs_h.era_ytd         AS h_p_era_ytd,
    prs_h.whip_ytd        AS h_p_whip_ytd,
    prs_h.k9_ytd          AS h_p_k9_ytd,
    prs_h.bb9_ytd         AS h_p_bb9_ytd,
    prs_h.kbb_ytd         AS h_p_kbb_ytd,
    prs_h.kbb_10          AS h_p_kbb_10,
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
    prs_a.kbb_10          AS a_p_kbb_10,
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

    -- ──────────────────────────────────────────────────────────────────────
    -- PLATOON / L·R FEATURES (team OPS + runs-per-game vs each arm) + SP hand
    -- From: through-date team OPS vs each arm (plato_*) + starter hand + game log
    -- ──────────────────────────────────────────────────────────────────────
    plato_h_lhp.ops       AS h_ops_vs_lhp,
    plato_h_rhp.ops       AS h_ops_vs_rhp,
    plato_a_lhp.ops       AS a_ops_vs_lhp,
    plato_a_rhp.ops       AS a_ops_vs_rhp,
    ROUND(rpgv_h_l.rpg, 2) AS h_rpg_vs_lhp,
    ROUND(rpgv_h_r.rpg, 2) AS h_rpg_vs_rhp,
    ROUND(rpgv_a_l.rpg, 2) AS a_rpg_vs_lhp,
    ROUND(rpgv_a_r.rpg, 2) AS a_rpg_vs_rhp,
    ph_h.h_pitcher_hand    AS h_pitcher_hand,
    ph_a.a_pitcher_hand    AS a_pitcher_hand,

    -- RESOLVED platoon feats: the offense's value vs the OPPOSING starter's arm.
    -- Home offense faces the AWAY pitcher; away offense faces the HOME pitcher.
    -- (Inline source exprs: PG can't reference sibling output aliases in SQL.)
    CASE WHEN ph_a.a_pitcher_hand = 'L' THEN plato_h_lhp.ops ELSE plato_h_rhp.ops END AS h_ops_vs_opp_hand,
    CASE WHEN ph_h.h_pitcher_hand = 'L' THEN plato_a_lhp.ops ELSE plato_a_rhp.ops END AS a_ops_vs_opp_hand,
    CASE WHEN ph_a.a_pitcher_hand = 'L' THEN ROUND(rpgv_h_l.rpg, 2)
         ELSE ROUND(rpgv_h_r.rpg, 2) END AS h_rpg_vs_opp_hand,
    CASE WHEN ph_h.h_pitcher_hand = 'L' THEN ROUND(rpgv_a_l.rpg, 2)
         ELSE ROUND(rpgv_a_r.rpg, 2) END AS a_rpg_vs_opp_hand,

    -- Pitcher VENUE ERA (real, from prior starts at this exact park)
    vph.venue_era_h AS h_pitcher_venue_era,
    vpa.venue_era_a AS a_pitcher_venue_era,
    vph.venue_starts_h AS h_pitcher_venue_starts,
    vpa.venue_starts_a AS a_pitcher_venue_starts,

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
LEFT JOIN mlb.venues v ON v.mlb_venue_id = g.venue_id

-- ──────────────────────────────────────────────────────────────────────
-- EFFECTIVE LINEUP GAME (prior-game fallback) — home & away
-- Resolve the "lineup source game" for each team of g. If g already has a
-- posted lineup for the side, use g itself. Otherwise fall back to the most
-- recent prior FINAL game (>=30min before g, either venue) for that team that
-- has lineups, so SCHEDULED games get a real expected lineup (read from the
-- team's most recent completed game's batting order) instead of 0/NULL until
-- actual lineups post ~2h pregame. Leak-safe: strictly prior, FINAL only.
LEFT JOIN LATERAL (
    -- Home team effective lineup game: g itself if it has home lineups posted,
    -- else the most recent prior FINAL game (either venue) that has lineups.
    SELECT
        CASE WHEN EXISTS (SELECT 1 FROM mlb.lineups x
                          WHERE x.game_id = g.id AND x.team_side = 'home' AND x.batting_order BETWEEN 1 AND 9)
             THEN g.id
             ELSE (
                 SELECT lpg.game_id
                 FROM mlb.lineups lpg
                 JOIN mlb.games gp ON gp.id = lpg.game_id
                 WHERE (gp.home_team_id = g.home_team_id OR gp.away_team_id = g.home_team_id)
                   AND gp.status = 'FINAL'
                   AND gp.date < g.date - INTERVAL '30 minutes'
                   AND gp.id <> g.id
                   AND lpg.batting_order BETWEEN 1 AND 9
                 GROUP BY lpg.game_id, gp.date
                 ORDER BY gp.date DESC, lpg.game_id DESC
                 LIMIT 1
             )
        END AS eff_game_id,
        CASE WHEN EXISTS (SELECT 1 FROM mlb.lineups x
                          WHERE x.game_id = g.id AND x.team_side = 'home' AND x.batting_order BETWEEN 1 AND 9)
             THEN 'home'
             ELSE (
                 SELECT CASE WHEN gp.home_team_id = g.home_team_id THEN 'home' ELSE 'away' END
                 FROM mlb.lineups lpg
                 JOIN mlb.games gp ON gp.id = lpg.game_id
                 WHERE (gp.home_team_id = g.home_team_id OR gp.away_team_id = g.home_team_id)
                   AND gp.status = 'FINAL'
                   AND gp.date < g.date - INTERVAL '30 minutes'
                   AND gp.id <> g.id
                   AND lpg.batting_order BETWEEN 1 AND 9
                 GROUP BY lpg.game_id, gp.date, gp.home_team_id
                 ORDER BY gp.date DESC, lpg.game_id DESC
                 LIMIT 1
             )
        END AS eff_side
) h_lin ON TRUE
LEFT JOIN LATERAL (
    -- Away team effective lineup game: g itself if it has away lineups posted,
    -- else the most recent prior FINAL game (either venue) that has lineups.
    SELECT
        CASE WHEN EXISTS (SELECT 1 FROM mlb.lineups x
                          WHERE x.game_id = g.id AND x.team_side = 'away' AND x.batting_order BETWEEN 1 AND 9)
             THEN g.id
             ELSE (
                 SELECT lpg.game_id
                 FROM mlb.lineups lpg
                 JOIN mlb.games gp ON gp.id = lpg.game_id
                 WHERE (gp.home_team_id = g.away_team_id OR gp.away_team_id = g.away_team_id)
                   AND gp.status = 'FINAL'
                   AND gp.date < g.date - INTERVAL '30 minutes'
                   AND gp.id <> g.id
                   AND lpg.batting_order BETWEEN 1 AND 9
                 GROUP BY lpg.game_id, gp.date
                 ORDER BY gp.date DESC, lpg.game_id DESC
                 LIMIT 1
             )
        END AS eff_game_id,
        CASE WHEN EXISTS (SELECT 1 FROM mlb.lineups x
                          WHERE x.game_id = g.id AND x.team_side = 'away' AND x.batting_order BETWEEN 1 AND 9)
             THEN 'away'
             ELSE (
                 SELECT CASE WHEN gp.home_team_id = g.away_team_id THEN 'home' ELSE 'away' END
                 FROM mlb.lineups lpg
                 JOIN mlb.games gp ON gp.id = lpg.game_id
                 WHERE (gp.home_team_id = g.away_team_id OR gp.away_team_id = g.away_team_id)
                   AND gp.status = 'FINAL'
                   AND gp.date < g.date - INTERVAL '30 minutes'
                   AND gp.id <> g.id
                   AND lpg.batting_order BETWEEN 1 AND 9
                 GROUP BY lpg.game_id, gp.date, gp.home_team_id
                 ORDER BY gp.date DESC, lpg.game_id DESC
                 LIMIT 1
             )
        END AS eff_side
) a_lin ON TRUE

-- ──────────────────────────────────────────────────────────────────────
-- STARTING PITCHER HAND + TEAM L/R OPS (vs RHP/LHP) + RPG vs arm
-- The platoon features: each offense's OPS / runs-per-game against righty
-- and lefty starters, plus the starting pitcher's throwing hand (pick card).
-- ──────────────────────────────────────────────────────────────────────
-- Starting pitcher throwing arm — resolved by the UNAMBIGUOUS pitcher_mlb_id
-- (via pgs) rather than pitcher name, which is ambiguous for the 45 names
-- shared by two real players and was double-counting games. Fall back to name
-- only when pgs has no mlb_id row for the starter.
LEFT JOIN LATERAL (
    SELECT COALESCE(
        (SELECT pl.throws FROM mlb.players pl
          JOIN mlb.pitcher_game_stats P_H ON P_H.pitcher_mlb_id = pl.mlb_id
          WHERE P_H.game_id = g.id AND P_H.pitcher_name = g.home_pitcher_name
            AND P_H.is_starter AND pl.throws IS NOT NULL LIMIT 1),
        (SELECT pl2.throws FROM mlb.players pl2
          WHERE pl2.name = g.home_pitcher_name AND pl2.throws IS NOT NULL LIMIT 1)
    ) AS h_pitcher_hand
) ph_h ON TRUE
LEFT JOIN LATERAL (
    SELECT COALESCE(
        (SELECT pl.throws FROM mlb.players pl
          JOIN mlb.pitcher_game_stats P_A ON P_A.pitcher_mlb_id = pl.mlb_id
          WHERE P_A.game_id = g.id AND P_A.pitcher_name = g.away_pitcher_name
            AND P_A.is_starter AND pl.throws IS NOT NULL LIMIT 1),
        (SELECT pl2.throws FROM mlb.players pl2
          WHERE pl2.name = g.away_pitcher_name AND pl2.throws IS NOT NULL LIMIT 1)
    ) AS a_pitcher_hand
) ph_a ON TRUE
-- Team OPS vs each arm, computed THROUGH-DATE and precomputed cumulatively in
-- mlb.team_ops_vs_arm (owned/refreshed by mlb-stats-refresh). These REPLACE the
-- old per-row platoon LATERALs (and the pre-2026-08 team_splits season aggregates,
-- which leaked games after the target date). Leak-safe here: we read ONLY the
-- PREVIOUS Final row for this (team, side, arm) strictly before the target
-- (g_prev.date < g.date - 30min), so the model sees the through-date OPS entering
-- the target game — never the target's own or later stats. OPS=(H+BB+HBP+TB)/(AB+BB+HBP+SF).
LEFT JOIN LATERAL (
    SELECT tvo.ops_vs_arm AS ops
    FROM mlb.team_ops_vs_arm tvo

    WHERE tvo.team_id = ht.id AND tvo.team_side = 'home' AND tvo.arm = 'L'
      AND tvo.season_id = g.season_id 
      AND tvo.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY tvo.game_timestamp DESC, tvo.game_id DESC
    LIMIT 1
) plato_h_lhp ON TRUE
LEFT JOIN LATERAL (
    SELECT tvo.ops_vs_arm AS ops
    FROM mlb.team_ops_vs_arm tvo

    WHERE tvo.team_id = ht.id AND tvo.team_side = 'home' AND tvo.arm = 'R'
      AND tvo.season_id = g.season_id 
      AND tvo.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY tvo.game_timestamp DESC, tvo.game_id DESC
    LIMIT 1
) plato_h_rhp ON TRUE
LEFT JOIN LATERAL (
    SELECT tvo.ops_vs_arm AS ops
    FROM mlb.team_ops_vs_arm tvo

    WHERE tvo.team_id = at.id AND tvo.team_side = 'away' AND tvo.arm = 'L'
      AND tvo.season_id = g.season_id 
      AND tvo.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY tvo.game_timestamp DESC, tvo.game_id DESC
    LIMIT 1
) plato_a_lhp ON TRUE
LEFT JOIN LATERAL (
    SELECT tvo.ops_vs_arm AS ops
    FROM mlb.team_ops_vs_arm tvo

    WHERE tvo.team_id = at.id AND tvo.team_side = 'away' AND tvo.arm = 'R'
      AND tvo.season_id = g.season_id 
      AND tvo.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY tvo.game_timestamp DESC, tvo.game_id DESC
    LIMIT 1
) plato_a_rhp ON TRUE

-- rpg_vs_arm (runs scored vs opposing starter hand): from team_runs_vs_arm.
-- Same LATERAL shape as plato_* above — reads the PREVIOUS arm-matched Final
-- row, leak-safe. Replaces the old 8 correlated scalar subqueries (~21% of
-- load_games time).
LEFT JOIN LATERAL (
    SELECT tvr.rpg_vs_arm AS rpg
    FROM mlb.team_runs_vs_arm tvr

    WHERE tvr.team_id = ht.id AND tvr.team_side = 'home' AND tvr.arm = 'L'
      AND tvr.season_id = g.season_id 
      AND tvr.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY tvr.game_timestamp DESC, tvr.game_id DESC
    LIMIT 1
) rpgv_h_l ON TRUE
LEFT JOIN LATERAL (
    SELECT tvr.rpg_vs_arm AS rpg
    FROM mlb.team_runs_vs_arm tvr

    WHERE tvr.team_id = ht.id AND tvr.team_side = 'home' AND tvr.arm = 'R'
      AND tvr.season_id = g.season_id 
      AND tvr.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY tvr.game_timestamp DESC, tvr.game_id DESC
    LIMIT 1
) rpgv_h_r ON TRUE
LEFT JOIN LATERAL (
    SELECT tvr.rpg_vs_arm AS rpg
    FROM mlb.team_runs_vs_arm tvr

    WHERE tvr.team_id = at.id AND tvr.team_side = 'away' AND tvr.arm = 'L'
      AND tvr.season_id = g.season_id 
      AND tvr.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY tvr.game_timestamp DESC, tvr.game_id DESC
    LIMIT 1
) rpgv_a_l ON TRUE
LEFT JOIN LATERAL (
    SELECT tvr.rpg_vs_arm AS rpg
    FROM mlb.team_runs_vs_arm tvr

    WHERE tvr.team_id = at.id AND tvr.team_side = 'away' AND tvr.arm = 'R'
      AND tvr.season_id = g.season_id 
      AND tvr.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY tvr.game_timestamp DESC, tvr.game_id DESC
    LIMIT 1
) rpgv_a_r ON TRUE

-- ──────────────────────────────────────────────────────────────────────
-- REST DAYS (find each team's last game before this one)
-- ──────────────────────────────────────────────────────────────────────
LEFT JOIN LATERAL (
    SELECT g_prev.date AS h_prev_game_date,
           v_prev.timezone AS h_prev_game_tz
    FROM mlb.games g_prev
    LEFT JOIN mlb.venues v_prev ON v_prev.mlb_venue_id = g_prev.venue_id
    WHERE (g_prev.home_team_id = g.home_team_id OR g_prev.away_team_id = g.home_team_id)
      AND g_prev.date < g.date - INTERVAL '30 minutes'
      AND g_prev.status = 'FINAL'
    ORDER BY g_prev.date DESC, g_prev.id DESC
    LIMIT 1
) h_last_game ON TRUE
LEFT JOIN LATERAL (
    SELECT g_prev.date AS a_prev_game_date,
           v_prev.timezone AS a_prev_game_tz
    FROM mlb.games g_prev
    LEFT JOIN mlb.venues v_prev ON v_prev.mlb_venue_id = g_prev.venue_id
    WHERE (g_prev.home_team_id = g.away_team_id OR g_prev.away_team_id = g.away_team_id)
      AND g_prev.date < g.date - INTERVAL '30 minutes'
      AND g_prev.status = 'FINAL'
    ORDER BY g_prev.date DESC, g_prev.id DESC
    LIMIT 1
) a_last_game ON TRUE

-- Cumulative stats through PREVIOUS game (home / away) — LATERAL finds the most recent completed game for this team
LEFT JOIN LATERAL (
    SELECT cgs_prev.*
    FROM mlb.cumulative_game_stats cgs_prev
    WHERE cgs_prev.team_id = ht.id
      AND cgs_prev.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY cgs_prev.game_timestamp DESC
    LIMIT 1
) cgs_h ON TRUE
LEFT JOIN LATERAL (
    SELECT cgs_prev.*
    FROM mlb.cumulative_game_stats cgs_prev
    WHERE cgs_prev.team_id = at.id
      AND cgs_prev.game_timestamp < g.date - INTERVAL '30 minutes'
    ORDER BY cgs_prev.game_timestamp DESC
    LIMIT 1
) cgs_a ON TRUE

-- Home/away runs-per-game computed DIRECTLY from box scores (side-correct & leak-safe).
-- h_home_rf = HOME TEAM'S OWN runs scored when playing AT HOME = AVG(gg.home_score) over
--   its FINAL home games this season (gg.home_score is the home team's own score when
--   gg.home_team_id = ht.id).
-- a_away_rf = AWAY TEAM'S OWN runs scored when playing AWAY = AVG(gg.away_score) over its
--   FINAL away games this season (gg.away_score is the away team's own score when
--   gg.away_team_id = at.id).
-- Only FINAL games strictly before the target are counted (30-min safety margin), so
-- these are true through-previous-game averages — no CGS shared-season-total muddle.
-- NOTE (2026-08-19): previously these used AVG(away_score)/AVG(home_score) respectively,
-- which measured OPPONENT runs (runs-allowed) — fixed to own runs-for.
LEFT JOIN LATERAL (
    SELECT ROUND(AVG(gg.home_score)::numeric, 4) AS h_home_runs_per_game
    FROM mlb.games gg
    WHERE gg.home_team_id   = ht.id
      AND gg.season_id      = g.season_id
      AND gg.status         = 'FINAL'
      AND gg.date           < g.date - INTERVAL '30 minutes'
) runfg_h ON TRUE
LEFT JOIN LATERAL (
    SELECT ROUND(AVG(gg.away_score)::numeric, 4) AS a_away_runs_per_game
    FROM mlb.games gg
    WHERE gg.away_team_id   = at.id
      AND gg.season_id      = g.season_id
      AND gg.status         = 'FINAL'
      AND gg.date           < g.date - INTERVAL '30 minutes'
) runfg_a ON TRUE

-- Team rolling stats — LATERAL finds most recent completed game for this team (venue-agnostic).
-- NOTE: team_rolling_stats has ONE row per (team, game) with team_side == that game's venue.
-- Filtering by team_side here would skip a team's recent games at the OTHER venue and read a
-- stale row (e.g. an away bullpen_ip_l5 over last-5-AWAY games instead of last-5-ACTUAL games).
-- Workload/form stats fed by trs_* are venue-agnostic by design (see commit f09e5e6 ed1989d 90ab40f).
LEFT JOIN LATERAL (
    SELECT trs.*
    FROM mlb.team_rolling_stats trs

    WHERE trs.team_id = g.home_team_id
      AND trs.game_date < g.date - INTERVAL '30 minutes'
      
    ORDER BY trs.game_date DESC, trs.game_id DESC
    LIMIT 1
) trs_h ON TRUE
LEFT JOIN LATERAL (
    SELECT trs.*
    FROM mlb.team_rolling_stats trs

    WHERE trs.team_id = g.away_team_id
      AND trs.game_date < g.date - INTERVAL '30 minutes'
      
    ORDER BY trs.game_date DESC, trs.game_id DESC
    LIMIT 1
) trs_a ON TRUE

-- Venue-conditional reads: the most recent FINAL row for each team AT this
-- target game's venue. trs_hv = home team's last HOME game (team_side='home');
-- trs_av = away team's last ROAD game (team_side='away'). These feed the
-- venue-scoped rolling/cumulative split features (h_home_* / a_away_*),
-- which only exist in the row matching the team_side. Added separately from
-- trs_h/trs_a so the venue-agnostic workload/main stats reads are untouched.
LEFT JOIN LATERAL (
    SELECT trs.venue_rf_r10, trs.venue_win_pct_r10
    FROM mlb.team_rolling_stats trs

    WHERE trs.team_id = g.home_team_id
      AND trs.team_side = 'home'
      AND trs.game_date < g.date - INTERVAL '30 minutes'
      
    ORDER BY trs.game_date DESC, trs.game_id DESC
    LIMIT 1
) trs_hv ON TRUE
LEFT JOIN LATERAL (
    SELECT trs.venue_rf_r10, trs.venue_win_pct_r10
    FROM mlb.team_rolling_stats trs

    WHERE trs.team_id = g.away_team_id
      AND trs.team_side = 'away'
      AND trs.game_date < g.date - INTERVAL '30 minutes'
      
    ORDER BY trs.game_date DESC, trs.game_id DESC
    LIMIT 1
) trs_av ON TRUE

-- Venue-scoped season win pct from cumulative (expanding, through that row's
-- venue). cgs_hv = home team's home win pct season-to-date; cgs_av = away team's
-- road win pct season-to-date. Read leak-safe (previous FINAL, capped 30min).
LEFT JOIN LATERAL (
    SELECT cgs.venue_win_pct_season
    FROM mlb.cumulative_game_stats cgs

    WHERE cgs.team_id = g.home_team_id
      AND cgs.team_side = 'home'
      AND cgs.game_timestamp < g.date - INTERVAL '30 minutes'
      
    ORDER BY cgs.game_timestamp DESC, cgs.game_id DESC
    LIMIT 1
) cgs_hv ON TRUE
LEFT JOIN LATERAL (
    SELECT cgs.venue_win_pct_season
    FROM mlb.cumulative_game_stats cgs

    WHERE cgs.team_id = g.away_team_id
      AND cgs.team_side = 'away'
      AND cgs.game_timestamp < g.date - INTERVAL '30 minutes'
      
    ORDER BY cgs.game_timestamp DESC, cgs.game_id DESC
    LIMIT 1
) cgs_av ON TRUE

-- Lineup OPS (per-hitter season ytd_ops as of each starter's most recent FINAL
-- game strictly before the target). Resolves the 9 starters from mlb.lineups
-- for THIS game, reads each one's player_batting_rolling_stats.ytd_ops at their
-- latest prior FINAL row (leak-safe, capped 30min), averages them, and counts
-- how many of the 9 resolved. lineup_ops_minus_team is computed in the outer
-- select vs trs_h.ops/trs_a.ops (which are already team season OPS, leak-safe).
LEFT JOIN LATERAL (
    SELECT AVG(plr.ytd_ops)                       AS lops,
           COUNT(plr.ytd_ops)                     AS lops_n
    FROM mlb.lineups lu
    CROSS JOIN LATERAL (
        SELECT COALESCE(pp.ops, fb.ops) AS ytd_ops
        FROM (SELECT 1) _d
        LEFT JOIN LATERAL (
            SELECT prv.ytd_ops AS ops
            FROM mlb.player_batting_rolling_stats prb2
            JOIN mlb.player_batting_rolling_stats prv
              ON prv.player_id = prb2.player_id AND prv.game_id = prb2.prev_game_id
            WHERE prb2.player_id = lu.player_id AND prb2.game_id = lu.game_id
              AND prv.game_date < g.date - INTERVAL '30 minutes'
        ) pp ON TRUE
        LEFT JOIN LATERAL (
            SELECT prv.ytd_ops AS ops
            FROM mlb.player_batting_rolling_stats prv

            WHERE pp.ops IS NULL
              AND prv.player_id = lu.player_id
              AND prv.game_date < g.date - INTERVAL '30 minutes'
              
            ORDER BY prv.game_date DESC, prv.game_id DESC
            LIMIT 1
        ) fb ON TRUE
    ) plr
    WHERE lu.game_id = h_lin.eff_game_id
      AND lu.team_side = h_lin.eff_side
      AND lu.batting_order BETWEEN 1 AND 9
) lops_h ON TRUE
LEFT JOIN LATERAL (
    SELECT AVG(plr.ytd_ops)                       AS lops,
           COUNT(plr.ytd_ops)                     AS lops_n
    FROM mlb.lineups lu
    CROSS JOIN LATERAL (
        SELECT COALESCE(pp.ops, fb.ops) AS ytd_ops
        FROM (SELECT 1) _d
        LEFT JOIN LATERAL (
            SELECT prv.ytd_ops AS ops
            FROM mlb.player_batting_rolling_stats prb2
            JOIN mlb.player_batting_rolling_stats prv
              ON prv.player_id = prb2.player_id AND prv.game_id = prb2.prev_game_id
            WHERE prb2.player_id = lu.player_id AND prb2.game_id = lu.game_id
              AND prv.game_date < g.date - INTERVAL '30 minutes'
        ) pp ON TRUE
        LEFT JOIN LATERAL (
            SELECT prv.ytd_ops AS ops
            FROM mlb.player_batting_rolling_stats prv

            WHERE pp.ops IS NULL
              AND prv.player_id = lu.player_id
              AND prv.game_date < g.date - INTERVAL '30 minutes'
              
            ORDER BY prv.game_date DESC, prv.game_id DESC
            LIMIT 1
        ) fb ON TRUE
    ) plr
    WHERE lu.game_id = a_lin.eff_game_id
      AND lu.team_side = a_lin.eff_side
      AND lu.batting_order BETWEEN 1 AND 9
) lops_a ON TRUE

-- Lineup production (RBI / runs) + share-of-team. For each side, sums the 9
-- starters' season runs + RBI from the pre-computed mlb.player_batting_rolling_stats
-- (each starter's ytd_runs/ytd_rbi at their most recent FINAL game strictly before
-- the target — leak-safe, capped 30min, identical framing to lops_*), plus the team's
-- OWN season runs+RBI (sum of every batter on that team's ytd_runs/ytd_rbi).
-- pct_runs = lineup_runs / team_runs, pct_rbi = lineup_rbi / team_rbi. Reads
-- pre-computed columns — NO query-time re-summation of batting_game_stats.
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(plr.runs), 0) AS lpr_runs,
           COALESCE(SUM(plr.rbi), 0)  AS lpr_rbi,
           COALESCE((SUM(plr.runs)::numeric) / NULLIF(MAX(team_tot.floor_runs), 0), 0) AS lpr_pct_runs,
           COALESCE((SUM(plr.rbi)::numeric) / NULLIF(MAX(team_tot.floor_rbi), 0), 0)   AS lpr_pct_rbi
    FROM mlb.lineups lu
    JOIN LATERAL (
        SELECT COALESCE(pp.runs, fb.runs) AS runs,
               COALESCE(pp.rbi, fb.rbi) AS rbi
        FROM (SELECT 1) _d
        LEFT JOIN LATERAL (
            SELECT prv.ytd_runs AS runs, prv.ytd_rbi AS rbi
            FROM mlb.player_batting_rolling_stats prb2
            JOIN mlb.player_batting_rolling_stats prv
              ON prv.player_id = prb2.player_id AND prv.game_id = prb2.prev_game_id
            WHERE prb2.player_id = lu.player_id AND prb2.game_id = lu.game_id
              AND prv.game_date < g.date - INTERVAL '30 minutes'
        ) pp ON TRUE
        LEFT JOIN LATERAL (
            SELECT prv.ytd_runs AS runs, prv.ytd_rbi AS rbi
            FROM mlb.player_batting_rolling_stats prv

            WHERE pp.runs IS NULL AND pp.rbi IS NULL
              AND prv.player_id = lu.player_id
              AND prv.game_date < g.date - INTERVAL '30 minutes'
              
            ORDER BY prv.game_date DESC, prv.game_id DESC
            LIMIT 1
        ) fb ON TRUE
    ) plr ON TRUE
    CROSS JOIN LATERAL (
        SELECT cg.bat_runs AS floor_runs,
               cg.bat_rbi  AS floor_rbi
        FROM mlb.cumulative_game_stats cg
        WHERE cg.team_id = g.home_team_id
          AND cg.game_timestamp < g.date - INTERVAL '30 minutes'
        ORDER BY cg.game_timestamp DESC, cg.game_id DESC
        LIMIT 1
     ) team_tot
    WHERE lu.game_id = h_lin.eff_game_id
      AND lu.team_side = h_lin.eff_side
      AND lu.batting_order BETWEEN 1 AND 9
) lpop_h ON TRUE
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(plr.runs), 0) AS lpr_runs,
           COALESCE(SUM(plr.rbi), 0)  AS lpr_rbi,
           COALESCE((SUM(plr.runs)::numeric) / NULLIF(MAX(team_tot.floor_runs), 0), 0) AS lpr_pct_runs,
           COALESCE((SUM(plr.rbi)::numeric) / NULLIF(MAX(team_tot.floor_rbi), 0), 0)   AS lpr_pct_rbi
    FROM mlb.lineups lu
    JOIN LATERAL (
        SELECT COALESCE(pp.runs, fb.runs) AS runs,
               COALESCE(pp.rbi, fb.rbi) AS rbi
        FROM (SELECT 1) _d
        LEFT JOIN LATERAL (
            SELECT prv.ytd_runs AS runs, prv.ytd_rbi AS rbi
            FROM mlb.player_batting_rolling_stats prb2
            JOIN mlb.player_batting_rolling_stats prv
              ON prv.player_id = prb2.player_id AND prv.game_id = prb2.prev_game_id
            WHERE prb2.player_id = lu.player_id AND prb2.game_id = lu.game_id
              AND prv.game_date < g.date - INTERVAL '30 minutes'
        ) pp ON TRUE
        LEFT JOIN LATERAL (
            SELECT prv.ytd_runs AS runs, prv.ytd_rbi AS rbi
            FROM mlb.player_batting_rolling_stats prv

            WHERE pp.runs IS NULL AND pp.rbi IS NULL
              AND prv.player_id = lu.player_id
              AND prv.game_date < g.date - INTERVAL '30 minutes'
              
            ORDER BY prv.game_date DESC, prv.game_id DESC
            LIMIT 1
        ) fb ON TRUE
    ) plr ON TRUE
    CROSS JOIN LATERAL (
        SELECT cg.bat_runs AS floor_runs,
               cg.bat_rbi  AS floor_rbi
        FROM mlb.cumulative_game_stats cg
        WHERE cg.team_id = g.away_team_id
          AND cg.game_timestamp < g.date - INTERVAL '30 minutes'
        ORDER BY cg.game_timestamp DESC, cg.game_id DESC
        LIMIT 1
     ) team_tot
    WHERE lu.game_id = a_lin.eff_game_id
      AND lu.team_side = a_lin.eff_side
      AND lu.batting_order BETWEEN 1 AND 9
) lpop_a ON TRUE

-- Prior season stats (for early-season blending)
LEFT JOIN mlb.prior_team_stats pts_h
    ON pts_h.team_abbr = ht.abbreviation
    AND pts_h.year = s.year - 1
LEFT JOIN mlb.prior_team_stats pts_a
    ON pts_a.team_abbr = at.abbreviation
    AND pts_a.year = s.year - 1

-- Pitcher game stats (home / away starter: the CURRENT game's pitcher, from their own most recent completed start)
LEFT JOIN LATERAL (
    SELECT pgs.*
    FROM mlb.pitcher_game_stats pgs

    WHERE pgs.pitcher_name = g.home_pitcher_name
      AND pgs.is_starter = TRUE
      AND pgs.game_timestamp < g.date - INTERVAL '30 minutes'
      
    ORDER BY pgs.game_timestamp DESC, pgs.game_id DESC
    LIMIT 1
) pgs_h ON TRUE
LEFT JOIN LATERAL (
    SELECT pgs.*
    FROM mlb.pitcher_game_stats pgs

    WHERE pgs.pitcher_name = g.away_pitcher_name
      AND pgs.is_starter = TRUE
      AND pgs.game_timestamp < g.date - INTERVAL '30 minutes'
      
    ORDER BY pgs.game_timestamp DESC, pgs.game_id DESC
    LIMIT 1
) pgs_a ON TRUE

-- Pitcher rolling stats (home / away: cumulative stats for the CURRENT game's pitcher, from that pitcher's most recent completed start)
LEFT JOIN LATERAL (
    SELECT prs.game_id, prs.player_id, prs.team_id, prs.team_abbr, prs.season_id,
           prs.game_date, prs.is_starter, prs.ip_outs, prs.er, prs.hits_allowed,
           prs.walks_allowed, prs.strikeouts, prs.home_runs_allowed,
           prs.era_this_start, prs.whip_this_start, prs.k9_this_start, prs.bb9_this_start,
           prs.is_quality_start, prs.era_ytd, prs.whip_ytd, prs.k9_ytd, prs.bb9_ytd,
           prs.kbb_ytd, prs.fip_ytd, prs.qs_rate_ytd, prs.starts_ytd,
           prs.era_5, prs.whip_5, prs.k9_5, prs.bb9_5, prs.kbb_5,
           prs.era_10, prs.whip_10, prs.k9_10, prs.bb9_10, prs.kbb_10,
           prs.era_15, prs.whip_15, prs.k9_15, prs.bb9_15,
           prs.era_20, prs.whip_20, prs.k9_20, prs.bb9_20, prs.kbb_20,
           prs.home_era_ytd, prs.road_era_ytd, prs.day_era_ytd, prs.night_era_ytd,
           -- Rest = days from THIS pitcher's most recent completed start to the
           -- TARGET game date (NOT the stored per-start rest_days, which is the
           -- gap to the pitcher's PRIOR start and is wrong for a scheduled game).
           EXTRACT(DAY FROM (g.date - prs.game_date))::int AS rest_days
    FROM mlb.pitcher_rolling_stats prs

    WHERE prs.player_id = pgs_h.pitcher_mlb_id
      AND prs.is_starter = TRUE
      AND prs.game_date < g.date - INTERVAL '30 minutes'
      
    ORDER BY prs.game_date DESC, prs.game_id DESC
    LIMIT 1
) prs_h ON TRUE
LEFT JOIN LATERAL (
    SELECT prs.game_id, prs.player_id, prs.team_id, prs.team_abbr, prs.season_id,
           prs.game_date, prs.is_starter, prs.ip_outs, prs.er, prs.hits_allowed,
           prs.walks_allowed, prs.strikeouts, prs.home_runs_allowed,
           prs.era_this_start, prs.whip_this_start, prs.k9_this_start, prs.bb9_this_start,
           prs.is_quality_start, prs.era_ytd, prs.whip_ytd, prs.k9_ytd, prs.bb9_ytd,
           prs.kbb_ytd, prs.fip_ytd, prs.qs_rate_ytd, prs.starts_ytd,
           prs.era_5, prs.whip_5, prs.k9_5, prs.bb9_5, prs.kbb_5,
           prs.era_10, prs.whip_10, prs.k9_10, prs.bb9_10, prs.kbb_10,
           prs.era_15, prs.whip_15, prs.k9_15, prs.bb9_15,
           prs.era_20, prs.whip_20, prs.k9_20, prs.bb9_20, prs.kbb_20,
           prs.home_era_ytd, prs.road_era_ytd, prs.day_era_ytd, prs.night_era_ytd,
           EXTRACT(DAY FROM (g.date - prs.game_date))::int AS rest_days
    FROM mlb.pitcher_rolling_stats prs

    WHERE prs.player_id = pgs_a.pitcher_mlb_id
      AND prs.is_starter = TRUE
      AND prs.game_date < g.date - INTERVAL '30 minutes'
      
    ORDER BY prs.game_date DESC, prs.game_id DESC
    LIMIT 1
) prs_a ON TRUE

-- Pitcher venue ERA (home / away): the CURRENT game's pitcher's cumulative ERA
-- at THIS exact venue, from all prior starts at this park (prior seasons + earlier
-- this season). No fallback: NULL when the pitcher has no starts at this venue.
LEFT JOIN LATERAL (
    SELECT CASE WHEN sum(cpgs.ip) > 0 THEN 9.0 * sum(cpgs.er) / sum(cpgs.ip) END AS venue_era_h,
           sum(cpgs.ip)  AS venue_ip_h,  -- diagnostic, keep for transparency
           count(*)      AS venue_starts_h
    FROM mlb.pitcher_game_stats cpgs
    JOIN mlb.games gpv ON gpv.id = cpgs.game_id
    WHERE cpgs.pitcher_mlb_id = pgs_h.pitcher_mlb_id
      AND cpgs.is_starter = TRUE
      AND gpv.venue_id = g.venue_id
      AND gpv.date < g.date - INTERVAL '30 minutes'
      AND gpv.status = 'FINAL'
) vph ON TRUE
LEFT JOIN LATERAL (
    SELECT CASE WHEN sum(cpgs.ip) > 0 THEN 9.0 * sum(cpgs.er) / sum(cpgs.ip) END AS venue_era_a,
           sum(cpgs.ip)  AS venue_ip_a,  -- diagnostic
           count(*)      AS venue_starts_a
    FROM mlb.pitcher_game_stats cpgs
    JOIN mlb.games gpv ON gpv.id = cpgs.game_id
    WHERE cpgs.pitcher_mlb_id = pgs_a.pitcher_mlb_id
      AND cpgs.is_starter = TRUE
      AND gpv.venue_id = g.venue_id
      AND gpv.date < g.date - INTERVAL '30 minutes'
      AND gpv.status = 'FINAL'
) vpa ON TRUE

-- Bullpen game stats (home / away)
-- Bullpen stats from the MOST RECENT completed game (no lookahead)
LEFT JOIN LATERAL (
    SELECT bg.bullpen_er, bg.bullpen_ip_outs, bg.num_pitchers
    FROM mlb.bullpen_game_stats bg

    WHERE bg.team_id = g.home_team_id
      AND bg.game_timestamp < g.date - INTERVAL '30 minutes'
      
    ORDER BY bg.game_timestamp DESC, bg.game_id DESC
    LIMIT 1
) bg_h ON TRUE
LEFT JOIN LATERAL (
    SELECT bg.bullpen_er, bg.bullpen_ip_outs, bg.num_pitchers
    FROM mlb.bullpen_game_stats bg

    WHERE bg.team_id = g.away_team_id
      AND bg.game_timestamp < g.date - INTERVAL '30 minutes'
      
    ORDER BY bg.game_timestamp DESC, bg.game_id DESC
    LIMIT 1
) bg_a ON TRUE

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


# Features added during featurization (computed by build_features)
# These won't be in the raw query but may appear after feature engineering.


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
    try:
        # Direct DB connection (works on any box) — do NOT shell out to docker.
        from app.db_urls import PSYCOPG2_DATABASE_URL as _FEAT_URL
        engine = create_engine(os.getenv("DATABASE_URL", _FEAT_URL))
        with engine.connect() as conn:
            rows = conn.execute(text(
                f"SELECT name FROM mlb.features WHERE {col} = true ORDER BY name"
            )).fetchall()
        engine.dispose()
        features = [r[0] for r in rows]
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

    from app.db_urls import PSYCOPG2_DATABASE_URL as _FALLBACK_URL
    db_url = os.getenv("DATABASE_URL", _FALLBACK_URL)

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


# ── 4. Pitcher stat aliases (for backward compat with model features) ───────
# Raw GAME_QUERY columns (*_p_*) -> the derived names used by model features
# and the pick card. Shared module-level so BOTH build_features() (which
# derives the aliases) and _build_query()'s projection (which must keep the raw
# SOURCE columns when a derived alias is requested) reference the same mapping.
P_ALIASES = {
    "h_p_era_5": "h_pitcher_era_l5",
    "h_p_era_10": "h_pitcher_era_l10",
    "h_p_whip_5": "h_pitcher_whip_l5",
    "h_p_whip_10": "h_pitcher_whip_l10",
    "h_p_k9_5": "h_pitcher_k9_l5",
    "h_p_k9_10": "h_pitcher_k9_l10",
    "h_p_era_20": "h_pitcher_era_l20",
    "h_p_whip_20": "h_pitcher_whip_l20",
    "h_p_k9_20": "h_pitcher_k9_l20",
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
    "a_p_era_20": "a_pitcher_era_l20",
    "a_p_whip_20": "a_pitcher_whip_l20",
    "a_p_k9_20": "a_pitcher_k9_l20",
    "a_p_kbb_10": "a_pitcher_kbb_l10",
    "a_p_fip_ytd": "a_pitcher_fip_ytd",
    "a_p_era_ytd": "a_pitcher_era_ytd",
    "a_p_whip_ytd": "a_pitcher_whip_ytd",
    "a_p_k9_ytd": "a_pitcher_k9_ytd",
    "a_p_bb9_ytd": "a_pitcher_bb9_ytd",
    "a_p_qs_rate_ytd": "a_pitcher_qs_rate",
}

# Derived pitcher SPLIT ERA names (dest -> raw *_p_*_era_ytd source columns).
# Same projection concern as P_ALIASES: if the model/pick-card requests a dest
# (e.g. h_pitcher_home_era) the projection must keep the raw source column
# (h_p_home_era_ytd) or build_features() sets it to NaN.
P_SPLIT_MAP = {
    "h_pitcher_home_era": "h_p_home_era_ytd",
    "h_pitcher_day_era": "h_p_day_era_ytd",
    "h_pitcher_night_era": "h_p_night_era_ytd",
    "a_pitcher_road_era": "a_p_road_era_ytd",
    "a_pitcher_day_era": "a_p_day_era_ytd",
    "a_pitcher_night_era": "a_p_night_era_ytd",
}


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

    # ── 2b. Platoon OPS prior fill (LEAK-FREE) ────────────────────────────
    # h_ops_vs_opp_hand / a_ops_vs_opp_hand come from plato_* which are
    # through-date OPS vs the opposing starter's hand. Early in the season a
    # team may not have faced that arm yet -> plato_* is NULL there. Impute to
    # the team's cumulative/overall OPS (h_cum_ops/a_cum_ops, which themselves
    # blend to prior-season OPS) so the model never sees a false 0.000 platoon.
    for h_col, h_cum in (("h_ops_vs_opp_hand", "h_cum_ops"), ("a_ops_vs_opp_hand", "a_cum_ops")):
        if h_col in result.columns and h_cum in result.columns:
            result[h_col] = result[h_col].fillna(result[h_cum])

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
    for src, dst in P_ALIASES.items():
        if src in result.columns and dst not in result.columns:
            result[dst] = result[src]

    # ── 4b. Starter-sample gating: pitcher RATE stats untrusted until enough starts ──
    # ERA/WHIP/K9/BB9/FIP are all season-cumulative or window-averages that swing on
    # tiny denominators (a 1-appearance bulk reliever/opener yields meaningless values
    # e.g. era_5=135, whip_20=21, fip=45-93). NULL out ALL pitcher rate features until
    # the starter has logged >= MIN_STARTS_FOR_PITCHER starts, so the model never sees
    # 1-start garbage. (Rich 2026-08-19 accuracy audit)
    MIN_STARTS_FOR_PITCHER = 3
    _P_RATE_SUFFIXES = ("_era_ytd", "_era_l5", "_era_l10", "_era_l20",
                        "_whip_ytd", "_whip_l5", "_whip_l10", "_whip_l20",
                        "_k9_ytd", "_k9_l5", "_k9_l10", "_k9_l20",
                        "_bb9_ytd", "_fip_ytd")
    for side in ("h", "a"):
        starts_col = f"{side}_p_starts_ytd"
        if starts_col not in result.columns:
            continue
        low_starts = result[starts_col].isna() | (result[starts_col] < MIN_STARTS_FOR_PITCHER)
        for suffix in _P_RATE_SUFFIXES:
            col = f"{side}_pitcher{suffix}"
            if col in result.columns:
                result.loc[low_starts, col] = None

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
        # h_rf5 may be absent if the load was projected down to only model
        # features (training). Make it optional rather than hard-required.
        if "h_rf5" in result.columns:
            has_5 = result["h_rf5"].notna() & ~has_10
        else:
            has_5 = pd.Series([False] * len(result), index=result.index)
        rookie = ~has_10 & ~has_5  # very early season
        
        # LEAK-SAFE W/L record: read the season-total win/loss ENTERING this game
        # from the pre-computed mlb.team_rolling_stats table (trs_h/trs_a -> h_wins/
        # h_losses/...). DO NOT use mlb.games.home_wins/away_wins etc. — that table
        # stores the record INCLUDING the current game's result (data leak).
        def _wos(src):  # wins-or-losses series w/ int cast + 0 default
            if src in result.columns and result[src].notna().any():
                return pd.to_numeric(result[src], errors="coerce").fillna(0).astype(int)
            return pd.Series([0]*len(result))
        result["home_wins"]   = _wos("h_wins")
        result["home_losses"] = _wos("h_losses")
        result["away_wins"]   = _wos("a_wins")
        result["away_losses"] = _wos("a_losses")

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

    # Day/night game flag. Prefer the authoritative SQL `g.day_night` (set by the
    # ingest from the actual scheduled local start time — verified 'day'/'night'
    # correctly against real games). Only if that column is missing do we fall
    # back to an hour-based guess.
    if "day_night" not in result.columns:
        if "time" in result.columns:
            # time is a scheduled local clock (e.g. '19:10'); D if < 5pm
            result["day_night"] = result["time"].apply(
                lambda t: "D" if pd.notna(t) and str(t)[:2].isdigit() and int(str(t)[:2]) < 17 else "N"
            )
        else:
            result["day_night"] = "N"  # default night

    # Stadium / park factors
    # USE the real venue park factor (v.park_factor_overall) instead of a constant.
    # Previously park_factor was hardcoded to 100 (neutral) for every game, so it
    # carried ZERO information for the OU model (Coors ~109 vs Petco ~90 were never
    # differentiated). venues.park_factor_overall is already joined in GAME_QUERY as
    # venue_park_factor (Rich 2026-08-19 accuracy audit).
    if "venue_park_factor" in result.columns:
        result["park_factor"] = result["venue_park_factor"].fillna(100.0).astype(float)
        result["home_park_factor"] = result["venue_park_factor"].fillna(100.0).astype(float)
        result["away_park_factor"] = result["venue_park_factor"].fillna(100.0).astype(float)
    elif "venue_roof" in result.columns:
        result["park_factor"] = 100  # neutral default
        result["home_park_factor"] = 100
        result["away_park_factor"] = 100

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

    # ── Rolling L10 W/L + season expanding avg (from pre-computed team_rolling_stats) ──
    # These are computed at ingest time (backward-looking, shift-1 style: only games BEFORE
    # this one) and stored in mlb.team_rolling_stats. Reading them from the table ensures
    # live and backtest see IDENTICAL values regardless of the DataFrame's game-log snapshot,
    # and avoids blanks when only a single game is loaded.
    l10_cols = [
        "h_rf_avg", "a_rf_avg", "h_ra_avg", "a_ra_avg",
        "h_wins_l10", "a_wins_l10", "h_losses_l10", "a_losses_l10",
    ]
    missing = [c for c in l10_cols if c not in result.columns]
    if missing:
        # Fallback (no pre-computed cols): fill defaults so the model never sees NaN.
        for c in l10_cols:
            result[c] = result.get(c, None)

    # L10 win percentages from pre-computed W/L counts
    def _winpct(w, l):
        denom = (w + l).clip(lower=1)
        return (w / denom).where((w.notna() & l.notna()), None)

    result["h_winpct_l10"] = _winpct(result["h_wins_l10"], result["h_losses_l10"])
    result["a_winpct_l10"] = _winpct(result["a_wins_l10"], result["a_losses_l10"])

    result["winpct_l10_diff"] = result["h_winpct_l10"] - result["a_winpct_l10"]

    # Form = L10 winpct for now (can be refined to EMA later)
    result["h_form_l10"] = result["h_winpct_l10"]
    result["a_form_l10"] = result["a_winpct_l10"]

    # Over frequency — from team_rolling_stats.over_pct (season-level) / over_pct5 (L5).
    # over_pct is NULL whenever closing_ou is missing (~15% of rows), so backfill to a
    # NEUTRAL 0.5 (no information) rather than leaving NaN (which would become 0 in the
    # model input and falsely signal 'under-leaning'). over_freq5 already backfills; this
    # makes over_freq consistent (Rich 2026-08-19 accuracy audit).
    if "h_over_pct" in result.columns and "a_over_pct" in result.columns:
        result["h_over_freq"] = result["h_over_pct"].fillna(0.5)
        result["a_over_freq"] = result["a_over_pct"].fillna(0.5)
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
        # kbb_l20 — prefer 20-start, fall back to YTD (else leave NULL; the model
        # path imputes ytd via _impute_feature, the pick card blanks it)
        src_kbb20 = f"{ps}kbb_20"
        src_kbb_ytd = f"{ps}kbb_ytd"
        dst_kbb = f"{pt}kbb_l20"
        if src_kbb20 in result.columns:
            result[dst_kbb] = result[src_kbb20]
        elif src_kbb_ytd in result.columns:
            result[dst_kbb] = result[src_kbb_ytd]
        else:
            result[dst_kbb] = np.nan
        # kbb_l10 — prefer 10-start, fall back to YTD (else NULL, imputed at model
        # path only)
        src_kbb10 = f"{ps}kbb_10"
        dst_kbb10 = f"{pt}kbb_l10"
        if src_kbb10 in result.columns:
            result[dst_kbb10] = result[src_kbb10]
        elif src_kbb_ytd in result.columns:
            result[dst_kbb10] = result[src_kbb_ytd]
        else:
            result[dst_kbb10] = np.nan

    # Pitcher rest — from PRS rest_days (days since last start). Preserve RAW
    # (NULL when unknown): the pick card blanks it; the model path imputes ~4.
    if "h_p_rest" in result.columns:
        result["h_pitcher_rest"] = result["h_p_rest"]
    else:
        result["h_pitcher_rest"] = np.nan
    if "a_p_rest" in result.columns:
        result["a_pitcher_rest"] = result["a_p_rest"]
    else:
        result["a_pitcher_rest"] = np.nan

    # Pitcher split ERA — from PRS split columns. Map raw ytd split values into
    # the named columns the model/pick-card expect, preserving RAW truth: real
    # value or NULL. A missing split (e.g. no night games) stays NULL so the pick
    # card blanks it and `_impute_feature` (model path) fills the season ytd ERA
    # only when that feature is a model input. No .fillna(0), no cross-side fallback.
    for dest, src in P_SPLIT_MAP.items():
        result[dest] = result[src] if src in result.columns else np.nan

    # Real pitcher VENUE ERA (from prior starts at this exact park, multi-season).
    # NULL (not 0) when the pitcher has no starts at this venue. Never proxied to
    # home/road, never .fillna(0) -- a missing venue ERA is meaningful and must
    # stay NULL for the model to treat it as unknown.
    for dest in ("h_pitcher_venue_era", "a_pitcher_venue_era"):
        if dest not in result.columns:
            # Query did not provide venue ERA (other load path); leave absent so
            # downstream fillna/feature code treats it as unknown (NULL), never 0.
            result[dest] = np.nan
    for dest in ("h_pitcher_venue_starts", "a_pitcher_venue_starts"):
        if dest not in result.columns:
            result[dest] = 0

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

    # ── 10b. Retractable-roof roof-state: per-stadium temp band ───────────
    # Retractable parks (Houston/Texas/Arizona/etc.) usually play with the roof
    # CLOSED; the outdoor temp/wind sensors still record raw values (e.g. Minute
    # Maid 0-97F) that would incorrectly feed the model as if the game were
    # open-air. Rule (Rich 2026-08-19): for a retractable park, the roof is only
    # OPEN when the game-time temperature falls INSIDE that park's temp band.
    # Outside the band (or missing temp) => roof CLOSED => neutralize weather:
    # set is_dome=1, replace temperature with a neutral indoor 72F, and zero the
    # wind (both wind_calculated and wind_speed). Inside the band => keep real weather.
    NEUTRAL_INDOOR_TEMP = 72.0
    # roof-open temp range [min, max] per retractable park (via venue_name or g.venue)
    RETRACTABLE_TEMP_BANDS = {
        "minute maid park": (70.0, 82.0),      # Houston (roof almost always closed in TX heat)
        "daikin park": (70.0, 82.0),           # Houston (new name)
        "globe life field": (65.0, 85.0),      # Texas/Arlington
        "globe life park in arlington": (65.0, 85.0),  # Texas (old name, roof_type often NULL)
        "chase field": (70.0, 95.0),           # Arizona (open only in moderate PHX temps)
        "t-mobile park": (50.0, 80.0),         # Seattle (often open)
        "american family field": (55.0, 80.0), # Milwaukee
        "miller park": (55.0, 80.0),           # Milwaukee (old name)
        "rogers centre": (55.0, 80.0),         # Toronto
        "loan deposit park": (70.0, 88.0),     # Miami
        "loanDepot park".lower(): (70.0, 88.0),
    }
    DEFAULT_RETRACTABLE_BAND = (60.0, 85.0)

    roof_col = "roof_type" if "roof_type" in result.columns else "venue_roof"
    if roof_col in result.columns:
        # A game is RETRACTABLE if roof_type says so OR the venue is a known
        # retractable park. roof_type is NULL for ~51% of retractable-park games
        # (Minute Maid / Chase / Rogers / Miller / Globe Life etc.), so keying only
        # on roof_type silently left those games un-neutralized. Match by venue name
        # too (Rich 2026-08-19 accuracy audit).
        roof_is_retr = result[roof_col].str.lower() == "retractable"
        name_col = "venue_name" if "venue_name" in result.columns else "venue"
        if name_col in result.columns:
            _vkey = result[name_col].astype(str).str.lower()
            # Match retractable parks by distinctive venue-name substrings (handles
            # name variants like 'Globe Life Park in Arlington', 'loanDepot park'):
            _retr_names = [
                "minute maid", "daikin", "globe life", "chase field", "t-mobile",
                "american family", "miller park", "rogers", "loan depot", "loandepot",
            ]
            venue_is_retr = pd.Series(False, index=result.index)
            for _nm in _retr_names:
                venue_is_retr = venue_is_retr | _vkey.str.contains(_nm, regex=False, na=False)
        else:
            venue_is_retr = pd.Series(False, index=result.index)
        is_retractable = roof_is_retr | venue_is_retr
        if name_col in result.columns:
            key = result[name_col].astype(str).str.lower()
            # band lookup by nearest park name (handle 'Globe Life Park in Arlington'
            # via substring against the same retractable key set)
            _bandmap = {k: v for k, v in RETRACTABLE_TEMP_BANDS.items()}
            lo = pd.Series(DEFAULT_RETRACTABLE_BAND[0], index=result.index, dtype=float)
            hi = pd.Series(DEFAULT_RETRACTABLE_BAND[1], index=result.index, dtype=float)
            for _nm, (_lo, _hi) in _bandmap.items():
                _m = key.str.contains(_nm, regex=False, na=False)
                lo = lo.where(~_m, _lo)
                hi = hi.where(~_m, _hi)
            lo = lo.astype(float)
            hi = hi.astype(float)
            has_temp = result.get("temperature").notna() if "temperature" in result else pd.Series(False, index=result.index)
            temp_outside = pd.Series(False, index=result.index)
            if "temperature" in result:
                t = result["temperature"]
                temp_outside = (t < lo) | (t > hi)
            roof_closed = is_retractable & (temp_outside | (~has_temp))
            # Neutralize temperature + wind where roof is closed
            if roof_closed.any():
                if "temperature" in result:
                    result.loc[roof_closed, "temperature"] = NEUTRAL_INDOOR_TEMP
                if "wind_calculated" in result:
                    result.loc[roof_closed, "wind_calculated"] = 0.0
                if "wind_speed" in result:
                    result.loc[roof_closed, "wind_speed"] = 0.0
                if "wind_direction" in result:
                    result.loc[roof_closed, "wind_direction"] = ""

    # ── Group 7 — Calendar/Situational features ────────────────────────────────
    if "game_date" in result.columns:
        result["game_date"] = pd.to_datetime(result["game_date"])
        result["month"] = result["game_date"].dt.month
        result["is_summer"] = result["month"].isin([6, 7, 8]).astype(int)
    else:
        result["month"] = 6
        result["is_summer"] = 1

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
    # combo_era_r10_diff = head-to-head L10 ERA gap: home L10 ERA minus away L10 ERA.
    # (NOT combo_era_r10 - combo_era_r5, which measures L10-vs-L5 combined change.)
    if "h_era_10" in result.columns and "a_era_10" in result.columns:
        result["combo_era_r10_diff"] = result["h_era_10"] - result["a_era_10"]
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
    # True elapsed hours between the target game's first pitch and each team's last
    # game (UTC timestamps subtracted directly). This replaces the old flat `rest*24`
    # approximation, which overstated hours for any gap that isn't a whole day.
    _gd = pd.to_datetime(result["game_date"], errors="coerce") if "game_date" in result.columns else None
    if _gd is not None and "h_prev_game_date" in result.columns:
        _hp = pd.to_datetime(result["h_prev_game_date"], errors="coerce")
        result["rest_h_hours"] = (_gd - _hp).dt.total_seconds() / 3600.0
    elif "h_rest" in result.columns:
        result["rest_h_hours"] = result["h_rest"] * 24
    else:
        result["rest_h_hours"] = 0
    if _gd is not None and "a_prev_game_date" in result.columns:
        _ap = pd.to_datetime(result["a_prev_game_date"], errors="coerce")
        result["rest_a_hours"] = (_gd - _ap).dt.total_seconds() / 3600.0
    elif "a_rest" in result.columns:
        result["rest_a_hours"] = result["a_rest"] * 24
    else:
        result["rest_a_hours"] = 0
    if "rest_h_hours" in result.columns and "rest_a_hours" in result.columns:
        result["rest_diff_hours"] = result["rest_h_hours"] - result["rest_a_hours"]
    else:
        result["rest_diff_hours"] = 0

    # ── h_home_rf / a_away_rf (per-game averages from mlb.games box scores) ────
    # h_home_rf = home team's avg runs scored when playing at home
    # a_away_rf = away team's avg runs scored when playing on the road
    # Computed directly from FINAL box scores (runfg_h / runfg_a LATERALs) —
    # side-correct and leak-safe (only games strictly before the target).
    # Fall back to PRIOR-SEASON rf_home/rf_away only when the team has not yet
    # logged a home/away game this season (AVG over zero rows = NULL).
    if "h_home_runs_per_game" in result.columns:
        cur = result["h_home_runs_per_game"]
        if "h_prior_rf_home" in result.columns:
            cur = cur.fillna(result["h_prior_rf_home"])
        result["h_home_rf"] = cur
    else:
        result["h_home_rf"] = result["h_prior_rf_home"] if "h_prior_rf_home" in result.columns else np.nan
    if "a_away_runs_per_game" in result.columns:
        cur = result["a_away_runs_per_game"]
        if "a_prior_rf_away" in result.columns:
            cur = cur.fillna(result["a_prior_rf_away"])
        result["a_away_rf"] = cur
    else:
        result["a_away_rf"] = result["a_prior_rf_away"] if "a_prior_rf_away" in result.columns else np.nan

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
    # on a long-form DataFrame.  No shift needed — the LATERAL join in the
    # GAME_QUERY already resolves to the most recent completed game.
    # The L5 columns come from mlb.team_rolling_stats (via trs_h/trs_a LATERAL,
    # bullpen_ip_l5/bullpen_er_l5). Guard on them being present; do NOT zero-fill
    # them here (that would clobber the leak-safe table values).
    bp_ready = all(c in result.columns for c in [
        "h_bullpen_er_l5", "h_bullpen_ip_l5",
        "a_bullpen_er_l5", "a_bullpen_ip_l5",
    ])

    if bp_ready:
        # LEAK-SAFE table-driven bullpen L5 (last-5-games ER + IP-outs, THROUGH -
        # game convention, stored in mlb.team_rolling_stats). The trs_h/trs_a
        # LATERALs already read the PREVIOUS Final row, so h/a_bullpen_*_l5 give
        # the bullpen's last 5 appearances ENTERING the target game — correct,
        # leak-safe, and identical regardless of the frame size passed in.
        # (This replaces the old ad-hoc Pandas .rolling(5) over the frame, which
        # was frame-order-dependent and produced blanks/wrong sums.)
        for col in ["h_bullpen_er_l5", "h_bullpen_ip_l5",
                    "a_bullpen_er_l5", "a_bullpen_ip_l5"]:
            if col in result.columns:
                result[col] = pd.to_numeric(result[col], errors="coerce")
    
    # Convert L5 sums to bullpen ERA rate.
    # RAW truth: era from actual bullpen innings when the team has thrown any
    # (ip_l5 > 0). When there is NO bullpen data (0 innings in the last-5 window
    # — e.g. team hasn't played or no relief work logged), the value is missing
    # and should fall back to the PREVIOUS-YEAR team ERA (h_prior_era), never 0
    # (a 0.00 reads as "unstoppable bullpen") and never the old flat 4.5.
    for pfx in ["h_", "a_"]:
        er_l5 = f"{pfx}bullpen_er_l5"
        ip_l5 = f"{pfx}bullpen_ip_l5"
        era = f"{pfx}bullpen_era_l5"
        ip_outs = f"{pfx}bullpen_ip_l5"
        prior_era = f"{pfx}prior_era"
        if er_l5 in result.columns:
            # real ERA from actual innings (ip_l5>0). 0/0 (no data) -> NaN.
            ip_safe = result[ip_l5].where(result[ip_l5].gt(0))
            real_era = 9.0 * result[er_l5] / (ip_safe / 3.0)
            # no data -> prior-year team ERA as the fallback (never 0)
            if prior_era in result.columns:
                result[era] = real_era.fillna(result[prior_era])
            else:
                result[era] = real_era
            result[era] = result[era].clip(lower=0, upper=27)
            # Bullpen IP (innings): real when data exists (outs/3), else NULL.
            # Preserve the trained model's innings scale (//3), but a genuinely
            # missing window stays NULL (never fabricate innings a team didn't pitch).
            ip_out_ser = result[ip_l5]
            # Defensive: ensure numeric (the L5 cols come from a .map(); if any
            # future path yields object dtype, .gt(0) throws an ambiguous error).
            if not pd.api.types.is_numeric_dtype(ip_out_ser):
                ip_out_ser = pd.to_numeric(ip_out_ser, errors="coerce").fillna(0.0)
            result[ip_outs] = (ip_out_ser.where(ip_out_ser.gt(0)) / 3.0)
        else:
            result[era] = result[prior_era] if prior_era in result.columns else np.nan
            result[ip_outs] = np.nan

    # PRIME DIRECTIVE: Every pick card MUST include complete handicapping data.
    # So we keep all the raw columns too for the pick card builder.

    return result


# ── Column-projection helpers ────────────────────────────────────────────────
# GAME_QUERY is a single SELECT over `FROM mlb.games g LEFT JOIN LATERAL (...)`
# (NOT a CTE), so restricting the outer SELECT list lets Postgres skip computing
# unselected LATERAL outputs (pruning) instead of materializing all 291 columns.
# Parsing is exact-verified: 291 unique, non-duplicate aliases, every expr kept.

def _split_select_top_level(sel_body: str):
    """Split a SELECT column list into top-level segments (comma at paren-depth 0).

    Multi-line expressions (GREATEST(a,b), nested parens) stay whole. Full-line
    ``--`` comments are stripped BEFORE scanning so a comma inside a comment
    (e.g. ``-- validated, so the running models are unaffected.``) cannot fake a
    column boundary and corrupt the split. Inline trailing ``--`` comments are
    left intact (stripped later by _parse_game_query_columns).
    """
    # Drop full-line comment lines: they carry commas at depth 0 that would
    # otherwise split the SELECT in the wrong place.
    filtered = "\n".join(
        ln for ln in sel_body.splitlines() if not ln.lstrip().startswith("--")
    )
    parts = []
    depth = 0
    cur = ""
    for ch in filtered:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return parts


def _parse_game_query_columns(gq: str):
    """Return dict alias -> SQL expr for every SELECT output column in GAME_QUERY.

    Correctness gate: every segment must end with `AS <alias>`; segments that
    don't (continuation fragments split by commas inside function args at
    depth 0 only) are merged into the previous segment. Comment-only lines are
    stripped from each expr. Verified: 291 unique aliases, no duplicates.
    """
    sel_start = gq.index("SELECT") + len("SELECT")
    sel_end = gq.index("FROM mlb.games g", sel_start)
    segs = _split_select_top_level(gq[sel_start:sel_end])
    merged = []
    for seg in segs:
        m = re.search(r'AS\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*$', seg.rstrip(), re.S)
        if m:
            merged.append((seg, m.group(1)))
        else:
            # No `AS` alias. Two cases:
            #  1) a continuation fragment of the PREVIOUS segment (split by a
            #     comma inside a function call / paren). Merge into previous.
            #  2) a real unaliased output column like `g.home_score` or
            #     `g.status` -> Postgres names it after the identifier, so the
            #     result column = the trailing identifier (after the last dot).
            bare = seg.rstrip()
            stripped = "\n".join(l for l in bare.split("\n") if not l.lstrip().startswith("--"))
            stripped = stripped.strip()
            if re.fullmatch(r"[a-zA-Z_][\w]*", stripped):
                merged.append((seg, stripped))
            elif re.fullmatch(r"[a-zA-Z_][\w]*(?:\.[a-zA-Z_][\w]*)+", stripped):
                merged.append((seg, stripped.rsplit(".", 1)[1]))
            else:
                if merged:
                    prev_expr, alias = merged[-1]
                    merged[-1] = (prev_expr + "," + seg, alias)
                else:
                    merged.append((seg, None))
    out = {}
    for expr, alias in merged:
        expr_clean = "\n".join(l for l in expr.split("\n") if not l.lstrip().startswith("--")).strip()
        if alias:
            out[alias] = expr_clean
    return out


_GAME_QUERY_COLUMNS = _parse_game_query_columns(GAME_QUERY)


def project_game_query(query: str, required: Optional[set]) -> str:
    """Return GAME_QUERY's SELECT restricted to `required` output aliases.

    Always keeps the full original SQL otherwise. If `required` is None or empty
    of valid aliases, returns the query unchanged. Unknown requested names are
    ignored (they may be build_features-derived, produced later in pandas).
    """
    if not required:
        return query
    fresh = _parse_game_query_columns(query)
    keep = [a for a in fresh if a in required]
    keep_exprs = [fresh[a] for a in keep]
    if not keep_exprs:
        return query
    sel_start = query.index("SELECT") + len("SELECT")
    sel_end = query.index("FROM mlb.games g", sel_start)
    header = "\n    " + ",\n    ".join(keep_exprs) + "\n"
    return query[:sel_start] + header + query[sel_end:]


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
        # Feature catalog cache (source of truth: mlb.features table)
        self._catalog_cache: Optional[Dict[str, Dict[str, str]]] = None
        self._catalog_ts: float = 0

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

    # ── Feature catalog (source of truth: DB `features` table) ───────────
    _CATALOG_SCHEMA = "mlb"

    def _load_catalog_from_db(self) -> Dict[str, Dict[str, str]]:
        """Load {name: {'description','display_name'}} from mlb.features.

        The DB is the single source of truth for the feature catalog. Values
        are cached for a short TTL so admin edits (display name / description)
        propagate quickly without hammering the DB on every call.
        """
        now = time.time()
        if self._catalog_cache is not None and (now - self._catalog_ts) < 60:
            return self._catalog_cache
        try:
            engine = create_engine(DEFAULT_DB_URL)
            with engine.connect() as conn:
                rows = conn.execute(
                    text(f"SELECT name, description, display_name FROM {self._CATALOG_SCHEMA}.features")
                ).mappings().all()
            engine.dispose()
            catalog = {}
            for r in rows:
                name = r["name"]
                catalog[name] = {
                    "description": r["description"] or "",
                    "display_name": r["display_name"] or name,
                }
            self._catalog_cache = catalog
            self._catalog_ts = now
            return catalog
        except Exception:
            logger.exception("Failed to load feature catalog from DB; using empty")
            self._catalog_cache = {}
            self._catalog_ts = now
            return self._catalog_cache

    def get_features_catalog(self) -> Dict[str, str]:
        """Return the full feature catalog (name -> description) from the DB.

        Any column that appears in GAME_QUERY but has no DB row falls back to
        an auto-generated description so nothing comes back blank.
        """
        db = self._load_catalog_from_db()
        merged = {name: meta["description"] for name, meta in db.items()}
        # Auto-generate descriptions for any GAME_QUERY column missing from DB
        seen = set(merged.keys())
        for m in re.finditer(r'\bAS\s+(\w+)', GAME_QUERY):
            col = m.group(1)
            if col not in merged:
                merged[col] = self._auto_description(col)
            seen.add(col)
        select_part = GAME_QUERY.split("ORDER BY")[0] if "ORDER BY" in GAME_QUERY else GAME_QUERY
        for m in re.finditer(r'(?:\w+\.)?(\w+)(?=\s*[,]\s*|\s*$)', select_part):
            col = m.group(1)
            if col and col not in seen and col.isidentifier() and len(col) > 1 and not col.isupper():
                seen.add(col)
                if col not in merged:
                    merged[col] = self._auto_description(col)
        return merged

    def get_feature_names(self) -> List[str]:
        """Return the list of all known feature names."""
        return list(self.get_features_catalog().keys())

    def get_feature_description(self, name: str) -> Optional[str]:
        """Return the DB description for a feature (auto fallback if absent)."""
        db = self._load_catalog_from_db()
        meta = db.get(name)
        if meta and meta["description"]:
            return meta["description"]
        return self._auto_description(name)

    def get_display_name(self, name: str) -> str:
        """Return the customer-facing display name from the DB.

        Falls back to the DB row name, then a smart auto-generated label.
        """
        db = self._load_catalog_from_db()
        meta = db.get(name)
        if meta and meta["display_name"]:
            return meta["display_name"]
        return self._auto_display(name)

    def get_all_with_display(self) -> List[Dict[str, str]]:
        """Return a list of dicts with name, description, display_name."""
        db = self._load_catalog_from_db()
        # Include DB rows plus any GAME_QUERY columns not present in DB
        names = set(db.keys())
        for m in re.finditer(r'\bAS\s+(\w+)', GAME_QUERY):
            names.add(m.group(1))
        out = []
        for name in names:
            meta = db.get(name)
            out.append({
                "name": name,
                "description": (meta["description"] if meta and meta["description"]
                                else self._auto_description(name)),
                "display_name": (meta["display_name"] if meta and meta["display_name"]
                                  else self._auto_display(name)),
            })
        return out

    # ── Public load methods ─────────────────────────────────────────────────

    def load_games(
        self,
        seasons: Optional[List[int]] = None,
        status: str = "FINAL",
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        game_ids: Optional[List[int]] = None,
        columns: Optional[List[str]] = None,
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
        columns :
            If set, restrict GAME_QUERY's SELECT to ONLY these output aliases
            (unknown names ignored). Since GAME_QUERY is a single SELECT over
            LATERAL joins (not a CTE), Postgres skips computing unselected
            LATERAL outputs — so load time tracks the number of requested
            feature columns instead of all 291. Pass the exact feature set a
            model needs to cut load dramatically. None = load everything.

        Returns
        -------
        pd.DataFrame
            One row per game, with all columns from GAME_QUERY (or `columns` subset).
        """
        engine = create_engine(
            self._db_url,
            # ``jit=off`` scoped to THIS loader's connection only: MLB's
            # GAME_QUERY (via _query below) pays a large JIT compile tax to
            # run modest row counts. Disabling JIT here avoids that overhead
            # without affecting other workloads on the instance.
            # statement_timeout=20min guards against orphaned backends when a
            # client dies mid-load (longest legit query ~4 min).
            connect_args={"options": "-c jit=off -c statement_timeout=1200000"},
        )
        try:
            return self._query(engine, seasons=seasons, status=status,
                               limit=limit, include_upcoming=include_upcoming,
                               game_ids=game_ids, columns=columns)
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
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Load game data as a pandas DataFrame (async, using an existing engine).
        See load_games() for the `columns` projection semantics."""
        return await self._query_async(engine, seasons=seasons, status=status,
                                       limit=limit, include_upcoming=include_upcoming,
                                       game_ids=game_ids, columns=columns)

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
        columns: Optional[List[str]] = None,
    ) -> str:
        """Build the SQL query with filters.

        If `columns` is given, the GAME_QUERY SELECT is projected down to only
        those output aliases (plus any that don't exist are ignored). Because
        GAME_QUERY is a single SELECT over LATERAL joins (not a CTE), Postgres
        skips computing unselected LATERAL outputs → load is much faster when a
        model only needs a subset of features. Use None to load everything.
        """
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

        if columns:
            req = set(columns)
            # Expand: any requested python-derived pitcher alias (e.g.
            # h_pitcher_whip_l20) needs its RAW GAME_QUERY source column
            # (h_p_whip_20) kept in the projection, or build_features() can't
            # derive it and the model/pick-card reads a 0.
            for src, dst in P_ALIASES.items():
                if dst in req:
                    req.add(src)
            # Also keep raw split-ERA source columns (h_p_home_era_ytd, etc.) so
            # the derived home/road/day/night pitcher ERA columns get the prior
            # FINAL-game values instead of NaN (same bug class as P_ALIASES).
            for dest, src in P_SPLIT_MAP.items():
                if dest in req and src not in req:
                    req.add(src)
            # Bullpen ERA L5 is python-derived from raw *_bullpen_{er,ip}_l5
            # (+ *_prior_era fallback). Keep those raw columns in the projection
            # or the derived bullpen_era_l5 goes NaN.
            for side in ("h", "a"):
                if f"{side}_bullpen_era_l5" in req:
                    req.add(f"{side}_bullpen_er_l5")
                    req.add(f"{side}_bullpen_ip_l5")
                    req.add(f"{side}_prior_era")
            # build_features 4b gating also keys off *_p_starts_ytd; keep it
            for side in ("h", "a"):
                req.add(f"{side}_p_starts_ytd")
            sql = project_game_query(sql, req)

        return sql

    def _query(
        self,
        engine: Any,
        seasons: Optional[List[int]],
        status: Optional[str],
        limit: Optional[int],
        include_upcoming: bool,
        game_ids: Optional[List[int]] = None,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        sql = self._build_query(seasons, status, limit, include_upcoming,
                                game_ids=game_ids, columns=columns)
        logger.debug("Executing query:\n%s", sql)
        with engine.connect() as conn:
            df = pd.read_sql(text(sql), conn, parse_dates=["game_date"])
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
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        sql = self._build_query(seasons, status, limit, include_upcoming,
                                game_ids=game_ids, columns=columns)
        logger.debug("Executing async query:\n%s", sql)
        async with engine.connect() as conn:
            result = await conn.execute(text(sql))
            rows = result.fetchall()
            cols = result.keys()
        df = pd.DataFrame(rows, columns=cols)
        # game_date arrives as TIMESTAMPTZ objects; coerce so downstream
        # build_features .dt accessors work.
        if "game_date" in df.columns:
            df["game_date"] = pd.to_datetime(df["game_date"])
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
