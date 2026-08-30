"""
NBA Data Loader — loads and prepares NBA game data for model training and inference.

Mirror of the NFL data_loader.py with NBA-specific schemas, team locations,
features from nba.features, and NBA-relevant computed features.

Key differences from NFL:
  - Schema: nba.* (not nfl.*)
  - Games have period-based quarter scoring (nba.games)
  - No dome/outdoor distinction (all indoor arenas)
  - No weather data (all indoor)
  - Different betting line columns (spread, over_under)
  - Time zone / travel logic uses NBA team cities
  - Opponent-adjusted scoring uses nba_xgb_model_ats.py's feature set
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from math import asin, cos, radians, sin, sqrt
from sqlalchemy import create_engine

logger = logging.getLogger(__name__)

# ── Database connection ────────────────────────────────────────────────────────
# Single source of truth via db_urls — avoids hardcoded passwords and +asyncpg issues.
from app.db_urls import PSYCOPG2_DATABASE_URL

DEFAULT_DB_URL: str = PSYCOPG2_DATABASE_URL


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in miles between two lat/lng points."""
    R = 3958.8  # Earth radius in miles
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return float(R * 2 * asin(sqrt(a)))


def rolling_mean_safe(
    s: pd.Series, window: int, min_periods: int = 1
) -> pd.Series:
    """Rolling mean with fallback — ensures float return."""
    return s.rolling(window, min_periods=min_periods).mean()


# ═══════════════════════════════════════════════════════════════════════════════
#  TEAM_LOCATIONS — lat/lng for NBA arenas
# ═══════════════════════════════════════════════════════════════════════════════

TEAM_LOCATIONS: Dict[str, Tuple[float, float]] = {
    "ATL": (33.7575, -84.3963),    # State Farm Arena — Atlanta
    "BOS": (42.3663, -71.0624),    # TD Garden — Boston
    "BKN": (40.6829, -73.9754),    # Barclays Center — Brooklyn
    "CHA": (35.2252, -80.8398),    # Spectrum Center — Charlotte
    "CHI": (41.8809, -87.6742),    # United Center — Chicago
    "CLE": (41.4963, -81.6882),    # Rocket Mortgage FieldHouse — Cleveland
    "DAL": (32.7905, -96.8103),    # American Airlines Center — Dallas
    "DEN": (39.7482, -105.0076),   # Ball Arena — Denver
    "DET": (42.3410, -83.0548),    # Little Caesars Arena — Detroit
    "GSW": (37.7479, -122.3873),   # Chase Center — Golden State
    "HOU": (29.7508, -95.3622),    # Toyota Center — Houston
    "IND": (39.7640, -86.1558),    # Gainbridge Fieldhouse — Indiana
    "LAC": (34.0430, -118.2673),   # Crypto.com Arena — LA Clippers
    "LAL": (34.0430, -118.2673),   # Crypto.com Arena — LA Lakers
    "MEM": (35.1382, -90.0506),    # FedExForum — Memphis
    "MIA": (25.7814, -80.1871),    # Kaseya Center — Miami
    "MIL": (43.0452, -87.9172),    # Fiserv Forum — Milwaukee
    "MIN": (44.9795, -93.2757),    # Target Center — Minnesota
    "NOP": (29.9491, -90.0822),    # Smoothie King Center — New Orleans
    "NYK": (40.7505, -73.9934),    # Madison Square Garden — New York
    "OKC": (35.4634, -97.5151),    # Paycom Center — Oklahoma City
    "ORL": (28.5392, -81.4687),    # Kia Center — Orlando
    "PHI": (39.9013, -75.1719),    # Wells Fargo Center — Philadelphia
    "PHX": (33.4457, -112.0710),   # Footprint Center — Phoenix
    "POR": (45.5316, -122.6668),   # Moda Center — Portland
    "SAC": (38.5803, -121.4996),   # Golden 1 Center — Sacramento
    "SAS": (29.4271, -98.4376),    # Frost Bank Center — San Antonio
    "TOR": (43.6435, -79.3791),    # Scotiabank Arena — Toronto
    "UTA": (40.7683, -111.9011),   # Delta Center — Utah
    "WAS": (38.8982, -77.0211),    # Capital One Arena — Washington
}


# ═══════════════════════════════════════════════════════════════════════════════
#  GAME_QUERY — loads raw per-game NBA data from the database
# ═══════════════════════════════════════════════════════════════════════════════

GAME_QUERY = """
WITH betting_agg AS (
    SELECT
        blc.game_id,
        blc.opening_spread,
        blc.opening_ou,
        blc.opening_home_ml,
        blc.opening_away_ml,
        blc.closing_spread,
        blc.closing_ou,
        blc.closing_home_ml                   AS home_moneyline,
        blc.closing_away_ml                   AS away_moneyline,
        blc.closing_spread_home_odds          AS spread_home_odds,
        blc.closing_spread_away_odds          AS spread_away_odds,
        blc.opening_spread_home_odds          AS opening_spread_home_odds,
        blc.opening_spread_away_odds          AS opening_spread_away_odds,
        blc.closing_over_odds                 AS over_odds,
        blc.closing_under_odds                AS under_odds,
        blc.closing_home_implied_probability  AS home_implied_probability,
        blc.closing_away_implied_probability  AS away_implied_probability
    FROM nba.betting_lines_consolidated blc
),
team_games AS (
    SELECT
        g.id                                                                    AS game_id,
        g.nba_game_id,
        g.season_id,
        s.year                                                                  AS season_year,
        g.date,
        g.home_team_id,
        g.away_team_id,
        g.home_score,
        g.away_score,
        g.status,
        g.game_type,
        g.attendance,
        g.home_field_goals_made,
        g.home_field_goals_attempted,
        g.home_three_points_made,
        g.home_three_points_attempted,
        g.home_free_throws_made,
        g.home_free_throws_attempted,
        g.home_rebounds,
        g.home_assists,
        g.home_steals,
        g.home_blocks,
        g.home_turnovers,
        g.home_fouls,
        g.away_field_goals_made,
        g.away_field_goals_attempted,
        g.away_three_points_made,
        g.away_three_points_attempted,
        g.away_free_throws_made,
        g.away_free_throws_attempted,
        g.away_rebounds,
        g.away_assists,
        g.away_steals,
        g.away_blocks,
        g.away_turnovers,
        g.away_fouls,
        ht.abbreviation                                                         AS home_abbr,
        ht.name                                                                 AS home_team_name,
        CONCAT(ht.name, ' ', ht.abbreviation)                                   AS home_team,
        at.abbreviation                                                         AS away_abbr,
        at.name                                                                 AS away_team_name,
        CONCAT(at.name, ' ', at.abbreviation)                                   AS away_team,
        ba.opening_spread,
        ba.opening_ou,
        ba.opening_home_ml,
        ba.opening_away_ml,
        ba.closing_spread,
        ba.closing_ou,
        ba.home_moneyline,
        ba.away_moneyline,
        ba.spread_home_odds,
        ba.spread_away_odds,
        ba.opening_spread_home_odds,
        ba.opening_spread_away_odds,
        ba.over_odds,
        ba.under_odds,
        ba.home_implied_probability,
        ba.away_implied_probability,

        -- Home team cumulative stats (backward-looking, season-to-date)
        hcs.games_played           AS h_games_played,
        hcs.cum_ppg                AS h_cum_ppg,
        hcs.cum_oppg               AS h_cum_oppg,
        hcs.cum_margin_pg          AS h_cum_margin_pg,
        hcs.cum_fg_pct             AS h_cum_fg_pct,
        hcs.cum_fg3_pct            AS h_cum_fg3_pct,
        hcs.cum_ft_pct             AS h_cum_ft_pct,
        hcs.cum_reb_pg             AS h_cum_reb_pg,
        hcs.cum_ast_pg             AS h_cum_ast_pg,
        hcs.cum_stl_pg             AS h_cum_stl_pg,
        hcs.cum_blk_pg             AS h_cum_blk_pg,
        hcs.cum_tov_pg             AS h_cum_tov_pg,
        hcs.cum_pf_pg              AS h_cum_pf_pg,
        hcs.cum_ortg               AS h_cum_ortg,
        hcs.cum_drtg               AS h_cum_drtg,
        hcs.cum_net_ortg           AS h_cum_net_ortg,
        hcs.cum_adj_ortg           AS h_cum_adj_ortg,
        hcs.cum_adj_drtg           AS h_cum_adj_drtg,
        hcs.cum_sos                AS h_sos,
        hcs.cum_pace               AS h_cum_pace,
        hcs.cum_efg_pct            AS h_cum_efg_pct,
        hcs.cum_opp_efg_pct        AS h_cum_opp_efg_pct,
        hcs.cum_tov_rate           AS h_cum_tov_rate,
        hcs.cum_opp_tov_rate       AS h_cum_opp_tov_rate,
        hcs.cum_ft_rate            AS h_cum_ft_rate,
        hcs.cum_3pa_rate           AS h_cum_3pa_rate,
        hcs.cum_ast_ratio          AS h_cum_ast_ratio,
        hcs.cum_stl_rate           AS h_cum_stl_rate,
        hcs.cum_blk_rate           AS h_cum_blk_rate,
        hcs.cum_win_pct            AS h_cum_win_pct,

        -- Tier 4: Momentum & recency
        hrs.rw3_ppg                AS h_rw3_ppg,
        hrs.rw5_ppg                AS h_rw5_ppg,
        hrs.rw3_net_rtg            AS h_rw3_net_rtg,
        hrs.rw5_net_rtg            AS h_rw5_net_rtg,
        hrs.rw3_efg_pct            AS h_rw3_efg_pct,
        hrs.rw5_efg_pct            AS h_rw5_efg_pct,
        hrs.rw3_drtg               AS h_rw3_drtg,
        hrs.rw5_drtg               AS h_rw5_drtg,
        hrs.cv10_ppg               AS h_cv10_ppg,
        hrs.cv20_ppg               AS h_cv20_ppg,
        hrs.cv10_net_rtg           AS h_cv10_net_rtg,
        hrs.recency_ppg            AS h_recency_ppg,
        hrs.recency_net_rtg        AS h_recency_net_rtg,
        hrs.net_rtg_r5             AS h_net_rtg_r5,
        hrs.net_rtg_r10            AS h_net_rtg_r10,
        hrs.ortg_r5                AS h_ortg_r5,
        hrs.ortg_r10               AS h_ortg_r10,
        hrs.drtg_r5                AS h_drtg_r5,
        hrs.drtg_r10               AS h_drtg_r10,
        hrs.efg_r5                 AS h_efg_r5,
        hrs.efg_r10                AS h_efg_r10,
        hrs.pace_r5                AS h_pace_r5,
        hrs.pace_r10               AS h_pace_r10,
        hrs.ast_ratio_r5           AS h_ast_ratio_r5,
        hrs.ast_ratio_r10          AS h_ast_ratio_r10,
        hrs.ft_rate_r5             AS h_ft_rate_r5,
        hrs.ft_rate_r10            AS h_ft_rate_r10,
        hrs.threep_rate_r5         AS h_threep_rate_r5,
        hrs.threep_rate_r10        AS h_threep_rate_r10,
        hrs.ats_margin_5           AS h_ats_margin_5,
        hrs.ats_margin_10          AS h_ats_margin_10,
        hrs.ats_wins_5             AS h_ats_wins_5,
        hrs.ats_wins_10            AS h_ats_wins_10,
        hrs.ou_wins_5              AS h_ou_wins_5,
        hrs.ou_wins_10             AS h_ou_wins_10,
        hrs.ou_margin_5            AS h_ou_margin_5,
        hrs.wins_5                 AS h_wins_5,
        hrs.wins_10                AS h_wins_10,
        hrs.adj_off_10             AS h_adj_off_10,
        hrs.adj_def_10             AS h_adj_def_10,
        hrs_hv.venue_pts_r10       AS h_home_pts_r10,
        hrs_hv.venue_win_pct_r10   AS h_home_win_pct_r10,
        cgs_hv.venue_win_pct_season AS h_home_win_pct_season,
        hrs.star_ppg_5             AS h_star_ppg_5,
        hrs.star1_ppg_5            AS h_star1_ppg_5,
        hrs.stars_active           AS h_stars_active,
        hrs.star1_active           AS h_star1_active,
        COALESCE(h_actv.actv_pts, 0)  AS h_active_pts,
        COALESCE(h_actv.actv_reb, 0)  AS h_active_reb,
        COALESCE(h_actv.actv_ast, 0)  AS h_active_ast,
        COALESCE(h_actv.actv_n, 0)    AS h_active_n,
        COALESCE(h_actv.actv_pts, 0) - COALESCE(hcs.cum_ppg, 0)  AS h_active_pts_minus_team,
        COALESCE(h_actv.actv_reb, 0) - COALESCE(hcs.cum_reb_pg, 0) AS h_active_reb_minus_team,
        COALESCE(h_actv.actv_ast, 0) - COALESCE(hcs.cum_ast_pg, 0) AS h_active_ast_minus_team,
        -- starter-only active-roster aggregates (same 5-man content, only starters)
        COALESCE(h_actv_st.st_pts, 0)  AS h_starter_pts,
        COALESCE(h_actv_st.st_reb, 0)  AS h_starter_reb,
        COALESCE(h_actv_st.st_ast, 0)  AS h_starter_ast,
        COALESCE(h_actv_st.st_n, 0)    AS h_starter_n,
        COALESCE(h_actv_st.st_pts, 0) - COALESCE(hcs.cum_ppg, 0)  AS h_starter_pts_minus_team,
        COALESCE(h_actv_st.st_reb, 0) - COALESCE(hcs.cum_reb_pg, 0) AS h_starter_reb_minus_team,
        COALESCE(h_actv_st.st_ast, 0) - COALESCE(hcs.cum_ast_pg, 0) AS h_starter_ast_minus_team,
        -- home starters' per-game-rate (games-PLAYED denominator) vs team season total
        COALESCE(h_actv_st.st_pts_gp, 0) - COALESCE(hcs.cum_ppg, 0) AS h_starter_pts_gp_minus_team,
        COALESCE(h_actv_st.st_reb_gp, 0) - COALESCE(hcs.cum_reb_pg, 0) AS h_starter_reb_gp_minus_team,
        COALESCE(h_actv_st.st_ast_gp, 0) - COALESCE(hcs.cum_ast_pg, 0) AS h_starter_ast_gp_minus_team,
        -- Away team cumulative stats (backward-looking, season-to-date)
        acs.games_played           AS a_games_played,
        acs.cum_ppg                AS a_cum_ppg,
        acs.cum_oppg               AS a_cum_oppg,
        acs.cum_margin_pg          AS a_cum_margin_pg,
        acs.cum_fg_pct             AS a_cum_fg_pct,
        acs.cum_fg3_pct            AS a_cum_fg3_pct,
        acs.cum_ft_pct             AS a_cum_ft_pct,
        acs.cum_reb_pg             AS a_cum_reb_pg,
        acs.cum_ast_pg             AS a_cum_ast_pg,
        acs.cum_stl_pg             AS a_cum_stl_pg,
        acs.cum_blk_pg             AS a_cum_blk_pg,
        acs.cum_tov_pg             AS a_cum_tov_pg,
        acs.cum_pf_pg              AS a_cum_pf_pg,
        acs.cum_ortg               AS a_cum_ortg,
        acs.cum_drtg               AS a_cum_drtg,
        acs.cum_net_ortg           AS a_cum_net_ortg,
        acs.cum_adj_ortg           AS a_cum_adj_ortg,
        acs.cum_adj_drtg           AS a_cum_adj_drtg,
        acs.cum_sos                AS a_sos,
        acs.cum_pace               AS a_cum_pace,
        acs.cum_efg_pct            AS a_cum_efg_pct,
        acs.cum_opp_efg_pct        AS a_cum_opp_efg_pct,
        acs.cum_tov_rate           AS a_cum_tov_rate,
        acs.cum_opp_tov_rate       AS a_cum_opp_tov_rate,
        acs.cum_ft_rate            AS a_cum_ft_rate,
        acs.cum_3pa_rate           AS a_cum_3pa_rate,
        acs.cum_ast_ratio          AS a_cum_ast_ratio,
        acs.cum_stl_rate           AS a_cum_stl_rate,
        acs.cum_blk_rate           AS a_cum_blk_rate,
        acs.cum_win_pct            AS a_cum_win_pct,

        -- Tier 4: Momentum & recency
        ars.rw3_ppg                AS a_rw3_ppg,
        ars.rw5_ppg                AS a_rw5_ppg,
        ars.rw3_net_rtg            AS a_rw3_net_rtg,
        ars.rw5_net_rtg            AS a_rw5_net_rtg,
        ars.rw3_efg_pct            AS a_rw3_efg_pct,
        ars.rw5_efg_pct            AS a_rw5_efg_pct,
        ars.rw3_drtg               AS a_rw3_drtg,
        ars.rw5_drtg               AS a_rw5_drtg,
        ars.cv10_ppg               AS a_cv10_ppg,
        ars.cv20_ppg               AS a_cv20_ppg,
        ars.cv10_net_rtg           AS a_cv10_net_rtg,
        ars.recency_ppg            AS a_recency_ppg,
        ars.recency_net_rtg        AS a_recency_net_rtg,
        ars.net_rtg_r5             AS a_net_rtg_r5,
        ars.net_rtg_r10            AS a_net_rtg_r10,
        ars.ortg_r5                AS a_ortg_r5,
        ars.ortg_r10               AS a_ortg_r10,
        ars.drtg_r5                AS a_drtg_r5,
        ars.drtg_r10               AS a_drtg_r10,
        ars.efg_r5                 AS a_efg_r5,
        ars.efg_r10                AS a_efg_r10,
        ars.pace_r5                AS a_pace_r5,
        ars.pace_r10               AS a_pace_r10,
        ars.ast_ratio_r5           AS a_ast_ratio_r5,
        ars.ast_ratio_r10          AS a_ast_ratio_r10,
        ars.ft_rate_r5             AS a_ft_rate_r5,
        ars.ft_rate_r10            AS a_ft_rate_r10,
        ars.threep_rate_r5         AS a_threep_rate_r5,
        ars.threep_rate_r10        AS a_threep_rate_r10,
        ars.ats_margin_5           AS a_ats_margin_5,
        ars.ats_margin_10          AS a_ats_margin_10,
        ars.ats_wins_5             AS a_ats_wins_5,
        ars.ats_wins_10            AS a_ats_wins_10,
        ars.ou_wins_5              AS a_ou_wins_5,
        ars.ou_wins_10             AS a_ou_wins_10,
        ars.ou_margin_5            AS a_ou_margin_5,
        ars.wins_5                 AS a_wins_5,
        ars.wins_10                AS a_wins_10,
        ars.adj_off_10             AS a_adj_off_10,
        ars.adj_def_10             AS a_adj_def_10,
        ars_av.venue_pts_r10       AS a_away_pts_r10,
        ars_av.venue_win_pct_r10   AS a_away_win_pct_r10,
        cgs_av.venue_win_pct_season AS a_away_win_pct_season,
        ars.star_ppg_5             AS a_star_ppg_5,
        ars.star1_ppg_5            AS a_star1_ppg_5,
        ars.stars_active           AS a_stars_active,
        ars.star1_active           AS a_star1_active,
        COALESCE(a_actv.actv_pts, 0)  AS a_active_pts,
        COALESCE(a_actv.actv_reb, 0)  AS a_active_reb,
        COALESCE(a_actv.actv_ast, 0)  AS a_active_ast,
        COALESCE(a_actv.actv_n, 0)    AS a_active_n,
        COALESCE(a_actv.actv_pts, 0) - COALESCE(acs.cum_ppg, 0)  AS a_active_pts_minus_team,
        COALESCE(a_actv.actv_reb, 0) - COALESCE(acs.cum_reb_pg, 0) AS a_active_reb_minus_team,
        COALESCE(a_actv.actv_ast, 0) - COALESCE(acs.cum_ast_pg, 0) AS a_active_ast_minus_team,
        -- starter-only active-roster aggregates (away)
        COALESCE(a_actv_st.st_pts, 0)  AS a_starter_pts,
        COALESCE(a_actv_st.st_reb, 0)  AS a_starter_reb,
        COALESCE(a_actv_st.st_ast, 0)  AS a_starter_ast,
        COALESCE(a_actv_st.st_n, 0)    AS a_starter_n,
        COALESCE(a_actv_st.st_pts, 0) - COALESCE(acs.cum_ppg, 0)  AS a_starter_pts_minus_team,
        COALESCE(a_actv_st.st_reb, 0) - COALESCE(acs.cum_reb_pg, 0) AS a_starter_reb_minus_team,
        COALESCE(a_actv_st.st_ast, 0) - COALESCE(acs.cum_ast_pg, 0) AS a_starter_ast_minus_team,
        -- away starters' per-game-rate (games-PLAYED denominator) vs team season total
        COALESCE(a_actv_st.st_pts_gp, 0) - COALESCE(acs.cum_ppg, 0) AS a_starter_pts_gp_minus_team,
        COALESCE(a_actv_st.st_reb_gp, 0) - COALESCE(acs.cum_reb_pg, 0) AS a_starter_reb_gp_minus_team,
        COALESCE(a_actv_st.st_ast_gp, 0) - COALESCE(acs.cum_ast_pg, 0) AS a_starter_ast_gp_minus_team,
        pts_h.cum_ppg              AS h_prior_cum_ppg,
        pts_h.cum_oppg             AS h_prior_cum_oppg,
        pts_h.cum_margin_pg        AS h_prior_cum_margin_pg,
        pts_h.cum_fg_pct           AS h_prior_cum_fg_pct,
        pts_h.cum_fg3_pct          AS h_prior_cum_fg3_pct,
        pts_h.cum_ft_pct           AS h_prior_cum_ft_pct,
        pts_h.cum_reb_pg           AS h_prior_cum_reb_pg,
        pts_h.cum_ast_pg           AS h_prior_cum_ast_pg,
        pts_h.cum_stl_pg           AS h_prior_cum_stl_pg,
        pts_h.cum_blk_pg           AS h_prior_cum_blk_pg,
        pts_h.cum_tov_pg           AS h_prior_cum_tov_pg,
        pts_h.cum_pf_pg            AS h_prior_cum_pf_pg,
        pts_h.cum_ortg             AS h_prior_cum_ortg,
        pts_h.cum_drtg             AS h_prior_cum_drtg,
        pts_h.cum_net_ortg         AS h_prior_cum_net_ortg,
        pts_h.cum_pace             AS h_prior_cum_pace,
        pts_h.cum_efg_pct          AS h_prior_cum_efg_pct,
        pts_h.cum_opp_efg_pct      AS h_prior_cum_opp_efg_pct,
        pts_h.cum_tov_rate         AS h_prior_cum_tov_rate,
        pts_h.cum_opp_tov_rate     AS h_prior_cum_opp_tov_rate,
        pts_h.cum_ft_rate          AS h_prior_cum_ft_rate,
        pts_h.cum_3pa_rate         AS h_prior_cum_3pa_rate,
        pts_h.cum_ast_ratio        AS h_prior_cum_ast_ratio,
        pts_h.cum_stl_rate         AS h_prior_cum_stl_rate,
        pts_h.cum_blk_rate         AS h_prior_cum_blk_rate,
        pts_h.cum_win_pct          AS h_prior_cum_win_pct,
        pts_h.cum_adj_ortg         AS h_prior_cum_adj_ortg,
        pts_h.cum_adj_drtg         AS h_prior_cum_adj_drtg,
        pts_h.cum_sos              AS h_prior_sos,
        pts_h.rw3_ppg              AS h_prior_rw3_ppg,
        pts_h.rw5_ppg              AS h_prior_rw5_ppg,
        pts_h.rw3_net_rtg          AS h_prior_rw3_net_rtg,
        pts_h.rw5_net_rtg          AS h_prior_rw5_net_rtg,
        pts_h.rw3_efg_pct          AS h_prior_rw3_efg_pct,
        pts_h.rw5_efg_pct          AS h_prior_rw5_efg_pct,
        pts_h.rw3_drtg             AS h_prior_rw3_drtg,
        pts_h.rw5_drtg             AS h_prior_rw5_drtg,
        pts_h.cv10_ppg             AS h_prior_cv10_ppg,
        pts_h.cv20_ppg             AS h_prior_cv20_ppg,
        pts_h.cv10_net_rtg         AS h_prior_cv10_net_rtg,
        pts_h.recency_ppg          AS h_prior_recency_ppg,
        pts_h.recency_net_rtg      AS h_prior_recency_net_rtg,
        pts_h.net_rtg_r5           AS h_prior_net_rtg_r5,
        pts_h.net_rtg_r10          AS h_prior_net_rtg_r10,
        pts_h.ortg_r5              AS h_prior_ortg_r5,
        pts_h.ortg_r10             AS h_prior_ortg_r10,
        pts_h.drtg_r5              AS h_prior_drtg_r5,
        pts_h.drtg_r10             AS h_prior_drtg_r10,
        pts_h.efg_r5               AS h_prior_efg_r5,
        pts_h.efg_r10              AS h_prior_efg_r10,
        pts_h.pace_r5              AS h_prior_pace_r5,
        pts_h.pace_r10             AS h_prior_pace_r10,
        pts_h.ast_ratio_r5         AS h_prior_ast_ratio_r5,
        pts_h.ast_ratio_r10        AS h_prior_ast_ratio_r10,
        pts_h.ft_rate_r5           AS h_prior_ft_rate_r5,
        pts_h.ft_rate_r10          AS h_prior_ft_rate_r10,
        pts_h.threep_rate_r5       AS h_prior_threep_rate_r5,
        pts_h.threep_rate_r10      AS h_prior_threep_rate_r10,
        pts_h.ats_margin_5         AS h_prior_ats_margin_5,
        pts_h.ats_margin_10        AS h_prior_ats_margin_10,
        pts_h.ats_wins_5           AS h_prior_ats_wins_5,
        pts_h.ats_wins_10          AS h_prior_ats_wins_10,
        pts_h.ou_wins_5            AS h_prior_ou_wins_5,
        pts_h.ou_wins_10           AS h_prior_ou_wins_10,
        pts_h.ou_margin_5          AS h_prior_ou_margin_5,
        pts_h.wins_5               AS h_prior_wins_5,
        pts_h.wins_10              AS h_prior_wins_10,
        pts_h.adj_off_10           AS h_prior_adj_off_10,
        pts_h.adj_def_10           AS h_prior_adj_def_10,
        pts_h.star_ppg_5           AS h_prior_star_ppg_5,
        pts_h.star1_ppg_5          AS h_prior_star1_ppg_5,
        pts_h.stars_active         AS h_prior_stars_active,
        pts_h.star1_active         AS h_prior_star1_active,
        -- prior-season (previous full season) away values for blending
        pts_a.cum_ppg              AS a_prior_cum_ppg,
        pts_a.cum_oppg             AS a_prior_cum_oppg,
        pts_a.cum_margin_pg        AS a_prior_cum_margin_pg,
        pts_a.cum_fg_pct           AS a_prior_cum_fg_pct,
        pts_a.cum_fg3_pct          AS a_prior_cum_fg3_pct,
        pts_a.cum_ft_pct           AS a_prior_cum_ft_pct,
        pts_a.cum_reb_pg           AS a_prior_cum_reb_pg,
        pts_a.cum_ast_pg           AS a_prior_cum_ast_pg,
        pts_a.cum_stl_pg           AS a_prior_cum_stl_pg,
        pts_a.cum_blk_pg           AS a_prior_cum_blk_pg,
        pts_a.cum_tov_pg           AS a_prior_cum_tov_pg,
        pts_a.cum_pf_pg            AS a_prior_cum_pf_pg,
        pts_a.cum_ortg             AS a_prior_cum_ortg,
        pts_a.cum_drtg             AS a_prior_cum_drtg,
        pts_a.cum_net_ortg         AS a_prior_cum_net_ortg,
        pts_a.cum_pace             AS a_prior_cum_pace,
        pts_a.cum_efg_pct          AS a_prior_cum_efg_pct,
        pts_a.cum_opp_efg_pct      AS a_prior_cum_opp_efg_pct,
        pts_a.cum_tov_rate         AS a_prior_cum_tov_rate,
        pts_a.cum_opp_tov_rate     AS a_prior_cum_opp_tov_rate,
        pts_a.cum_ft_rate          AS a_prior_cum_ft_rate,
        pts_a.cum_3pa_rate         AS a_prior_cum_3pa_rate,
        pts_a.cum_ast_ratio        AS a_prior_cum_ast_ratio,
        pts_a.cum_stl_rate         AS a_prior_cum_stl_rate,
        pts_a.cum_blk_rate         AS a_prior_cum_blk_rate,
        pts_a.cum_win_pct          AS a_prior_cum_win_pct,
        pts_a.cum_adj_ortg         AS a_prior_cum_adj_ortg,
        pts_a.cum_adj_drtg         AS a_prior_cum_adj_drtg,
        pts_a.cum_sos              AS a_prior_sos,
        pts_a.rw3_ppg              AS a_prior_rw3_ppg,
        pts_a.rw5_ppg              AS a_prior_rw5_ppg,
        pts_a.rw3_net_rtg          AS a_prior_rw3_net_rtg,
        pts_a.rw5_net_rtg          AS a_prior_rw5_net_rtg,
        pts_a.rw3_efg_pct          AS a_prior_rw3_efg_pct,
        pts_a.rw5_efg_pct          AS a_prior_rw5_efg_pct,
        pts_a.rw3_drtg             AS a_prior_rw3_drtg,
        pts_a.rw5_drtg             AS a_prior_rw5_drtg,
        pts_a.cv10_ppg             AS a_prior_cv10_ppg,
        pts_a.cv20_ppg             AS a_prior_cv20_ppg,
        pts_a.cv10_net_rtg         AS a_prior_cv10_net_rtg,
        pts_a.recency_ppg          AS a_prior_recency_ppg,
        pts_a.recency_net_rtg      AS a_prior_recency_net_rtg,
        pts_a.net_rtg_r5           AS a_prior_net_rtg_r5,
        pts_a.net_rtg_r10          AS a_prior_net_rtg_r10,
        pts_a.ortg_r5              AS a_prior_ortg_r5,
        pts_a.ortg_r10             AS a_prior_ortg_r10,
        pts_a.drtg_r5              AS a_prior_drtg_r5,
        pts_a.drtg_r10             AS a_prior_drtg_r10,
        pts_a.efg_r5               AS a_prior_efg_r5,
        pts_a.efg_r10              AS a_prior_efg_r10,
        pts_a.pace_r5              AS a_prior_pace_r5,
        pts_a.pace_r10             AS a_prior_pace_r10,
        pts_a.ast_ratio_r5         AS a_prior_ast_ratio_r5,
        pts_a.ast_ratio_r10        AS a_prior_ast_ratio_r10,
        pts_a.ft_rate_r5           AS a_prior_ft_rate_r5,
        pts_a.ft_rate_r10          AS a_prior_ft_rate_r10,
        pts_a.threep_rate_r5       AS a_prior_threep_rate_r5,
        pts_a.threep_rate_r10      AS a_prior_threep_rate_r10,
        pts_a.ats_margin_5         AS a_prior_ats_margin_5,
        pts_a.ats_margin_10        AS a_prior_ats_margin_10,
        pts_a.ats_wins_5           AS a_prior_ats_wins_5,
        pts_a.ats_wins_10          AS a_prior_ats_wins_10,
        pts_a.ou_wins_5            AS a_prior_ou_wins_5,
        pts_a.ou_wins_10           AS a_prior_ou_wins_10,
        pts_a.ou_margin_5          AS a_prior_ou_margin_5,
        pts_a.wins_5               AS a_prior_wins_5,
        pts_a.wins_10              AS a_prior_wins_10,
        pts_a.adj_off_10           AS a_prior_adj_off_10,
        pts_a.adj_def_10           AS a_prior_adj_def_10,
        pts_a.star_ppg_5           AS a_prior_star_ppg_5,
        pts_a.star1_ppg_5          AS a_prior_star1_ppg_5,
        pts_a.stars_active         AS a_prior_stars_active,
        pts_a.star1_active         AS a_prior_star1_active
    FROM nba.games g
    JOIN nba.teams ht ON ht.id = g.home_team_id
    JOIN nba.teams at ON at.id = g.away_team_id
    JOIN nba.seasons s ON s.id = g.season_id
    INNER JOIN betting_agg ba ON ba.game_id = g.id
    -- ── Efficient prior-game pointer resolution (prev_game_id fast path) ──
    -- Resolve each team's effective prior game_id ONCE. For a FINAL target game the
    -- snapshot tables carry prev_game_id_season (cross-venue) / prev_game_id_side
    -- (same venue) directly -> O(1) equality join downstream, no ORDER BY scan.
    -- For a scheduled game there is no snapshot row yet, so fall back to the
    -- indexed "most recent prior game" lookup (single ORDER BY, leak-safe).
    LEFT JOIN LATERAL (
        SELECT COALESCE(
            (SELECT cgs.prev_game_id_season FROM nba.cumulative_game_stats cgs
              WHERE cgs.game_id = g.id AND cgs.team_id = g.home_team_id),
            (SELECT cgs2.game_id FROM nba.cumulative_game_stats cgs2
              WHERE cgs2.team_id = g.home_team_id AND cgs2.game_id != g.id
                AND cgs2.game_date < (g.date AT TIME ZONE 'America/New_York')::date AND cgs2.season_id = g.season_id
              ORDER BY cgs2.game_date DESC, cgs2.game_id DESC LIMIT 1)
        ) AS prior_game_id
    ) h_prio ON true
    LEFT JOIN LATERAL (
        SELECT COALESCE(
            (SELECT cgs.prev_game_id_season FROM nba.cumulative_game_stats cgs
              WHERE cgs.game_id = g.id AND cgs.team_id = g.away_team_id),
            (SELECT cgs2.game_id FROM nba.cumulative_game_stats cgs2
              WHERE cgs2.team_id = g.away_team_id AND cgs2.game_id != g.id
                AND cgs2.game_date < (g.date AT TIME ZONE 'America/New_York')::date AND cgs2.season_id = g.season_id
              ORDER BY cgs2.game_date DESC, cgs2.game_id DESC LIMIT 1)
        ) AS prior_game_id
    ) a_prio ON true
    LEFT JOIN LATERAL (
        SELECT COALESCE(
            (SELECT rs.prev_game_id_side FROM nba.team_rolling_stats rs
              WHERE rs.game_id = g.id AND rs.team_id = g.home_team_id AND rs.team_side = 'home'),
            (SELECT rs2.game_id FROM nba.team_rolling_stats rs2
              WHERE rs2.team_id = g.home_team_id AND rs2.team_side = 'home' AND rs2.game_id != g.id
                AND rs2.game_date < (g.date AT TIME ZONE 'America/New_York')::date AND rs2.season_id = g.season_id
              ORDER BY rs2.game_date DESC, rs2.game_id DESC LIMIT 1)
        ) AS prior_game_id
    ) h_prio_side ON true
    LEFT JOIN LATERAL (
        SELECT COALESCE(
            (SELECT rs.prev_game_id_side FROM nba.team_rolling_stats rs
              WHERE rs.game_id = g.id AND rs.team_id = g.away_team_id AND rs.team_side = 'away'),
            (SELECT rs2.game_id FROM nba.team_rolling_stats rs2
              WHERE rs2.team_id = g.away_team_id AND rs2.team_side = 'away' AND rs2.game_id != g.id
                AND rs2.game_date < (g.date AT TIME ZONE 'America/New_York')::date AND rs2.season_id = g.season_id
              ORDER BY rs2.game_date DESC, rs2.game_id DESC LIMIT 1)
        ) AS prior_game_id
    ) a_prio_side ON true
    LEFT JOIN LATERAL (
        SELECT cgs.* FROM nba.cumulative_game_stats cgs
        WHERE cgs.team_id = g.home_team_id AND cgs.game_id = h_prio.prior_game_id
    ) hcs ON true
    LEFT JOIN LATERAL (
        SELECT rs.* FROM nba.team_rolling_stats rs
        WHERE rs.team_id = g.home_team_id AND rs.game_id = h_prio.prior_game_id
    ) hrs ON true
    LEFT JOIN LATERAL (
        SELECT cgs.* FROM nba.cumulative_game_stats cgs
        WHERE cgs.team_id = g.away_team_id AND cgs.game_id = a_prio.prior_game_id
    ) acs ON true
    LEFT JOIN LATERAL (
        SELECT rs.* FROM nba.team_rolling_stats rs
        WHERE rs.team_id = g.away_team_id AND rs.game_id = a_prio.prior_game_id
    ) ars ON true
    -- Venue-conditional reads (home team's last HOME row / away team's last ROAD
    -- row). These feed the venue-scoped split features (h_home_pts_r10,
    -- h_home_win_pct_r10 / a_away_pts_r10, a_away_win_pct_r10). Since the
    -- rolling table stores one row per (game, team_side), we filter team_side so
    -- venue_* is read from the row matching the target venue. Leak-safe.
    LEFT JOIN LATERAL (
        SELECT rs.venue_pts_r10, rs.venue_win_pct_r10
        FROM nba.team_rolling_stats rs
        WHERE rs.team_id = g.home_team_id
          AND rs.team_side = 'home'
          AND rs.game_id = h_prio_side.prior_game_id
    ) hrs_hv ON true
    LEFT JOIN LATERAL (
        SELECT rs.venue_pts_r10, rs.venue_win_pct_r10
        FROM nba.team_rolling_stats rs
        WHERE rs.team_id = g.away_team_id
          AND rs.team_side = 'away'
          AND rs.game_id = a_prio_side.prior_game_id
    ) ars_av ON true
    -- Venue-scoped season win pct from cumulative (home team's home win pct season-to-date
    -- / away team's road win pct season-to-date). Leak-safe.
    LEFT JOIN LATERAL (
        SELECT cgs.venue_win_pct_season
        FROM nba.cumulative_game_stats cgs
        WHERE cgs.team_id = g.home_team_id
          AND cgs.team_side = 'home'
          AND cgs.game_id = h_prio_side.prior_game_id
    ) cgs_hv ON true
    LEFT JOIN LATERAL (
        SELECT cgs.venue_win_pct_season
        FROM nba.cumulative_game_stats cgs
        WHERE cgs.team_id = g.away_team_id
          AND cgs.team_side = 'away'
          AND cgs.game_id = a_prio_side.prior_game_id
    ) cgs_av ON true
    -- ── Active-player aggregates (the core active-roster feature) ──
    -- For each team use the TARGET game's OWN active roster (nba.active_players
    -- rows for game g) when present -- the active roster is knowable before
    -- tip-off (pregame info), so this is NOT a leak. Fall back to the team's
    -- most recent prior FINAL game's roster (h_prio/a_prio) only if game g's
    -- roster isn't filled yet (e.g. a scheduled game not yet pregame-ingested).
    -- Each active player's cum_ppg/rpg/apg is read from their MOST RECENT PRIOR
    -- player_rolling_stats row (strictly before the target game -> leak-safe),
    -- then SUM across the roster.
    --
    -- PERF (2026-08-23): rewritten from nested correlated subqueries to indexed
    -- O(log n) lookups. (1) The effective roster game per team is resolved once
    -- (EXISTS-prefer g.id else prior game). (2) active_players is read on
    -- (team_id, game_id=eff) via idx_active_players_team_game. (3) each player's
    -- prior stats use `game_id < g.id ORDER BY game_id DESC LIMIT 1` -- a reverse
    -- index seek on the PK (player_id, game_id); game_id is strictly chronological
    -- within (season, game_type), so this picks the most recent prior game and is
    -- leak-safe, carrying across the season boundary exactly like the old sort.
    LEFT JOIN LATERAL (
        SELECT CASE
            WHEN EXISTS (SELECT 1 FROM nba.active_players x
                         WHERE x.team_id = g.home_team_id AND x.game_id = g.id)
            THEN g.id ELSE COALESCE(h_prio.prior_game_id, g.id) END AS eff
    ) heff ON true
    LEFT JOIN LATERAL (
        SELECT CASE
            WHEN EXISTS (SELECT 1 FROM nba.active_players x
                         WHERE x.team_id = g.away_team_id AND x.game_id = g.id)
            THEN g.id ELSE COALESCE(a_prio.prior_game_id, g.id) END AS eff
    ) aeff ON true
    LEFT JOIN LATERAL (
        -- 🔴 FIX 2026-08-24: active/roster totals are per-team-GAME, not a sum of
        -- per-player rates. Old code did SUM(prs.cum_ppg/rpg/apg) (each player's own
        -- per-game avg), which over-counts players that missed games and lets the
        -- "active" sum exceed the team total (impossible, active is a subset of team).
        -- Correct: SUM(player season TOTALS) / team games_played, so active <= team.
        SELECT
            COALESCE(SUM(prs.cum_points), 0)    / NULLIF(hcs.games_played, 0) AS actv_pts,
            COALESCE(SUM(prs.cum_rebounds), 0)  / NULLIF(hcs.games_played, 0) AS actv_reb,
            COALESCE(SUM(prs.cum_assists), 0)   / NULLIF(hcs.games_played, 0) AS actv_ast,
            COUNT(prs.cum_points)               AS actv_n
        FROM nba.active_players ap
        LEFT JOIN LATERAL (
            SELECT prs.cum_points, prs.cum_rebounds, prs.cum_assists
            FROM nba.player_rolling_stats prs
            WHERE prs.player_id = ap.player_id
              AND prs.game_id < g.id
              AND prs.season_id = g.season_id   -- 🔴 season-scoped: no cross-season leak
            ORDER BY prs.game_id DESC
            LIMIT 1
        ) prs ON true
        WHERE ap.team_id = g.home_team_id
          AND ap.game_id = heff.eff
          AND ap.status <> 'INACTIVE'   -- injury/health scratches excluded from ACTIVE roster sum
    ) h_actv ON true
    -- Starter-only equivalent of the home active-roster aggregate: identical
    -- player_rolling_stats prior read (leak-safe), but only players flagged
    -- is_starter on the effective active roster. Same pts/ast/reb sums.
    LEFT JOIN LATERAL (
        SELECT
            COALESCE(SUM(prs.cum_points), 0)    / NULLIF(hcs.games_played, 0) AS st_pts,
            COALESCE(SUM(prs.cum_rebounds), 0)  / NULLIF(hcs.games_played, 0) AS st_reb,
            COALESCE(SUM(prs.cum_assists), 0)   / NULLIF(hcs.games_played, 0) AS st_ast,
            COUNT(prs.cum_points)               AS st_n,
            -- per-game-rate (games-PLAYED denominator) variant: each starter's own
            -- season avg in the games he actually played, summed over the 5 starters
            COALESCE(SUM(prs.cum_ppg), 0)       AS st_pts_gp,
            COALESCE(SUM(prs.cum_rpg), 0)       AS st_reb_gp,
            COALESCE(SUM(prs.cum_apg), 0)       AS st_ast_gp
        FROM nba.active_players ap
        LEFT JOIN LATERAL (
            SELECT prs.cum_points, prs.cum_rebounds, prs.cum_assists, prs.cum_ppg, prs.cum_rpg, prs.cum_apg
            FROM nba.player_rolling_stats prs
            WHERE prs.player_id = ap.player_id
              AND prs.game_id < g.id
              AND prs.season_id = g.season_id   -- 🔴 season-scoped: no cross-season leak
            ORDER BY prs.game_id DESC
            LIMIT 1
        ) prs ON true
        WHERE ap.team_id = g.home_team_id
          AND ap.is_starter
          AND ap.game_id = heff.eff
          AND ap.status <> 'INACTIVE'
    ) h_actv_st ON true
    LEFT JOIN LATERAL (
        SELECT
            COALESCE(SUM(prs.cum_points), 0)    / NULLIF(acs.games_played, 0) AS actv_pts,
            COALESCE(SUM(prs.cum_rebounds), 0)  / NULLIF(acs.games_played, 0) AS actv_reb,
            COALESCE(SUM(prs.cum_assists), 0)   / NULLIF(acs.games_played, 0) AS actv_ast,
            COUNT(prs.cum_points)               AS actv_n
        FROM nba.active_players ap
        LEFT JOIN LATERAL (
            SELECT prs.cum_points, prs.cum_rebounds, prs.cum_assists
            FROM nba.player_rolling_stats prs
            WHERE prs.player_id = ap.player_id
              AND prs.game_id < g.id
              AND prs.season_id = g.season_id   -- 🔴 season-scoped: no cross-season leak
            ORDER BY prs.game_id DESC
            LIMIT 1
        ) prs ON true
        WHERE ap.team_id = g.away_team_id
          AND ap.game_id = aeff.eff
          AND ap.status <> 'INACTIVE'   -- injury/health scratches excluded from ACTIVE roster sum
    ) a_actv ON true
    LEFT JOIN LATERAL (
        SELECT
            COALESCE(SUM(prs.cum_points), 0)    / NULLIF(acs.games_played, 0) AS st_pts,
            COALESCE(SUM(prs.cum_rebounds), 0)  / NULLIF(acs.games_played, 0) AS st_reb,
            COALESCE(SUM(prs.cum_assists), 0)   / NULLIF(acs.games_played, 0) AS st_ast,
            COUNT(prs.cum_points)               AS st_n,
            -- per-game-rate (games-PLAYED denominator) variant
            COALESCE(SUM(prs.cum_ppg), 0)       AS st_pts_gp,
            COALESCE(SUM(prs.cum_rpg), 0)       AS st_reb_gp,
            COALESCE(SUM(prs.cum_apg), 0)       AS st_ast_gp
        FROM nba.active_players ap
        LEFT JOIN LATERAL (
            SELECT prs.cum_points, prs.cum_rebounds, prs.cum_assists, prs.cum_ppg, prs.cum_rpg, prs.cum_apg
            FROM nba.player_rolling_stats prs
            WHERE prs.player_id = ap.player_id
              AND prs.game_id < g.id
              AND prs.season_id = g.season_id   -- 🔴 season-scoped: no cross-season leak
            ORDER BY prs.game_id DESC
            LIMIT 1
        ) prs ON true
        WHERE ap.team_id = g.away_team_id
          AND ap.is_starter
          AND ap.game_id = aeff.eff
          AND ap.status <> 'INACTIVE'
    ) a_actv_st ON true
    LEFT JOIN nba.prior_team_stats pts_h
        ON pts_h.team_id = g.home_team_id AND pts_h.season_year = s.year - 1
    LEFT JOIN nba.prior_team_stats pts_a
        ON pts_a.team_id = g.away_team_id AND pts_a.season_year = s.year - 1
    WHERE g.status = 'FINAL'
      AND g.home_score IS NOT NULL
      AND g.away_score IS NOT NULL
      AND g.home_score > 0
      AND g.away_score > 0
)
SELECT * FROM team_games
ORDER BY season_id, date ASC
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  Feature catalogs — synchronised with nba.features DB table
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
#  Location cache for haversine lookups
# ═══════════════════════════════════════════════════════════════════════════════

_location_cache = {
    abbr: (loc[0], loc[1]) for abbr, loc in TEAM_LOCATIONS.items()
}


# ═══════════════════════════════════════════════════════════════════════════════
#  NBADataLoader
# ═══════════════════════════════════════════════════════════════════════════════


class NBADataLoader:
    """Loads and prepares NBA game data for model training and inference.

    Mirrors the NFLDataLoader pattern.

    Parameters
    ----------
    db_url :
        PostgreSQL connection string.  If ``None`` uses ``DEFAULT_DB_URL``.
    ats_only :
        If True, only load / compute features needed for the ATS model.
    ou_only :
        If True, only load / compute features needed for the OU model.
    """

    def __init__(
        self,
        db_url: Optional[str] = None,
        ats_only: bool = False,
        ou_only: bool = False,
    ) -> None:
        self.db_url: str = db_url or DEFAULT_DB_URL
        self.ats_only: bool = ats_only
        self.ou_only: bool = ou_only
        self._engine: Any = None
        # Source of truth: nba.features table (name -> description)
        self._catalog, self._display_names = self._load_catalog_from_db()
        self._feature_cache: Optional[pd.DataFrame] = None
        logger.info(
            "NBADataLoader initialized (ats_only=%s, ou_only=%s, catalog=%d)",
            ats_only, ou_only, len(self._catalog),
        )

    @property
    def engine(self):
        """Lazy-initialized SQLAlchemy engine."""
        if self._engine is None:
            from sqlalchemy import create_engine
            # ``jit=off`` scoped to THIS loader's pooled connections only:
            # the GAME_QUERY is wide-but-modest (23k rows), so Postgres spends
            # ~11.8s compiling JIT expressions (118 fns) that dwarf the ~1.5s
            # of actual execution. Disabling JIT here cuts load from ~13-23s
            # to ~2-3s. Not applied instance-wide (other workloads benefit).
            self._engine = create_engine(
                self.db_url,
                pool_pre_ping=True,
                # statement_timeout=20min: guards against orphaned backends when
                # a client dies mid-load (longest legit query ~4 min).
                connect_args={"options": "-c jit=off -c statement_timeout=1200000"},
            )
        return self._engine

    def _load_catalog_from_db(self) -> "Tuple[Dict[str, str], Dict[str, str]]":
        """Load {name: description} and {name: display_name} from nba.features.

        The DB is the single source of truth for the feature catalog. Returns a
        (catalog, display_names) pair. Safe: on any DB failure it falls back to
        the hardcoded in-memory catalogs so nothing breaks during startup.
        """
        try:
            with psycopg2.connect(PSYCOPG2_DATABASE_URL) as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT name, description, display_name FROM nba.features")
                    rows = cur.fetchall()
            catalog = {}
            display = {}
            for r in rows:
                name = r["name"]
                catalog[name] = r["description"] or ""
                display[name] = r["display_name"] or name
            return catalog, display
        except Exception:
            logger.exception("Failed to load catalog from DB; leaving catalog empty")
            return {}, {}

    def __repr__(self) -> str:
        return (
            f"NBADataLoader(db_url={self.db_url!r}, "
            f"ats_only={self.ats_only}, ou_only={self.ou_only})"
        )

    # ── Feature catalog helpers ──────────────────────────────────────────────

    def get_features_catalog(self) -> Dict[str, str]:
        """Return the full feature catalog (base + computed)."""
        return dict(self._catalog)

    def get_feature_names(self) -> List[str]:
        """Return sorted list of all known feature names."""
        return sorted(self._catalog.keys())

    def get_feature_description(self, name: str) -> str:
        """Return the description for a feature (or empty string)."""
        return self._catalog.get(name, "")

    def get_display_name(self, name: str) -> str:
        """Return the human-readable display name for a feature."""
        return self._display_names.get(name, name)

    def get_feature_columns(self, target: Optional[str] = None, live: bool = False) -> List[str]:
        """Return trainable feature column names.

        Parameters
        ----------
        target :
            If ``'ats'``, only return features flagged current_ats / is_trainable
            that correspond to ATS features.  If ``None``, return all
            trainable features (all computed).
        live :
            If ``True``, read the ``live_ats`` / ``live_ou`` flags (matching the
            MLB loader's convention and the live models).  Default ``False``
            reads ``current_ats`` / ``current_ou``.  ``db_training.py`` keeps both
            in sync from the trained Booster's feature set, so they're identical.

        Returns
        -------
        Sorted list of feature column names.
        """
        if target in ("ats", "ou"):
            flag = ("live_ats" if live else "current_ats") if target == "ats" \
                else ("live_ou" if live else "current_ou")
            try:
                with psycopg2.connect(PSYCOPG2_DATABASE_URL) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT name FROM nba.features WHERE {flag} = TRUE "
                            "AND is_trainable = TRUE ORDER BY id"
                        )
                        rows = cur.fetchall()
                        db_features = [r[0] for r in rows]
                        known = set(self._catalog.keys())
                        return sorted(c for c in db_features if c in known)
            except Exception:
                pass
            # Fallback: return home/away computed features
            return sorted(
                k for k in self._catalog
                if k.startswith(("h_", "a_"))
            )
        return sorted(self._catalog.keys())

    def get_all_with_display(self) -> List[Dict[str, str]]:
        """Return a list of dicts with 'name', 'description', 'display_name'."""
        return [
            {
                "name": name,
                "description": desc,
                "display_name": self._display_names.get(name, name),
            }
            for name, desc in self._catalog.items()
        ]

    # ── Query helpers ────────────────────────────────────────────────────────

    def _build_query(self, base_query: str, **kwargs: Any) -> str:
        """Build a query string from the base query and optional overrides."""
        if not kwargs:
            return base_query
        return base_query.format(**kwargs)

    def _query(self, sql: str) -> pd.DataFrame:
        """Execute raw SQL via the engine and return a DataFrame."""
        t0 = time.time()
        df = pd.read_sql(sql, self.engine)
        elapsed = time.time() - t0
        logger.info("Query returned %d rows in %.2fs", len(df), elapsed)
        return df

    # ── Cumulative stats refresh ──────────────────────────────────────

    def refresh_cumulative_stats(
        self,
        force_rebuild: bool = False,
    ) -> Dict[str, int]:
        """Refresh the nba.cumulative_game_stats pre-computed table.

        Calls the module-level ``populate_cumulative_stats()``, which
        runs incremental upserts (only processes new games) unless
        ``force_rebuild=True`` drops and rebuilds everything.

        Returns
        -------
        Summary dict with ``rows_processed``.
        """
        from .cumulative_stats import populate_cumulative_stats

        return populate_cumulative_stats(
            self.db_url,
            force_rebuild=force_rebuild,
        )

    # ── Load methods ─────────────────────────────────────────────────────────

    def load_games(
        self,
        seasons: Optional[List[int]] = None,
        status: Optional[str] = "FINAL",
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        game_ids: Optional[List[int]] = None,
        game_types: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Load NBA games from the database.

        Parameters
        ----------
        seasons :
            List of season years (calendar years, e.g. ``[2024, 2025]``) to
            load.  Consistent with the NFL/MLB loaders, which filter on
            ``s.year`` — NOT internal season IDs.  If ``None`` loads all.
        status :
            Game status filter (e.g. ``'FINAL'``).
        limit :
            Maximum number of games to return.
        include_upcoming :
            If True, include games that haven't been played yet.
        game_ids :
            Specific game IDs to load.

        Returns
        -------
        DataFrame with raw game data.
        """
        # Track whether this is a sparse single/batch-game inference load (game_ids
        # given) vs a full-season training load.  Used by _build_features to pad
        # team_games with each team's prior games so rest/travel/fatigue features
        # are computed against the true previous game (not NaN/0).
        self._sparse_inference = game_ids is not None
        query = GAME_QUERY

        where_parts: List[str] = []
        # Preseason games must never feed stats/training. NBA's PRE games can
        # carry a FINAL status with results, so excluding purely by status is
        # not enough — always drop them here.
        if game_types:
            # Explicit game_type allow-list (e.g. traffic only REG). When set,
            # this REPLACES the broad "!= 'PRE'" filter so callers can restrict
            # training/inference to specific game types (e.g. regular season only).
            type_list = ", ".join(f"'{t}'" for t in game_types)
            where_parts.append(f"game_type IN ({type_list})")
        else:
            where_parts.append("game_type != 'PRE'")
        if status:
            where_parts.append(f"status = '{status}'")
        if seasons:
            season_list = ", ".join(str(s) for s in seasons)
            # Filter on calendar year (s.year AS season_year), NOT internal
            # season_id — matches NFL/MLB loaders and callers that pass years.
            where_parts.append(f"season_year IN ({season_list})")
        if game_ids:
            id_list = ", ".join(str(g) for g in game_ids)
            where_parts.append(f"game_id IN ({id_list})")

        if where_parts:
            where_clause = " AND ".join(where_parts)
            query = query.replace(
                "SELECT * FROM team_games",
                f"SELECT * FROM team_games WHERE {where_clause}",
            ).replace("ORDER BY season_id, date ASC", "")
            query += " ORDER BY season_id, date ASC"

        if limit:
            query += f" LIMIT {limit}"

        return self._query(query)

    def load_all_games(self) -> pd.DataFrame:
        """Load *all* games (convenience wrapper)."""
        return self.load_games()

    def load_data(
        self,
        seasons: Optional[List[int]] = None,
        limit: Optional[int] = None,
        refresh_cumulative: bool = False,
        force_rebuild_cumulative: bool = False,
        game_types: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Load game data and apply full feature engineering.

        Main entry point for training pipelines.

        ``refresh_cumulative`` defaults to ``False``: the
        ``nba.cumulative_game_stats`` table is refreshed by a dedicated
        scheduled task during the season, NOT by this loader.  Pass
        ``refresh_cumulative=True`` explicitly to the task / manual run that
        owns the wholesale refresh (or to force a rebuild via
        ``force_rebuild_cumulative``).

        Parameters
        ----------
        refresh_cumulative :
            If True, refresh ``nba.cumulative_game_stats`` before loading
            games (incremental upsert of new games only).
        force_rebuild_cumulative :
            If True, drop and rebuild the cumulative stats table entirely.
        """
        if refresh_cumulative:
            self.refresh_cumulative_stats(
                force_rebuild=force_rebuild_cumulative,
            )
        df = self.load_games(seasons=seasons, limit=limit, game_types=game_types)
        if df.empty:
            logger.warning("No NBA games found for seasons=%s", seasons)
            return df

        df = self._build_features(df)
        return df

    def load_inference_data(
        self,
        game_ids: Optional[List[int]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Load data for inference on specific (or recent) games."""
        self._inference_requested = (
            list(game_ids) if game_ids is not None else None
        )
        df = self.load_games(
            seasons=None,
            status=None,
            limit=limit,
            include_upcoming=True,
            game_ids=game_ids,
        )
        if df.empty:
            return df

        df = self._build_features(df)
        return df

    # ── Feature column management ────────────────────────────────────────────

    def extract_features_from_training_run(
        self,
        results_json: Any,
        min_importance: float = 0.0,
    ) -> List[str]:
        """Extract feature names from a training run's results_json."""
        if results_json is None:
            return []

        imp_list: List[Dict[str, Any]] = []

        if isinstance(results_json, dict) and "results" in results_json:
            for res in reversed(results_json["results"]):
                fi = res.get("feature_importance", [])
                if fi:
                    imp_list = fi
                    break
        elif isinstance(results_json, dict) and "feature_importance" in results_json:
            imp_list = results_json["feature_importance"]
        elif isinstance(results_json, list):
            if results_json and isinstance(results_json[0], dict):
                if "feature" in results_json[0]:
                    imp_list = results_json
                elif "feature_importance" in results_json[0]:
                    imp_list = results_json[-1].get("feature_importance", [])

        if not imp_list:
            logger.info("No feature_importance found in results_json")
            return []

        raw: List[Tuple[float, str]] = []
        for item in imp_list:
            if isinstance(item, dict) and "feature" in item:
                imp = float(item.get("importance", 0.0) or 0.0)
                if imp >= min_importance:
                    raw.append((imp, item["feature"]))

        raw.sort(key=lambda x: -x[0])

        seen: set[str] = set()
        result: List[str] = []
        for _imp_val, feat in raw:
            if feat not in seen:
                seen.add(feat)
                result.append(feat)

        logger.info(
            "Extracted %d features (min_importance=%.4f)", len(result), min_importance
        )
        return result

    # ── Internal ─────────────────────────────────────────────────────────────

    def _build_features(self, df: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """Apply module-level feature engineering and order columns."""
        kwargs.setdefault("sparse_inference", getattr(self, "_sparse_inference", False))

        # ── Sparse-inference context padding ──────────────────────────────────────
        # When only a handful of games are loaded (admin inspection / batch picks),
        # every schedule-derived feature (_venue_lookup split-blend, team_games
        # rest/travel/fatigue) is computed by counting the team's PRIOR games inside
        # `df`.  With n_prev=0 the venue-blend collapses to last-season anchors, and
        # rest/travel go missing — producing values that do NOT match training (which
        # loads the full season).  To make admin == training == live inference, pad
        # `df` with each team's recent through-date games FIRST, run the whole feature
        # pipeline over the padded set, then slice the result back to only the
        # originally-requested game_ids.  Padding is strictly chronological BEFORE the
        # earliest requested game, so no leakage.  Training (full-season) is untouched.
        if kwargs.get("sparse_inference", False) and len(df) > 0:
            requested = list(getattr(self, "_inference_requested", None) or [])
            if not requested:
                requested = sorted(int(x) for x in df["game_id"].unique().tolist())
            try:
                _min_dt = pd.to_datetime(df["date"]).min().strftime("%Y-%m-%d")
                # Context must support BOTH engines and match TRAINING's scope exactly:
                #  - Training loads REG-only games across all seasons
                #    (load_data(seasons=[...], game_types=['REG'])), so `team_games`
                #    rest_days/fatigue uses the prior REGULAR-SEASON game (shift(1))
                #    and _venue_lookup counts REG-only venue splits.
                #  - To be pixel-identical, pad with the SAME scope: REG games only,
                #    any season, strictly before the earliest requested date.  This
                #    correctly captures the season-boundary prior REG game (e.g. season
                #    34's finale is the prior REG game for a season-35 opener).
                _teams = sorted({
                    int(x) for x in
                    list(df["home_team_id"]) + list(df["away_team_id"])
                })
                _tl = ", ".join(str(t) for t in _teams)
                with create_engine(DEFAULT_DB_URL).connect() as _ctx_conn:
                    _ctx = pd.read_sql(f"""
                        SELECT g.id AS game_id, g.season_id, g.date,
                               g.home_team_id, g.away_team_id,
                               g.home_score, g.away_score, g.game_type, g.status,
                               ht.abbreviation AS home_abbr,
                               at.abbreviation AS away_abbr
                        FROM nba.games g
                        JOIN nba.teams ht ON ht.id = g.home_team_id
                        JOIN nba.teams at ON at.id = g.away_team_id
                        WHERE (g.home_team_id IN ({_tl}) OR g.away_team_id IN ({_tl}))
                          AND g.date::date < DATE '{_min_dt}'
                          AND g.game_type = 'REG'
                          AND g.status = 'FINAL'
                        ORDER BY g.date DESC
                    """, _ctx_conn)
                if len(_ctx):
                    # Bound rows: a full REG season is <=82 games/team; keep a little
                    # margin so both the season-boundary prior REG game and a full
                    # season of venue history are present.
                    _keep = set()
                    for _t in _teams:
                        _sub = _ctx[(_ctx["home_team_id"] == _t) |
                                    (_ctx["away_team_id"] == _t)]
                        _keep |= set(_sub.nlargest(95, "date")["game_id"].tolist())
                    _ctx = _ctx[_ctx["game_id"].isin(_keep)]
                    # avoid dup if a prior game is already in df
                    _have = set(df["game_id"].tolist())
                    _ctx = _ctx[~_ctx["game_id"].isin(_have)]
                    if len(_ctx):
                        df = pd.concat([df, _ctx], ignore_index=True)
                        self._inference_requested = requested
            except Exception as _e:
                print(f"[data_loader] df context padding skipped: {_e}")

        df = build_features(df, **kwargs)

        # Slice back to only the originally-requested games (context padding rows
        # must never leak into the feature matrix / predictions).
        if kwargs.get("sparse_inference", False) and len(df) > 0:
            requested = list(getattr(self, "_inference_requested", None) or [])
            if requested and "game_id" in df.columns:
                df = df[df["game_id"].isin(requested)].copy()

        known = set(self._catalog.keys())
        keep = [c for c in df.columns if c in known]
        # Add the non-trainable prior-season + raw display columns produced by
        # build_features(). They are NOT in the catalogs (so they never become
        # model features — trainability is gated by nba.features.is_trainable), but
        # they must survive here so the pick card and admin loader can read them.
        keep += [
            c for c in df.columns
            if c.startswith(("h_prior_", "a_prior_")) or c.endswith("_raw")
        ]
        # The catalog already registers most `*_raw` display columns, so the
        # `*_raw` keep above can re-add them -> `df[keep]` returned DUPLICATE
        # columns. A duplicate column makes `row["x_raw"]` resolve to a 2-element
        # pandas Series instead of a scalar; that Series repr strings into
        # features_json as garbage like "x_raw 3.1 x_raw 3.1 Name: 23222".
        # Dedupe while preserving order:
        keep = list(dict.fromkeys(keep))
        return df[keep].copy()


# ── Module-level: feature engineering ─────────────────────────────────────────


def build_features(df: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
    """NBA feature engineering — computes all features from ``nba.features``.

    Mirrors the NFL ``build_features()`` pattern.  Computes:

    *   Opponent-adjusted scoring (10- and 20-game windows)
    *   Rest days and back-to-back flags
    *   Travel miles (haversine)
    *   Betting market features (implied probability, spread movement, mismatch)
    *   Form & streaks (ATS, straight-up wins, cover margins)
    *   Split-into-home/away halves

    Parameters
    ----------
    df :
        Raw game data from ``load_games()``.
    **kwargs :
        Unused; accepted for API compatibility.

    Returns
    -------
    DataFrame with all computed features.
    """
    df = df.copy()

    # Normalise column names
    df.columns = [c.lower() for c in df.columns]

    # Ensure date is datetime
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])

    # ═══════════════════════════════════════════════════════════════════════════
    #  Split into home / away halves for team-level rolling computations
    # ═══════════════════════════════════════════════════════════════════════════

    # Alias spread column for readability in the feature code
    df["spread"] = df["closing_spread"]

    home_cols = {
        "game_id": "game_id",
        "season_id": "season_id",
        "home_team_id": "team_id",
        "home_abbr": "team_abbr",
        "home_team": "team",
        "away_team_id": "opp_id",
        "away_abbr": "opp_abbr",
        "home_score": "score_for",
        "away_score": "score_against",
        "spread": "spread",
        "home_moneyline": "moneyline",
        "spread_home_odds": "spread_odds",
        "over_odds": "over_odds",
        "under_odds": "under_odds",
        # ── Box score for ORTG/DRTG/Pace ──────────────────────────────
        "home_field_goals_made": "fgm",
        "home_field_goals_attempted": "fga",
        "home_three_points_made": "fgm3",
        "home_three_points_attempted": "fga3",
        "home_free_throws_made": "ftm",
        "home_free_throws_attempted": "fta",
        "home_rebounds": "reb",
        "home_assists": "ast",
        "home_steals": "stl",
        "home_blocks": "blk",
        "home_fouls": "pf",
        # ── Opponent box score (for DRTG) ─────────────────────────────
        "away_field_goals_made": "opp_fgm",
        "away_field_goals_attempted": "opp_fga",
        "away_free_throws_attempted": "opp_fta",
        "away_rebounds": "opp_reb",
        # ── Cumulative stats (pre-computed, season-to-date) ───────────
        "h_cum_ppg": "cum_ppg",
        "h_cum_oppg": "cum_oppg",
        "h_cum_margin_pg": "cum_margin_pg",
        "h_cum_fg_pct": "cum_fg_pct",
        "h_cum_fg3_pct": "cum_fg3_pct",
        "h_cum_ft_pct": "cum_ft_pct",
        "h_cum_reb_pg": "cum_reb_pg",
        "h_cum_ast_pg": "cum_ast_pg",
        "h_cum_stl_pg": "cum_stl_pg",
        "h_cum_blk_pg": "cum_blk_pg",
        "h_cum_tov_pg": "cum_tov_pg",
        "h_cum_pf_pg": "cum_pf_pg",
        "h_cum_ortg": "cum_ortg",
        "h_cum_drtg": "cum_drtg",
        "h_cum_net_ortg": "cum_net_ortg",
        "h_cum_pace": "cum_pace",
        "h_cum_efg_pct": "cum_efg_pct",
        "h_cum_opp_efg_pct": "cum_opp_efg_pct",
        "h_cum_tov_rate": "cum_tov_rate",
        "h_cum_opp_tov_rate": "cum_opp_tov_rate",
        "h_cum_ft_rate": "cum_ft_rate",
        "h_cum_3pa_rate": "cum_3pa_rate",
        "h_cum_ast_ratio": "cum_ast_ratio",
        "h_cum_stl_rate": "cum_stl_rate",
        "h_cum_blk_rate": "cum_blk_rate",
        "h_cum_adj_ortg": "cum_adj_ortg",
        "h_cum_adj_drtg": "cum_adj_drtg",
        "h_sos": "cum_sos",
        "h_games_played": "games_played",

        # Tier 4: Momentum & recency
        "h_rw3_ppg": "rw3_ppg",
        "h_rw5_ppg": "rw5_ppg",
        "h_rw3_net_rtg": "rw3_net_rtg",
        "h_rw5_net_rtg": "rw5_net_rtg",
        "h_rw3_efg_pct": "rw3_efg_pct",
        "h_rw5_efg_pct": "rw5_efg_pct",
        "h_rw3_drtg": "rw3_drtg",
        "h_rw5_drtg": "rw5_drtg",
        "h_cv10_ppg": "cv10_ppg",
        "h_cv20_ppg": "cv20_ppg",
        "h_cv10_net_rtg": "cv10_net_rtg",
        "h_recency_ppg": "recency_ppg",
        "h_recency_net_rtg": "recency_net_rtg",
        "h_net_rtg_r5": "net_rtg_r5",
        "h_net_rtg_r10": "net_rtg_r10",
        "h_ortg_r5": "ortg_r5",
        "h_ortg_r10": "ortg_r10",
        "h_drtg_r5": "drtg_r5",
        "h_drtg_r10": "drtg_r10",
        "h_efg_r5": "efg_r5",
        "h_efg_r10": "efg_r10",
        "h_pace_r5": "pace_r5",
        "h_pace_r10": "pace_r10",
        "h_ast_ratio_r5": "ast_ratio_r5",
        "h_ast_ratio_r10": "ast_ratio_r10",
        "h_ft_rate_r5": "ft_rate_r5",
        "h_ft_rate_r10": "ft_rate_r10",
        "h_threep_rate_r5": "threep_rate_r5",
        "h_threep_rate_r10": "threep_rate_r10",
        "h_ats_margin_5": "ats_margin_5",
        "h_ats_margin_10": "ats_margin_10",
        "h_ats_wins_5": "ats_wins_5",
        "h_ats_wins_10": "ats_wins_10",
        "h_ou_wins_5": "ou_wins_5",
        "h_ou_wins_10": "ou_wins_10",
        "h_ou_margin_5": "ou_margin_5",
        "h_wins_5": "wins_5",
        "h_wins_10": "wins_10",
        "h_adj_off_10": "adj_off_10",
        "h_adj_def_10": "adj_def_10",
        "h_star_ppg_5": "star_ppg_5",
        "h_star1_ppg_5": "star1_ppg_5",
        "h_cum_win_pct": "cum_win_pct",
        "h_home_pts_r10": "venue_pts_r10",
        "h_home_win_pct_r10": "venue_win_pct_r10",
        "h_home_win_pct_season": "venue_win_pct_season",
    }
    away_cols = {
        "game_id": "game_id",
        "season_id": "season_id",
        "away_team_id": "team_id",
        "away_abbr": "team_abbr",
        "away_team": "team",
        "home_team_id": "opp_id",
        "home_abbr": "opp_abbr",
        "away_score": "score_for",
        "home_score": "score_against",
        "spread": "spread",
        "away_moneyline": "moneyline",
        "spread_away_odds": "spread_odds",
        "over_odds": "over_odds",
        "under_odds": "under_odds",
        # ── Box score for ORTG/DRTG/Pace ──────────────────────────────
        "away_field_goals_made": "fgm",
        "away_field_goals_attempted": "fga",
        "away_three_points_made": "fgm3",
        "away_three_points_attempted": "fga3",
        "away_free_throws_made": "ftm",
        "away_free_throws_attempted": "fta",
        "away_rebounds": "reb",
        "away_assists": "ast",
        "away_steals": "stl",
        "away_blocks": "blk",
        "away_fouls": "pf",
        # ── Opponent box score (for DRTG) ─────────────────────────────
        "home_field_goals_made": "opp_fgm",
        "home_field_goals_attempted": "opp_fga",
        "home_free_throws_attempted": "opp_fta",
        "home_rebounds": "opp_reb",
        # ── Cumulative stats (pre-computed, season-to-date) ───────────
        "a_cum_ppg": "cum_ppg",
        "a_cum_oppg": "cum_oppg",
        "a_cum_margin_pg": "cum_margin_pg",
        "a_cum_fg_pct": "cum_fg_pct",
        "a_cum_fg3_pct": "cum_fg3_pct",
        "a_cum_ft_pct": "cum_ft_pct",
        "a_cum_reb_pg": "cum_reb_pg",
        "a_cum_ast_pg": "cum_ast_pg",
        "a_cum_stl_pg": "cum_stl_pg",
        "a_cum_blk_pg": "cum_blk_pg",
        "a_cum_tov_pg": "cum_tov_pg",
        "a_cum_pf_pg": "cum_pf_pg",
        "a_cum_ortg": "cum_ortg",
        "a_cum_drtg": "cum_drtg",
        "a_cum_net_ortg": "cum_net_ortg",
        "a_cum_pace": "cum_pace",
        "a_cum_efg_pct": "cum_efg_pct",
        "a_cum_opp_efg_pct": "cum_opp_efg_pct",
        "a_cum_tov_rate": "cum_tov_rate",
        "a_cum_opp_tov_rate": "cum_opp_tov_rate",
        "a_cum_ft_rate": "cum_ft_rate",
        "a_cum_3pa_rate": "cum_3pa_rate",
        "a_cum_ast_ratio": "cum_ast_ratio",
        "a_cum_stl_rate": "cum_stl_rate",
        "a_cum_blk_rate": "cum_blk_rate",
        "a_cum_adj_ortg": "cum_adj_ortg",
        "a_cum_adj_drtg": "cum_adj_drtg",
        "a_sos": "cum_sos",
        "a_games_played": "games_played",

        # Tier 4: Momentum & recency
        "a_rw3_ppg": "rw3_ppg",
        "a_rw5_ppg": "rw5_ppg",
        "a_rw3_net_rtg": "rw3_net_rtg",
        "a_rw5_net_rtg": "rw5_net_rtg",
        "a_rw3_efg_pct": "rw3_efg_pct",
        "a_rw5_efg_pct": "rw5_efg_pct",
        "a_rw3_drtg": "rw3_drtg",
        "a_rw5_drtg": "rw5_drtg",
        "a_cv10_ppg": "cv10_ppg",
        "a_cv20_ppg": "cv20_ppg",
        "a_cv10_net_rtg": "cv10_net_rtg",
        "a_recency_ppg": "recency_ppg",
        "a_recency_net_rtg": "recency_net_rtg",
        "a_net_rtg_r5": "net_rtg_r5",
        "a_net_rtg_r10": "net_rtg_r10",
        "a_ortg_r5": "ortg_r5",
        "a_ortg_r10": "ortg_r10",
        "a_drtg_r5": "drtg_r5",
        "a_drtg_r10": "drtg_r10",
        "a_efg_r5": "efg_r5",
        "a_efg_r10": "efg_r10",
        "a_pace_r5": "pace_r5",
        "a_pace_r10": "pace_r10",
        "a_ast_ratio_r5": "ast_ratio_r5",
        "a_ast_ratio_r10": "ast_ratio_r10",
        "a_ft_rate_r5": "ft_rate_r5",
        "a_ft_rate_r10": "ft_rate_r10",
        "a_threep_rate_r5": "threep_rate_r5",
        "a_threep_rate_r10": "threep_rate_r10",
        "a_ats_margin_5": "ats_margin_5",
        "a_ats_margin_10": "ats_margin_10",
        "a_ats_wins_5": "ats_wins_5",
        "a_ats_wins_10": "ats_wins_10",
        "a_ou_wins_5": "ou_wins_5",
        "a_ou_wins_10": "ou_wins_10",
        "a_ou_margin_5": "ou_margin_5",
        "a_wins_5": "wins_5",
        "a_wins_10": "wins_10",
        "a_adj_off_10": "adj_off_10",
        "a_adj_def_10": "adj_def_10",
        "a_star_ppg_5": "star_ppg_5",
        "a_star1_ppg_5": "star1_ppg_5",
        "a_cum_win_pct": "cum_win_pct",
        "a_away_pts_r10": "venue_pts_r10",
        "a_away_win_pct_r10": "venue_win_pct_r10",
        "a_away_win_pct_season": "venue_win_pct_season",
    }

    home_half = df[list(home_cols.keys())].rename(columns=home_cols).copy()

    # Build away half — invert spread (spread is from home perspective)
    away_half_raw = df[list(away_cols.keys())].copy()
    away_half_raw["spread"] = -away_half_raw["spread"]
    away_half = away_half_raw.rename(columns=away_cols)

    # Mark is_home
    home_half["is_home"] = 1
    away_half["is_home"] = 0

    # Date for sorting
    home_half["date"] = df["date"].values
    away_half["date"] = df["date"].values

    # Combine and sort
    team_games = pd.concat([home_half, away_half], ignore_index=True)
    team_games.sort_values(["team_id", "date", "game_id"], inplace=True)
    team_games.reset_index(drop=True, inplace=True)

    team_games.sort_values(["team_id", "date", "game_id"], inplace=True)
    team_games.reset_index(drop=True, inplace=True)

    # Keep a date-ordered per-team index for rolling computations
    team_games.sort_values(["team_id", "date"], inplace=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # 0. Per-game ORTG/DRTG/Pace/eFG/ft_rate/threep_rate/ast_ratio are precomputed
    #    in nba.team_rolling_stats (r5/r10, inclusive windows) and read via the
    #    GAME_QUERY LATERAL joins (prior row = entering-this-game). DB = source of truth.
    #
    # 1. Opponent-adjusted scoring (adj_off_10/adj_def_10) also precomputed in DB.
    #    No pandas recompute for these team rolling stats.

    # ═══════════════════════════════════════════════════════════════════════════
    #  1b. Rolling ORTG, DRTG, Net Rating, Pace, eFG, ft_rate, threep_rate,
    #      ast_ratio, wins (5/10) are ALL precomputed in nba.team_rolling_stats
    #      and provided by GAME_QUERY (h_*/a_* columns). No re-derivation needed.
    # ═══════════════════════════════════════════════════════════════════════════

    # ── Net Rating differential (matchup feature, home - away) ──────────────
    # ═══════════════════════════════════════════════════════════════════════════
    df["net_rtg_diff_5"] = df["h_net_rtg_r5"] - df["a_net_rtg_r5"]
    df["net_rtg_diff_10"] = df["h_net_rtg_r10"] - df["a_net_rtg_r10"]
    df["pace_diff_5"] = df["h_pace_r5"] - df["a_pace_r5"]

    # ═══════════════════════════════════════════════════════════════════════════
    #  1c. Star player availability — NOW PRECOMPUTED (perf)
    #  star_ppg_5 / star1_ppg_5 (rolling) AND the per-game availability flags
    #  (stars_active / star1_active) are all precomputed in nba.team_rolling_stats
    #  and read via GAME_QUERY (h_stars_active/h_star1_active/a_stars_active/a_star1_active).
    #  Previously this block re-joined nba.player_game_stats (all 513k rows) + ran
    #  a regex lambda on the VARCHAR minutes column every training run — removed.
    # ═══════════════════════════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════════
    #  1d. Team splits — CURRENT-SEASON through-date, blended from prior season
    #  ATS cover %, OU over %, venue scoring (home/away) for handicapping.
    #
    #  DESIGN (Rich, 2026-08-12):
    #   * These features reflect how the team is CURRENTLY playing this season
    #     (through-date), NOT just last season.
    #   * At the START of the season (few games), BLEND from the prior year's
    #     split; as the season progresses, transition to the team's actual
    #     current-season performance.
    #   * vs-conference features (vs_east/vs_west) were removed 2026-08-24 (Rich):
    #     never computed, always NULL, pick-card-only noise. Deleted from nba.features.
    #
    #  IMPLEMENTATION:
    #   * Build a per-(team,venue) game log for every game in df (only the
    #     venue the team occupies in that game: home team -> home games,
    #     away team -> away games), ordered by (season, date, game_id).
    #   * For each game, compute the team's PRIOR games at that venue this
    #     season (strictly before this game) via venue-partitioned expanding
    #     sums SHIFTED BY 1, so the current game is excluded -> no lookahead.
    #   * ATS/OU come from nba.betting_lines_consolidated (closing, fallback
    #     opening) joined per game; result margin + line => cover / over (same
    #     semantics as populate_team_rolling_stats.py).
    #   * Venue scoring is computed directly from nba.games scores (in df).
    #   * Blend weight w = min(venue_games_played / VENUE_BLEND, 1):
    #         value = (1 - w) * prior_season_split + w * current_season_split
    #     so the very first games use the prior season, and ~VENUE_BLEND
    #     games in they reflect the current season.
    # ═══════════════════════════════════════════════════════════════════════════
    if len(df) > 0:
        try:
            VENUE_BLEND = 12  # ~ a quarter of the home/away schedule

            with create_engine(DEFAULT_DB_URL).connect() as _ts_conn:
                _lines = pd.read_sql("""
                    SELECT game_id,
                           COALESCE(closing_spread, opening_spread) AS spread,
                           COALESCE(closing_ou, opening_ou)         AS ou
                    FROM nba.betting_lines_consolidated
                    WHERE game_id IN (SELECT id FROM nba.games)
                """, _ts_conn)
                _prior = pd.read_sql("""
                    SELECT team_id, season_id, split_type, ats_pct, ou_overs_pct,
                           points_for, points_against
                    FROM nba.team_splits
                    WHERE split_type IN ('home', 'away')
                """, _ts_conn)

            _lines = _lines.dropna(subset=["spread", "ou"], how="all")
            _spread = _lines.set_index("game_id")["spread"].to_dict()
            _ou = _lines.set_index("game_id")["ou"].to_dict()

            # Prior-season anchors: home split / away split per team.
            _prior_h = _prior[_prior["split_type"] == "home"].set_index(
                ["team_id", "season_id"])
            _prior_a = _prior[_prior["split_type"] == "away"].set_index(
                ["team_id", "season_id"])

            # Career anchors (season_id IS NULL) — fallback when ATS/OU prior-
            # season split is NULL (team_splits only fills ATS/OU for 30-35).
            _career_h = _prior[(_prior["split_type"] == "home") &
                               _prior["season_id"].isna()].set_index("team_id")
            _career_a = _prior[(_prior["split_type"] == "away") &
                               _prior["season_id"].isna()].set_index("team_id")


            # Build per-(team,venue) event log from df's games.
            _d = df[["game_id", "season_id", "date", "home_team_id", "away_team_id",
                     "home_score", "away_score"]].copy()
            _ev_rows = []
            for _, r in _d.iterrows():
                gid = r["game_id"]
                spread = _spread.get(gid)
                ou = _ou.get(gid)
                for side, tid, opp, pts, opp_pts in (
                    ("home", r["home_team_id"], r["away_team_id"],
                     r["home_score"], r["away_score"]),
                    ("away", r["away_team_id"], r["home_team_id"],
                     r["away_score"], r["home_score"]),
                ):
                    ats_won = None
                    if spread is not None:
                        am = (pts - opp_pts) + spread if side == "home" else (pts - opp_pts) - spread
                        ats_won = 1 if am > 0 else (0 if am < 0 else None)
                    ou_won = None
                    if ou is not None:
                        ou_won = 1 if (pts + opp_pts) > ou else (0 if (pts + opp_pts) < ou else None)
                    _ev_rows.append({
                        "game_id": gid, "season_id": r["season_id"],
                        "date": r["date"], "team_id": tid, "side": side,
                        "pts": float(pts), "opp_pts": float(opp_pts),
                        "ats_won": ats_won, "ou_won": ou_won,
                    })
            _ev = pd.DataFrame(_ev_rows, columns=[
                "game_id", "season_id", "date", "team_id", "side",
                "pts", "opp_pts", "ats_won", "ou_won"])
            _ev["date"] = pd.to_datetime(_ev["date"])

            def _venue_lookup(_side):
                """Through-date (prior-games-only) venue splits for one side."""
                _g = _ev[_ev["side"] == _side].sort_values(
                    ["season_id", "team_id", "date", "game_id"]).copy()
                _g["genkey"] = _g["season_id"].astype(str) + "_" + _g["team_id"].astype(str)
                _count = _g.groupby("genkey").cumcount() + 1  # 1-based games played at this venue this season
                _g["n_prev"] = _count  # after we shift, becomes prior count
                _g["ats_c"] = _g["ats_won"].fillna(0.0).groupby(_g["genkey"]).cumsum()
                _g["ou_c"] = _g["ou_won"].fillna(0.0).groupby(_g["genkey"]).cumsum()
                _g["pf_c"] = _g["pts"].groupby(_g["genkey"]).cumsum()
                _g["pa_c"] = _g["opp_pts"].groupby(_g["genkey"]).cumsum()
                # Shift within venue+season -> exclude the current game.
                _g["n_prev"] = _g.groupby("genkey")["n_prev"].shift(1).fillna(0)
                _g["ats_p"] = _g.groupby("genkey")["ats_c"].shift(1).fillna(0)
                _g["ou_p"] = _g.groupby("genkey")["ou_c"].shift(1).fillna(0)
                _g["pf_p"] = _g.groupby("genkey")["pf_c"].shift(1).fillna(0)
                _g["pa_p"] = _g.groupby("genkey")["pa_c"].shift(1).fillna(0)
                _g["w"] = (_g["n_prev"] / VENUE_BLEND).clip(upper=1.0)
                _g.set_index("game_id", inplace=True)
                _g["ats_r"] = _g["ats_p"] / _g["n_prev"].where(_g["n_prev"] > 0)
                _g["ou_r"] = _g["ou_p"] / _g["n_prev"].where(_g["n_prev"] > 0)
                _g["pf_v"] = _g["pf_p"] / _g["n_prev"].where(_g["n_prev"] > 0)
                _g["pa_v"] = _g["pa_p"] / _g["n_prev"].where(_g["n_prev"] > 0)
                return _g[["n_prev", "w", "ats_r", "ou_r", "pf_v", "pa_v",
                          "season_id", "team_id"]]

            def _blend(_g, dcol, stat, anchor_df, anchor_col, side_team, career_df=None):
                """Blend current through-date toward prior-season split.

                When the prior-season (season-1) split is missing (NaN), falls
                back to the career split (career_df) when available.
                """
                cur = df["game_id"].map(_g[stat])
                wt = df["game_id"].map(_g["w"]).fillna(0.0)
                # prior anchor per row: (team_id, season_id - 1)
                teams = df["home_team_id"] if side_team == "home" else df["away_team_id"]
                prior = df["season_id"] - 1
                if anchor_df is not None and anchor_col in anchor_df.columns:
                    # vectorized prior-season anchor via merge (handles duplicate team/season keys)
                    _a = anchor_df.reset_index()
                    _a_cols = [c for c in _a.columns if c in ("team_id", "season_id")]
                    _m = pd.DataFrame({
                        "_t": teams.values,
                        "_s": prior.values,
                        "_o": np.arange(len(df)),
                    }).merge(
                        _a[["team_id", "season_id", anchor_col]].rename(columns={"team_id": "_t", "season_id": "_s"}),
                        on=["_t", "_s"], how="left",
                    )
                    anc = _m.sort_values("_o").set_index("_o")[anchor_col]
                    anc.index = df.index
                else:
                    anc = pd.Series(np.nan, index=df.index)
                if career_df is not None and anchor_col in career_df.columns:
                    # where anchor missing, fall back to career split (per-team)
                    miss = anc.isna()
                    if miss.any():
                        _car = career_df.loc[teams.values[miss.values], [anchor_col]]
                        _car.index = df.index[miss.values]
                        anc = anc.copy()
                        anc.loc[miss] = _car[anchor_col].values
                blended = anc * (1.0 - wt) + cur * wt
                blended = blended.where(anc.notna(), cur)  # no anchor -> pure current
                blended = blended.where(cur.notna(), anc)  # no current-season games yet -> pure anchor
                df[dcol] = blended

            _h = _venue_lookup("home")
            _a = _venue_lookup("away")

            _blend(_h, "h_ats_pct_home", "ats_r", _prior_h, "ats_pct", "home", _career_h)
            _blend(_h, "h_ou_over_pct_home", "ou_r", _prior_h, "ou_overs_pct", "home", _career_h)
            _blend(_h, "h_pts_home", "pf_v", _prior_h, "points_for", "home", _career_h)
            _blend(_h, "h_pts_against_home", "pa_v", _prior_h, "points_against", "home", _career_h)

            _blend(_a, "a_ats_pct_away", "ats_r", _prior_a, "ats_pct", "away", _career_a)
            _blend(_a, "a_ou_over_pct_away", "ou_r", _prior_a, "ou_overs_pct", "away", _career_a)
            _blend(_a, "a_pts_away", "pf_v", _prior_a, "points_for", "away", _career_a)
            _blend(_a, "a_pts_against_away", "pa_v", _prior_a, "points_against", "away", _career_a)
        except Exception as _ts_err:  # noqa: BLE001
            print(f"[data_loader] team_splits through-date features skipped: {_ts_err}")

    # ═══════════════════════════════════════════════════════════════════════════
    #  2. Rest days & back-to-back
    # ═══════════════════════════════════════════════════════════════════════════

    team_games["prev_date"] = team_games.groupby("team_abbr")["date"].shift(1)
    team_games["rest_days"] = (
        team_games["date"] - team_games["prev_date"]
    ).dt.days
    team_games["b2b"] = (team_games["rest_days"] == 1).astype(int)

    _hl2 = team_games[team_games["is_home"] == 1].set_index("game_id")
    _al2 = team_games[team_games["is_home"] == 0].set_index("game_id")
    df["rest_h"] = df["game_id"].map(_hl2["rest_days"])
    df["rest_a"] = df["game_id"].map(_al2["rest_days"])
    df["home_b2b"] = df["game_id"].map(_hl2["b2b"])
    df["away_b2b"] = df["game_id"].map(_al2["b2b"])

    df["rest_diff"] = df["rest_h"] - df["rest_a"]
    df["rest_diff"] = df["rest_diff"].fillna(0)

    # ── Enhanced fatigue: 3-in-4, 4-in-5, 5-in-8 ────────────────────
    # Rolling date windows require DatetimeIndex; build index per team group
    def _schedule_density_values(grp, window_days: int, threshold: int):
        """
        For a team's sorted games, check if current game has >= threshold games
        within the last window_days (counting current). Returns bool array.
        """
        srt = grp.set_index("date").sort_index()
        cnt = srt.index.to_series().rolling(f"{window_days}D", min_periods=1).count()
        return (cnt >= threshold).astype(int).values

    for team_abbr, grp in team_games.sort_values("date").groupby("team_abbr", sort=False):
        idx = grp.index
        team_games.loc[idx, "three_in_four"] = _schedule_density_values(grp, 4, 3)
        team_games.loc[idx, "four_in_five"] = _schedule_density_values(grp, 5, 4)
        team_games.loc[idx, "five_in_eight"] = _schedule_density_values(grp, 8, 5)

    _hl3 = team_games[team_games["is_home"] == 1].set_index("game_id")
    _al3 = team_games[team_games["is_home"] == 0].set_index("game_id")
    df["h_three_in_four"] = df["game_id"].map(_hl3["three_in_four"])
    df["a_three_in_four"] = df["game_id"].map(_al3["three_in_four"])
    df["h_four_in_five"] = df["game_id"].map(_hl3["four_in_five"])
    df["a_four_in_five"] = df["game_id"].map(_al3["four_in_five"])
    df["h_five_in_eight"] = df["game_id"].map(_hl3["five_in_eight"])
    df["a_five_in_eight"] = df["game_id"].map(_al3["five_in_eight"])

    # ═══════════════════════════════════════════════════════════════════════════
    #  3. Travel miles (haversine)
    # ═══════════════════════════════════════════════════════════════════════════

    team_games["lat"] = team_games["team_abbr"].map(
        lambda abbr: _location_cache.get(abbr, (0, 0))[0]
    )
    team_games["lon"] = team_games["team_abbr"].map(
        lambda abbr: _location_cache.get(abbr, (0, 0))[1]
    )
    # Game venue: home game uses team's own city, away game uses opponent's city
    team_games["venue_lat"] = np.where(
        team_games["is_home"] == 1,
        team_games["lat"],
        team_games["opp_abbr"].map(lambda abbr: _location_cache.get(abbr, (0, 0))[0]),
    )
    team_games["venue_lon"] = np.where(
        team_games["is_home"] == 1,
        team_games["lon"],
        team_games["opp_abbr"].map(lambda abbr: _location_cache.get(abbr, (0, 0))[1]),
    )

    team_games["prev_venue_lat"] = team_games.groupby("team_abbr")["venue_lat"].shift(1)
    team_games["prev_venue_lon"] = team_games.groupby("team_abbr")["venue_lon"].shift(1)

    team_games["team_travel"] = team_games.apply(
        lambda r: haversine_miles(
            r["prev_venue_lat"], r["prev_venue_lon"],
            r["venue_lat"], r["venue_lon"],
        )
        if pd.notna(r["prev_venue_lat"])
        else 0.0,
        axis=1,
    )

    team_games["away_travel"] = np.where(
        team_games["is_home"] == 0, team_games["team_travel"], 0.0
    )

    away_games = team_games[team_games["is_home"] == 0][["game_id", "away_travel"]]
    df = df.merge(away_games, on="game_id", how="left")
    df["travel_miles"] = df["away_travel"].fillna(0.0)
    df.drop(columns=["away_travel"], inplace=True, errors="ignore")

    # ═══════════════════════════════════════════════════════════════════════════
    #  4. Betting market features
    # ═══════════════════════════════════════════════════════════════════════════

    df["spread_movement"] = df["opening_spread"] - df["closing_spread"]
    df["ou_movement"] = df["closing_ou"] - df["opening_ou"]

    # ── Moneyline movement: closing - opening, from the HOME team's perspective ──
    # Raw American-odds points (mirrors MLB's ml_movement). home_moneyline /
    # away_moneyline ARE the current (closing) values. Sign: a POSITIVE value
    # means the closing home ML is less negative / more positive than opening
    # (book pushed odds TOWARD the home team). Also add the away-side point-change
    # and an implied-probability movement so the model can read both raw-point and
    # probability-scale shifts.
    df["ml_movement"] = df["home_moneyline"] - df["opening_home_ml"]
    df["away_ml_movement"] = df["away_moneyline"] - df["opening_away_ml"]

    def _implied_prob(moneyline: pd.Series) -> pd.Series:
        """Convert American moneyline odds to implied probability."""
        moneyline = moneyline.astype(float)
        result = pd.Series(np.nan, index=moneyline.index)
        pos_mask = moneyline > 0
        neg_mask = moneyline < 0
        result.loc[pos_mask] = 100.0 / (moneyline.loc[pos_mask] + 100.0)
        result.loc[neg_mask] = -moneyline.loc[neg_mask] / (
            -moneyline.loc[neg_mask] + 100.0
        )
        return result

    df["h_implied"] = _implied_prob(df["home_moneyline"])
    df["a_implied"] = _implied_prob(df["away_moneyline"])

    # Opening implied probability + implied-probability movement (closing - opening,
    # home side). Mirrors MLB's ml_implied_movement. Feature survives as 0.0 when
    # either side is missing so the model isn't poisoned with NaN gaps.
    df["h_open_implied"] = _implied_prob(df["opening_home_ml"])
    df["a_open_implied"] = _implied_prob(df["opening_away_ml"])
    df["ml_implied_movement"] = (df["h_implied"] - df["h_open_implied"]).fillna(0.0)

    # ── Spread-odds (juice) movement + combined market move ──────────────────
    # spread_movement measures the line move in POINTS; the juice on that spread
    # can also move independently (e.g. line anchors at -5.5 but the price goes
    # -110 → -130). Both are responses to the SAME money flow / sharp-vs-public
    # imbalance, so we convert both to HOME-COVER PROBABILITY units and combine
    # them into one coherent signal: market_move_home.
    #
    #   spread_movement_implied  = spread_movement / K  (K≈14.0 pts per prob-unit,
    #                               same calibration as implied_margin).
    #   juice_movement_implied   = vig-free home cover prob(closing) -
    #                              vig-free home cover prob(opening).
    #   market_move_home         = sum of the two. Positive → market moved toward
    #                              the HOME team covering. Line & juice in the same
    #                              direction compound the signal; conflicting
    #                              movement (reverse line movement) partially
    #                              cancels, which is itself a signal.
    #
    # Data caveat: seasons 17-29 have opening==closing for both line and juice
    # (historical backfill limitation), so these are 0.0 there and only carry real
    # variance for 2020-21 onward.
    df["spread_movement_implied"] = df["spread_movement"] / 14.0

    def _vigfree(home_odds: pd.Series, away_odds: pd.Series) -> pd.Series:
        """Normalize two-side juice into a home-cover probability (vig removed)."""
        hp = _implied_prob(home_odds)
        ap = _implied_prob(away_odds)
        denom = hp + ap
        return (hp / denom.replace(0.0, np.nan))

    open_home_cover = _vigfree(
        df["opening_spread_home_odds"], df["opening_spread_away_odds"]
    )
    close_home_cover = _vigfree(df["spread_home_odds"], df["spread_away_odds"])
    df["juice_movement_implied"] = (close_home_cover - open_home_cover).fillna(0.0)

    df["market_move_home"] = (
        df["spread_movement_implied"] + df["juice_movement_implied"]
    ).fillna(0.0)

    # ── Implied point margin from ML win-probability edge ────────────────────
    # implied_margin = (h_implied - a_implied) * K, in points, positive = home
    # favored. K is calibrated empirically on 23.7k NBA games (2026-08-16):
    #   |closing_spread| ≈ K * |h_implied - a_implied|,   K ≈ 14.0
    # OLS through the origin, R²≈0.96 (spread is ~linear in the ML edge), stable
    # across seasons (13.0–16.0). This puts implied_margin on the SAME scale as
    # closing_spread so ml_spread_mismatch below is a real point-vs-point signal.
    # (Formerly a magic *50.0 scale — uncalibrated.)
    _IMPLIED_MARGIN_K = 14.0
    df["implied_margin"] = (df["h_implied"] - df["a_implied"]) * _IMPLIED_MARGIN_K

    # ── Implied team scores from closing total + closing spread ───────────────
    # home_implied = (OU - |spread|)/2 ; away_implied = (OU + |spread|)/2
    # (spread quoted from home perspective). Fall back to splitting the OU
    # evenly when the spread is unavailable.
    _ou = df["closing_ou"].where(df["closing_ou"].notna(), df.get("opening_ou"))
    _spr = df["closing_spread"].where(df["closing_spread"].notna(), df.get("opening_spread")).abs()
    _spr = _spr.where(_spr.notna(), 0.0)
    _implied_base = _ou / 2.0
    df["h_implied_score"] = _implied_base - _spr / 2.0
    df["a_implied_score"] = _implied_base + _spr / 2.0

    df["ml_spread_mismatch"] = df["implied_margin"] - df["closing_spread"].abs()

    # ── Over/under implied probability (vig-free) ────────────────────────────
    _over_ip = _implied_prob(df["over_odds"])
    _under_ip = _implied_prob(df["under_odds"])
    df["over_implied_prob"] = _over_ip / (_over_ip + _under_ip)

    # ═══════════════════════════════════════════════════════════════════════════
    #  5. Form & streaks (per-game ATS / OU actuals only)
    #     Rolling ATS/OU/win counts (ats_wins_5/10, ats_margin_5/10, ou_wins_5/10,
    #     ou_margin_5, wins_5/10) are precomputed in nba.team_rolling_stats and already
    #     loaded via the GAME_QUERY LATERAL joins — read directly from DB, no pandas.
    # ═══════════════════════════════════════════════════════════════════════════

    df["home_actual_margin"] = df["home_score"] - df["away_score"]
    df["home_ats_cover"] = (
        df["home_actual_margin"] > -df["closing_spread"]
    ).astype(int)
    df["home_ats_margin"] = df["home_actual_margin"] - (-df["closing_spread"])

    df["away_ats_cover"] = (
        -df["home_actual_margin"] > df["closing_spread"]
    ).astype(int)
    df["away_ats_margin"] = -df["home_actual_margin"] - df["closing_spread"]

    df.sort_values(["game_id"], inplace=True)


    # ── Over/under result ────────────────────────────────────────────────
    df["over_result"] = (
        (df["home_score"] + df["away_score"]) > df["closing_ou"]
    ).astype(float)

    # ── OU per-game actuals (rolling records are DB-sourced) ─────────────
    df["ou_total"] = df["home_score"] + df["away_score"]
    df["ou_margin"] = df["ou_total"] - df["closing_ou"]


    # ═══════════════════════════════════════════════════════════════════════════
    #  6. NaN handling — two-path (mirrors MLB/NFL): the MODEL imputes a
    #     reasoned prior via _impute_feature; the PICK CARD blanks a missing
    #     value. We therefore do NOT blanket fillna(0) here — a missing stat
    #     must stay NaN so the card never shows a fabricated 0 (e.g. '0 PPG').
    # ═══════════════════════════════════════════════════════════════════════════

    # ── Prior-season weighted blend (MLB-style w/ game 1-8 decay) ──
    # The raw in-season value stays in the trainable column (h_cum_ppg, ...)
    # UNLESS the current sample is NULL/small, in which case we blend with the
    # prior full-season value from nba.prior_team_stats (via *_prior_* cols).
    #   prior_w = 1 - min(games_played / BLEND_WINDOW, 1)
    # so game 1 (games_played=0) is ~100% prior, decaying to ~0% prior by
    # game 9+ (>=8 games played).  We also emit parallel *_raw columns holding
    # the raw in-season value (falling back to prior when in-season is null) so
    # the PICK CARD can always show the real underlying stat.
    #
    # The MODEL reads the blended h_cum_ppg (trainable, is_trainable=TRUE).
    # The PICK CARD reads h_cum_ppg_raw (is_trainable=FALSE, pick_card=TRUE).
    BLEND_WINDOW = 8

    def _prior_weight(games_played):
        if games_played is None or pd.isna(games_played):
            return 1.0  # no in-season sample -> fully prior
        return float(np.clip(1.0 - games_played / BLEND_WINDOW, 0.0, 1.0))

    def _blend_series(train, prior, w):
        """Weighted blend, NaN-safe."""
        train = pd.to_numeric(train, errors="coerce")
        prior = pd.to_numeric(prior, errors="coerce")
        blended = w * prior + (1.0 - w) * train
        # Where train is missing, fall back to the (weighted) prior only.
        blended = blended.where(train.notna(), w * prior)
        # Where BOTH are missing, leave NaN for the model imputer / blank card.
        return blended

    # Collect all prior columns present (home + away).
    prior_cols = sorted(c for c in df.columns if c.startswith(("h_prior_", "a_prior_")))

    # Build all new raw + blended columns up front, then assign once (avoids
    # DataFrame fragmentation from one-by-one inserts).
    new_raw = {}
    new_blend = {}
    for side, gcol in (("h", "h_games_played"), ("a", "a_games_played")):
        gp = df.get(gcol)
        prefix = f"{side}_prior_"
        for pc in prior_cols:
            if not pc.startswith(prefix):
                continue
            suffix = pc[len(prefix):]
            train_col = f"{side}_{suffix}"
            if train_col not in df.columns:
                continue
            # prior weight per row (vector, since games_played varies)
            if gp is not None:
                w = gp.apply(_prior_weight)
            else:
                w = 1.0
            raw_col = f"{train_col}_raw"
            in_season_raw = pd.to_numeric(df[train_col], errors="coerce")
            prior = pd.to_numeric(df[pc], errors="coerce")
            # raw display column (pick card): raw in-season, else prior
            new_raw[raw_col] = in_season_raw.where(in_season_raw.notna(), prior)
            # blend into the trainable column
            new_blend[train_col] = _blend_series(in_season_raw, prior, w)

    if new_raw:
        df = pd.concat([df, pd.DataFrame(new_raw, index=df.index)], axis=1)
    if new_blend:
        for col, s in new_blend.items():
            df[col] = s

    # Recompute head-to-head differential features from the BLENDED rolling
    # values (they were computed earlier from the pre-blend, possibly NULL,
    # values). E.g. net_rtg_diff_5 = blended h_net_rtg_r5 - blended a_net_rtg_r5.
    for diff, hcol, acol in [
        ("net_rtg_diff_5", "h_net_rtg_r5", "a_net_rtg_r5"),
        ("net_rtg_diff_10", "h_net_rtg_r10", "a_net_rtg_r10"),
        ("pace_diff_5", "h_pace_r5", "a_pace_r5"),
    ]:
        if diff in df.columns and hcol in df.columns and acol in df.columns:
            df[diff] = pd.to_numeric(df[hcol], errors="coerce") - pd.to_numeric(
                df[acol], errors="coerce"
            )

    drop_cols = [
        c for c in df.columns
        if c.startswith("home_actual_") or c.startswith("away_ats_")
    ]
    df.drop(columns=drop_cols, inplace=True, errors="ignore")

    return df


# ── Module-level helpers ──────────────────────────────────────────────────────


def get_model_features(target: Optional[str] = None, live: bool = False) -> List[str]:
    """Return the list of trainable feature names for NBA models.

    Parameters
    ----------
    target :
        If ``'ats'``, only return features for the ATS model. If ``'ou'``,
        return features for the OU model.
    live :
        If ``True``, read the ``live_*`` flags (the live models). Default
        ``False`` reads ``current_*``. Both are kept in sync by
        ``db_training.py`` from the trained Booster's feature set.

    Returns
    -------
    Sorted list of trainable feature names.
    """
    return NBADataLoader().get_feature_columns(target=target, live=live)


# ── Singleton / factory ───────────────────────────────────────────────────────

_loader_instance: Optional[NBADataLoader] = None


def get_data_loader(db_url: Optional[str] = None,
                    ats_only: bool = False,
                    ou_only: bool = False) -> NBADataLoader:
    """Return a singleton NBADataLoader instance."""
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = NBADataLoader(db_url=db_url, ats_only=ats_only, ou_only=ou_only)
    return _loader_instance


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI smoke test
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    dl = get_data_loader()
    print(f"Feature catalog: {len(dl.get_features_catalog())} entries")
    print(f"ATS features: {dl.get_feature_columns(target='ats')}")
    print(f"All trainable: {dl.get_feature_columns()}")

    df = dl.load_games(limit=10)
    print(f"Games loaded: {len(df)} rows, {len(df.columns)} cols")
    if not df.empty:
        print(f"Columns: {list(df.columns)}")
        print(f"Date range: {df['date'].iloc[0]} to {df['date'].iloc[-1]}")

    df_feats = dl.load_data(limit=200)
    print(f"Featurized: {len(df_feats)} rows, {len(df_feats.columns)} cols")
    if not df_feats.empty:
        print(f"Feature columns: {list(df_feats.columns)}")
        nulls = df_feats.isnull().sum()
        if nulls.any():
            print(f"Nulls:\n{nulls[nulls > 0]}")
        else:
            print("No null values ✅")
