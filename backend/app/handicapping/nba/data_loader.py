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
        blc.closing_spread,
        blc.closing_ou,
        blc.closing_home_ml                   AS home_moneyline,
        blc.closing_away_ml                   AS away_moneyline,
        blc.closing_spread_home_odds          AS spread_home_odds,
        blc.closing_spread_away_odds          AS spread_away_odds,
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
        ba.closing_spread,
        ba.closing_ou,
        ba.home_moneyline,
        ba.away_moneyline,
        ba.spread_home_odds,
        ba.spread_away_odds,
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
        hrs.star_ppg_5             AS h_star_ppg_5,
        hrs.star1_ppg_5            AS h_star1_ppg_5,
        hrs.stars_active           AS h_stars_active,
        hrs.star1_active           AS h_star1_active,

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
        ars.star_ppg_5             AS a_star_ppg_5,
        ars.star1_ppg_5            AS a_star1_ppg_5,
        ars.stars_active           AS a_stars_active,
        ars.star1_active           AS a_star1_active,
        -- prior-season (previous full season) home values for blending
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
    LEFT JOIN LATERAL (
        SELECT cgs.* FROM nba.cumulative_game_stats cgs
        WHERE cgs.team_id = g.home_team_id
          AND cgs.game_id != g.id
          AND cgs.game_date < g.date::date
          AND cgs.season_id = g.season_id
        ORDER BY cgs.game_date DESC, cgs.game_id DESC
        LIMIT 1
    ) hcs ON true
    LEFT JOIN LATERAL (
        SELECT rs.* FROM nba.team_rolling_stats rs
        WHERE rs.team_id = g.home_team_id
          AND rs.game_id != g.id
          AND rs.game_date < g.date::date
          AND rs.season_id = g.season_id
        ORDER BY rs.game_date DESC, rs.game_id DESC
        LIMIT 1
    ) hrs ON true
    LEFT JOIN LATERAL (
        SELECT cgs.* FROM nba.cumulative_game_stats cgs
        WHERE cgs.team_id = g.away_team_id
          AND cgs.game_id != g.id
          AND cgs.game_date < g.date::date
          AND cgs.season_id = g.season_id
        ORDER BY cgs.game_date DESC, cgs.game_id DESC
        LIMIT 1
    ) acs ON true
    LEFT JOIN LATERAL (
        SELECT rs.* FROM nba.team_rolling_stats rs
        WHERE rs.team_id = g.away_team_id
          AND rs.game_id != g.id
          AND rs.game_date < g.date::date
          AND rs.season_id = g.season_id
        ORDER BY rs.game_date DESC, rs.game_id DESC
        LIMIT 1
    ) ars ON true
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

FEATURES_CATALOG: Dict[str, str] = {
    "spread": "Closing point spread (negative = home favorite)",
    "closing_ou": "Closing over/under total",
    "home_moneyline": "Home team moneyline odds",
    "away_moneyline": "Away team moneyline odds",
    "spread_home_odds": "Home team spread betting odds (e.g. -110)",
    "spread_away_odds": "Away team spread betting odds (e.g. -110)",
    "over_odds": "Over total betting odds (e.g. -110)",
    "under_odds": "Under total betting odds (e.g. -110)",
    "home_score": "Home team final score",
    "away_score": "Away team final score",
    "season_year": "Calendar year of the season (via nba.seasons join)",
    "season_id": "Season identifier",
    "game_id": "Unique game identifier",
    "date": "Game date",
    "home_team_id": "Home team ID",
    "away_team_id": "Away team ID",

    # ── Cumulative game stats (pre-computed, backward-looking) ────────
    "h_games_played": "Home team games played before this game in season",
    "h_cum_ppg": "Home cumulative PPG (season-to-date, excl. current)",
    "h_cum_oppg": "Home cumulative opponent PPG",
    "h_cum_margin_pg": "Home cumulative point margin per game",
    "h_cum_fg_pct": "Home cumulative FG%",
    "h_cum_fg3_pct": "Home cumulative 3P%",
    "h_cum_ft_pct": "Home cumulative FT%",
    "h_cum_reb_pg": "Home cumulative RPG",
    "h_cum_ast_pg": "Home cumulative APG",
    "h_cum_stl_pg": "Home cumulative SPG",
    "h_cum_blk_pg": "Home cumulative BPG",
    "h_cum_tov_pg": "Home cumulative TOV per game",
    "h_cum_pf_pg": "Home cumulative fouls per game",
    "h_cum_ortg": "Home cumulative offensive rating",
    "h_cum_drtg": "Home cumulative defensive rating",
    "h_cum_net_ortg": "Home cumulative net rating",
    "h_cum_pace": "Home cumulative estimated pace",
    "h_cum_efg_pct": "Home cumulative effective FG%",
    "h_cum_opp_efg_pct": "Home cumulative opponent eFG%",
    "h_cum_tov_rate": "Home cumulative turnover rate",
    "h_cum_opp_tov_rate": "Home cumulative opponent TOV rate",
    "h_cum_ft_rate": "Home cumulative free throw rate (FTA/FGA)",
    "h_cum_3pa_rate": "Home cumulative 3PA rate (3PA/FGA)",
    "h_cum_ast_ratio": "Home cumulative assist ratio (AST/FGM)",
    "h_cum_stl_rate": "Home cumulative steal rate (STL/opp_poss)",
    "h_cum_blk_rate": "Home cumulative block rate (BLK/opp_FGA)",

    "a_games_played": "Away team games played before this game in season",
    "a_cum_ppg": "Away cumulative PPG (season-to-date, excl. current)",
    "a_cum_oppg": "Away cumulative opponent PPG",
    "a_cum_margin_pg": "Away cumulative point margin per game",
    "a_cum_fg_pct": "Away cumulative FG%",
    "a_cum_fg3_pct": "Away cumulative 3P%",
    "a_cum_ft_pct": "Away cumulative FT%",
    "a_cum_reb_pg": "Away cumulative RPG",
    "a_cum_ast_pg": "Away cumulative APG",
    "a_cum_stl_pg": "Away cumulative SPG",
    "a_cum_blk_pg": "Away cumulative BPG",
    "a_cum_tov_pg": "Away cumulative TOV per game",
    "a_cum_pf_pg": "Away cumulative fouls per game",
    "a_cum_ortg": "Away cumulative offensive rating",
    "a_cum_drtg": "Away cumulative defensive rating",
    "a_cum_net_ortg": "Away cumulative net rating",
    "a_cum_pace": "Away cumulative estimated pace",
    "a_cum_efg_pct": "Away cumulative effective FG%",
    "a_cum_opp_efg_pct": "Away cumulative opponent eFG%",
    "a_cum_tov_rate": "Away cumulative turnover rate",
    "a_cum_opp_tov_rate": "Away cumulative opponent TOV rate",
    "a_cum_ft_rate": "Away cumulative free throw rate (FTA/FGA)",
    "a_cum_3pa_rate": "Away cumulative 3PA rate (3PA/FGA)",
    "a_cum_ast_ratio": "Away cumulative assist ratio (AST/FGM)",
    "a_cum_stl_rate": "Away cumulative steal rate (STL/opp_poss)",
    "a_cum_blk_rate": "Away cumulative block rate (BLK/opp_FGA)",

    # ── Tier 4: Momentum & recency ─────────────────────────────────────
    "h_rw3_ppg": "Home trailing 3-game recency-weighted PPG",
    "h_rw5_ppg": "Home trailing 5-game recency-weighted PPG",
    "h_rw3_net_rtg": "Home trailing 3-game net rating",
    "h_rw5_net_rtg": "Home trailing 5-game net rating",
    "h_rw3_efg_pct": "Home trailing 3-game eFG%",
    "h_rw5_efg_pct": "Home trailing 5-game eFG%",
    "h_rw3_drtg": "Home trailing 3-game defensive rating",
    "h_rw5_drtg": "Home trailing 5-game defensive rating",
    "h_cv10_ppg": "Home coefficient of variation PPG (last 10)",
    "h_cv20_ppg": "Home coefficient of variation PPG (last 20)",
    "h_cv10_net_rtg": "Home coefficient of variation net rating (last 10)",
    "h_recency_ppg": "Home % of PPG from last 3 games",
    "h_recency_net_rtg": "Home % of net rating from last 3 games",
    "h_cum_win_pct": "Home season-to-date win %",

    "a_rw3_ppg": "Away trailing 3-game recency-weighted PPG",
    "a_rw5_ppg": "Away trailing 5-game recency-weighted PPG",
    "a_rw3_net_rtg": "Away trailing 3-game net rating",
    "a_rw5_net_rtg": "Away trailing 5-game net rating",
    "a_rw3_efg_pct": "Away trailing 3-game eFG%",
    "a_rw5_efg_pct": "Away trailing 5-game eFG%",
    "a_rw3_drtg": "Away trailing 3-game defensive rating",
    "a_rw5_drtg": "Away trailing 5-game defensive rating",
    "a_cv10_ppg": "Away coefficient of variation PPG (last 10)",
    "a_cv20_ppg": "Away coefficient of variation PPG (last 20)",
    "a_cv10_net_rtg": "Away coefficient of variation net rating (last 10)",
    "a_recency_ppg": "Away % of PPG from last 3 games",
    "a_recency_net_rtg": "Away % of net rating from last 3 games",
    "a_cum_win_pct": "Away season-to-date win %",
}

COMPUTED_FEATURES_CATALOG: Dict[str, str] = {
    "h_adj_off_10": "Home opponent-adjusted offense, rolling 10",
    "h_adj_def_10": "Home opponent-adjusted defense, rolling 10",
    "a_adj_off_10": "Away opponent-adjusted offense, rolling 10",
    "a_adj_def_10": "Away opponent-adjusted defense, rolling 10",

    "rest_h": "Home team rest days since last game",
    "rest_a": "Away team rest days since last game",
    "rest_diff": "Rest days advantage (home - away)",
    "home_b2b": "Binary: 1 if home team on back-to-back",
    "away_b2b": "Binary: 1 if away team on back-to-back",
    "travel_miles": "Away team travel distance in miles (haversine)",
    "h_implied": "Home team implied win probability from moneyline",
    "a_implied": "Away team implied win probability from moneyline",
    "spread_movement": "Spread movement: opening - closing",
    "ou_movement": "OU movement: closing - opening",
    "over_implied_prob": "Vig-free over probability from over/under odds",
    "implied_margin": "Expected point margin from moneyline implied probability",
    "ml_spread_mismatch": "Disagreement between ML-implied margin and closing spread",
    "h_implied_score": "Implied home points from closing total + spread ((OU-|spread|)/2)",
    "a_implied_score": "Implied away points from closing total + spread ((OU+|spread|)/2)",
    "h_ats_wins_5": "Home team ATS wins in last 5 games",
    "a_ats_wins_5": "Away team ATS wins in last 5 games",
    "h_ats_margin_5": "Home team avg ATS cover margin last 5 games",
    "a_ats_margin_5": "Away team avg ATS cover margin last 5 games",
    "h_wins_5": "Home team straight-up wins in last 5 games",
    "h_wins_10": "Home team straight-up wins in last 10 games",
    "a_wins_5": "Away team straight-up wins in last 5 games",
    "a_wins_10": "Away team straight-up wins in last 10 games",
    "home_ats_cover": "Home team covered the spread (1=yes, 0=no)",
    "away_ats_cover": "Away team covered the spread (1=yes, 0=no)",
    "over_result": "Game went over the total (1=yes, 0=no)",

    # ── Enhanced fatigue ──────────────────────────────────────────────
    "h_three_in_four": "Home team has 3+ games in 4 nights",
    "a_three_in_four": "Away team has 3+ games in 4 nights",
    "h_four_in_five": "Home team has 4+ games in 5 nights",
    "a_four_in_five": "Away team has 4+ games in 5 nights",
    "h_five_in_eight": "Home team has 5+ games in 8 nights",
    "a_five_in_eight": "Away team has 5+ games in 8 nights",

    # ── OU rolling records (mirrors ATS pattern) ──────────────────────
    "h_ou_wins_5": "Home team over wins in last 5 games",
    "a_ou_wins_5": "Away team over wins in last 5 games",
    "h_ou_wins_10": "Home team over wins in last 10 games",
    "a_ou_wins_10": "Away team over wins in last 10 games",
    "h_ou_margin_5": "Home team avg OU margin (pts above/below) last 5",
    "a_ou_margin_5": "Away team avg OU margin (pts above/below) last 5",

    # ── Extended ATS windows ───────────────────────────────────────────
    "h_ats_wins_10": "Home team ATS wins in last 10 games",
    "a_ats_wins_10": "Away team ATS wins in last 10 games",
    "h_ats_margin_10": "Home team avg ATS cover margin last 10 games",
    "a_ats_margin_10": "Away team avg ATS cover margin last 10 games",

    # ── Rolling ORTG, DRTG, Net Rating, Pace ─────────────────────────–
    "h_ortg_r5": "Home team offensive rating rolling 5",
    "a_ortg_r5": "Away team offensive rating rolling 5",
    "h_ortg_r10": "Home team offensive rating rolling 10",
    "a_ortg_r10": "Away team offensive rating rolling 10",

    "h_drtg_r5": "Home team defensive rating rolling 5",
    "a_drtg_r5": "Away team defensive rating rolling 5",
    "h_drtg_r10": "Home team defensive rating rolling 10",
    "a_drtg_r10": "Away team defensive rating rolling 10",

    "h_net_rtg_r5": "Home team net rating rolling 5",
    "a_net_rtg_r5": "Away team net rating rolling 5",
    "h_net_rtg_r10": "Home team net rating rolling 10",
    "a_net_rtg_r10": "Away team net rating rolling 10",

    "h_pace_r5": "Home team pace (possessions) rolling 5",
    "a_pace_r5": "Away team pace (possessions) rolling 5",
    "h_pace_r10": "Home team pace (possessions) rolling 10",
    "a_pace_r10": "Away team pace (possessions) rolling 10",

    "net_rtg_diff_5": "Net rating differential (home - away) rolling 5",
    "net_rtg_diff_10": "Net rating differential (home - away) rolling 10",
    "pace_diff_5": "Pace differential (home - away) rolling 5",

    # ── Rolling per-possession stats (TOV rate excluded — TOV data NULL in DB) ──
    "h_ft_rate_r5": "Home team free throw rate (FTA/FGA) rolling 5",
    "a_ft_rate_r5": "Away team free throw rate (FTA/FGA) rolling 5",
    "h_ft_rate_r10": "Home team free throw rate (FTA/FGA) rolling 10",
    "a_ft_rate_r10": "Away team free throw rate (FTA/FGA) rolling 10",

    "h_efg_r5": "Home team effective FG% rolling 5",
    "a_efg_r5": "Away team effective FG% rolling 5",
    "h_efg_r10": "Home team effective FG% rolling 10",
    "a_efg_r10": "Away team effective FG% rolling 10",

    "h_threep_rate_r5": "Home team 3PA rate (3PA/FGA) rolling 5",
    "a_threep_rate_r5": "Away team 3PA rate (3PA/FGA) rolling 5",
    "h_threep_rate_r10": "Home team 3PA rate (3PA/FGA) rolling 10",
    "a_threep_rate_r10": "Away team 3PA rate (3PA/FGA) rolling 10",

    "h_ast_ratio_r5": "Home team assist ratio (AST/FGM) rolling 5",
    "a_ast_ratio_r5": "Away team assist ratio (AST/FGM) rolling 5",
    "h_ast_ratio_r10": "Home team assist ratio (AST/FGM) rolling 10",
    "a_ast_ratio_r10": "Away team assist ratio (AST/FGM) rolling 10",


    # ── Star player features (season 35 only) ──────────────────────────
    "h_star_ppg_5": "Home team top-3 scorers PPG rolling 5",
    "a_star_ppg_5": "Away team top-3 scorers PPG rolling 5",
    "h_stars_active": "Home team active top-3 scorers count",
    "a_stars_active": "Away team active top-3 scorers count",
    "h_star1_ppg_5": "Home team leading scorer PPG rolling 5",
    "a_star1_ppg_5": "Away team leading scorer PPG rolling 5",
    "h_star1_active": "Home team leading scorer active (binary)",
    "a_star1_active": "Away team leading scorer active (binary)",

    # ── Team splits (home/away + vs conference) — PRIOR SEASON ─────────
    # Derived from nba.team_splits (ATS/OU over-rate + venue scoring). Home/away
    # venue splits for the venue team; vs_conf = vs the OPPONENT's conference.
    # (Back-to-back/rest splits excluded per Rich.)
    "h_ats_pct_home": "Home team ATS cover % at home (prior season)",
    "a_ats_pct_away": "Away team ATS cover % on road (prior season)",
    "h_ou_over_pct_home": "Home team OU over % at home (prior season)",
    "a_ou_over_pct_away": "Away team OU over % on road (prior season)",
    "h_pts_home": "Home team pts-for per game at home (prior season)",
    "a_pts_away": "Away team pts-for per game on road (prior season)",
    "h_pts_against_home": "Home team pts-against per game at home (prior season)",
    "a_pts_against_away": "Away team pts-against per game on road (prior season)",
    "h_ats_pct_vs_conf": "Home team ATS cover % vs opponent conference (prior season)",
    "a_ats_pct_vs_conf": "Away team ATS cover % vs opponent conference (prior season)",
    "h_ou_over_pct_vs_conf": "Home team OU over % vs opponent conference (prior season)",
    "a_ou_over_pct_vs_conf": "Away team OU over % vs opponent conference (prior season)",
}

DISPLAY_NAMES: Dict[str, str] = {
    "spread": "Spread",
    "closing_ou": "Closing OU",
    "home_moneyline": "Home ML",
    "away_moneyline": "Away ML",
    "home_score": "Home Score",
    "away_score": "Away Score",
    "season_year": "Season",
    "season_id": "Season",
    "game_id": "Game ID",
    "date": "Date",
    "home_team_id": "Home Team ID",
    "away_team_id": "Away Team ID",
    "h_adj_off_10": "Home Adj Off L10",
    "h_adj_def_10": "Home Adj Def L10",
    "a_adj_off_10": "Away Adj Off L10",
    "a_adj_def_10": "Away Adj Def L10",

    "rest_h": "Home Rest",
    "rest_a": "Away Rest",
    "rest_diff": "Rest Diff",
    "home_b2b": "Home B2B",
    "away_b2b": "Away B2B",
    "travel_miles": "Travel Miles",
    "h_implied": "Home Implied",
    "a_implied": "Away Implied",
    "spread_movement": "Spread Movement",
    "ou_movement": "OU Movement",
    "over_implied_prob": "Over Implied Prob",
    "implied_margin": "Implied Margin",
    "ml_spread_mismatch": "ML-Spread Mismatch",
    "h_implied_score": "Home Implied Score",
    "a_implied_score": "Away Implied Score",
    "h_ats_wins_5": "Home ATS Wins L5",
    "a_ats_wins_5": "Away ATS Wins L5",
    "h_ats_margin_5": "Home ATS Margin L5",
    "a_ats_margin_5": "Away ATS Margin L5",
    "h_wins_5": "Home Wins L5",
    "h_wins_10": "Home Wins L10",
    "a_wins_5": "Away Wins L5",
    "a_wins_10": "Away Wins L10",
    "home_ats_cover": "Home team covered the spread (1=yes, 0=no)",
    "away_ats_cover": "Away team covered the spread (1=yes, 0=no)",
    "over_result": "Game went over the total (1=yes, 0=no)",

    # ── Enhanced fatigue ──────────────────────────────────────────────
    "h_three_in_four": "Home 3-in-4",
    "a_three_in_four": "Away 3-in-4",
    "h_four_in_five": "Home 4-in-5",
    "a_four_in_five": "Away 4-in-5",
    "h_five_in_eight": "Home 5-in-8",
    "a_five_in_eight": "Away 5-in-8",

    # ── OU rolling records ────────────────────────────────────────────
    "h_ou_wins_5": "Home Over Wins L5",
    "a_ou_wins_5": "Away Over Wins L5",
    "h_ou_wins_10": "Home Over Wins L10",
    "a_ou_wins_10": "Away Over Wins L10",
    "h_ou_margin_5": "Home OU Margin L5",
    "a_ou_margin_5": "Away OU Margin L5",

    # ── Extended ATS windows ───────────────────────────────────────────
    "h_ats_wins_10": "Home ATS Wins L10",
    "a_ats_wins_10": "Away ATS Wins L10",
    "h_ats_margin_10": "Home ATS Margin L10",
    "a_ats_margin_10": "Away ATS Margin L10",

    # ── Rolling ORTG, DRTG, Net Rating, Pace ───────────────────────────
    "h_ortg_r5": "Home ORTG L5",
    "a_ortg_r5": "Away ORTG L5",
    "h_ortg_r10": "Home ORTG L10",
    "a_ortg_r10": "Away ORTG L10",

    "h_drtg_r5": "Home DRTG L5",
    "a_drtg_r5": "Away DRTG L5",
    "h_drtg_r10": "Home DRTG L10",
    "a_drtg_r10": "Away DRTG L10",

    "h_net_rtg_r5": "Home Net Rtg L5",
    "a_net_rtg_r5": "Away Net Rtg L5",
    "h_net_rtg_r10": "Home Net Rtg L10",
    "a_net_rtg_r10": "Away Net Rtg L10",

    "h_pace_r5": "Home Pace L5",
    "a_pace_r5": "Away Pace L5",
    "h_pace_r10": "Home Pace L10",
    "a_pace_r10": "Away Pace L10",

    "net_rtg_diff_5": "Net Rtg Diff L5",
    "net_rtg_diff_10": "Net Rtg Diff L10",
    "pace_diff_5": "Pace Diff L5",

    # ── Rolling per-possession stats ───────────────────────────────────
    "h_ft_rate_r5": "Home FTr L5",
    "a_ft_rate_r5": "Away FTr L5",
    "h_ft_rate_r10": "Home FTr L10",
    "a_ft_rate_r10": "Away FTr L10",

    "h_efg_r5": "Home eFG% L5",
    "a_efg_r5": "Away eFG% L5",
    "h_efg_r10": "Home eFG% L10",
    "a_efg_r10": "Away eFG% L10",

    "h_threep_rate_r5": "Home 3PA% L5",
    "a_threep_rate_r5": "Away 3PA% L5",
    "h_threep_rate_r10": "Home 3PA% L10",
    "a_threep_rate_r10": "Away 3PA% L10",

    "h_ast_ratio_r5": "Home AST/FGM L5",
    "a_ast_ratio_r5": "Away AST/FGM L5",
    "h_ast_ratio_r10": "Home AST/FGM L10",
    "a_ast_ratio_r10": "Away AST/FGM L10",


    # ── Star player features ───────────────────────────────────────────
    "h_star_ppg_5": "Home Stars PPG L5",
    "a_star_ppg_5": "Away Stars PPG L5",
    "h_stars_active": "Home Stars Active",
    "a_stars_active": "Away Stars Active",
    "h_star1_ppg_5": "Home Top Scorer PPG L5",
    "a_star1_ppg_5": "Away Top Scorer PPG L5",
    "h_star1_active": "Home Top Scorer Active",
    "a_star1_active": "Away Top Scorer Active",

    # ── Team splits (home/away + vs conference) — PRIOR SEASON ─────────
    # Derived from nba.team_splits (contains ATS/OU over-rate + venue scoring).
    # Uses the team's previous completed season to avoid lookahead. Home/away
    # venue splits for the venue team; vs_conf = team's split vs the OPPONENT's
    # conference. (Back-to-back/rest splits deliberately excluded.)
    "h_ats_pct_home": "Home team ATS cover % at home (prior season)",
    "a_ats_pct_away": "Away team ATS cover % on road (prior season)",
    "h_ou_over_pct_home": "Home team OU over % at home (prior season)",
    "a_ou_over_pct_away": "Away team OU over % on road (prior season)",
    "h_pts_home": "Home team pts-for per game at home (prior season)",
    "a_pts_away": "Away team pts-for per game on road (prior season)",
    "h_pts_against_home": "Home team pts-against per game at home (prior season)",
    "a_pts_against_away": "Away team pts-against per game on road (prior season)",
    "h_ats_pct_vs_conf": "Home team ATS cover % vs opponent conference (prior season)",
    "a_ats_pct_vs_conf": "Away team ATS cover % vs opponent conference (prior season)",
    "h_ou_over_pct_vs_conf": "Home team OU over % vs opponent conference (prior season)",
    "a_ou_over_pct_vs_conf": "Away team OU over % vs opponent conference (prior season)",
}


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
        self._catalog = {**FEATURES_CATALOG, **COMPUTED_FEATURES_CATALOG}
        self._feature_cache: Optional[pd.DataFrame] = None
        logger.info(
            "NBADataLoader initialized (ats_only=%s, ou_only=%s)",
            ats_only, ou_only,
        )

    @property
    def engine(self):
        """Lazy-initialized SQLAlchemy engine."""
        if self._engine is None:
            from sqlalchemy import create_engine
            self._engine = create_engine(self.db_url, pool_pre_ping=True)
        return self._engine

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
        return DISPLAY_NAMES.get(name, name)

    def get_feature_columns(self, target: Optional[str] = None) -> List[str]:
        """Return trainable feature column names.

        Parameters
        ----------
        target :
            If ``'ats'``, only return features in ``COMPUTED_FEATURES_CATALOG``
            that correspond to ATS features.  If ``None``, return all
            trainable features (all computed).

        Returns
        -------
        Sorted list of feature column names.
        """
        if target in ("ats", "ou"):
            flag = "current_ats" if target == "ats" else "current_ou"
            try:
                with psycopg2.connect(PSYCOPG2_DATABASE_URL) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT name FROM nba.features WHERE {flag} = TRUE "
                            "AND is_trainable = TRUE ORDER BY id"
                        )
                        rows = cur.fetchall()
                        db_features = [r[0] for r in rows]
                        known = set(FEATURES_CATALOG.keys()) | set(COMPUTED_FEATURES_CATALOG.keys())
                        return sorted(c for c in db_features if c in known)
            except Exception:
                pass
            # Fallback: return home/away computed features
            return sorted(
                k for k in COMPUTED_FEATURES_CATALOG
                if k.startswith(("h_", "a_"))
            )
        known = set(FEATURES_CATALOG.keys()) | set(COMPUTED_FEATURES_CATALOG.keys())
        return sorted(known)

    def get_all_with_display(self) -> List[Dict[str, str]]:
        """Return a list of dicts with 'name', 'description', 'display_name'."""
        return [
            {
                "name": name,
                "description": desc,
                "display_name": DISPLAY_NAMES.get(name, name),
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
        query = GAME_QUERY

        where_parts: List[str] = []
        # Preseason games must never feed stats/training. NBA's PRE games can
        # carry a FINAL status with results, so excluding purely by status is
        # not enough — always drop them here.
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
        df = self.load_games(seasons=seasons, limit=limit)
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
        df = build_features(df, **kwargs)

        known = set(list(FEATURES_CATALOG.keys()) + list(COMPUTED_FEATURES_CATALOG.keys()))
        keep = [c for c in df.columns if c in known]
        # Add the non-trainable prior-season + raw display columns produced by
        # build_features(). They are NOT in the catalogs (so they never become
        # model features — trainability is gated by nba.features.is_trainable), but
        # they must survive here so the pick card and admin loader can read them.
        keep += [
            c for c in df.columns
            if c.startswith(("h_prior_", "a_prior_")) or c.endswith("_raw")
        ]
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
    #   * vs-conference features (vs_east/vs_west) are intentionally EXCLUDED
    #     here — they are marked is_trainable=false in nba.features and don't
    #     help training.
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

    df["implied_margin"] = (
        (df["h_implied"] - df["a_implied"]).abs() * 50.0
    ) * np.sign(df["h_implied"] - df["a_implied"])

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


def get_model_features(target: Optional[str] = None) -> List[str]:
    """Return the list of trainable feature names for NBA models.

    Parameters
    ----------
    target :
        If ``'ats'``, only return features for the ATS model.

    Returns
    -------
    Sorted list of trainable feature names.
    """
    return NBADataLoader().get_feature_columns(target=target)


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
