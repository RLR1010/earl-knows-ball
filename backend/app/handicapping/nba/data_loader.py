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
DEFAULT_DB_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)


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
        acs.cum_blk_rate           AS a_cum_blk_rate
    FROM nba.games g
    JOIN nba.teams ht ON ht.id = g.home_team_id
    JOIN nba.teams at ON at.id = g.away_team_id
    JOIN nba.seasons s ON s.id = g.season_id
    INNER JOIN betting_agg ba ON ba.game_id = g.id
    LEFT JOIN nba.cumulative_game_stats hcs
        ON hcs.game_id = g.id AND hcs.team_side = 'home'
    LEFT JOIN nba.cumulative_game_stats acs
        ON acs.game_id = g.id AND acs.team_side = 'away'
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
                with psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql://earl:earl2025@localhost:5432/earl_knows_football")) as conn:
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
            List of season IDs to load.  If ``None`` loads all.
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
        if status:
            where_parts.append(f"status = '{status}'")
        if seasons:
            season_list = ", ".join(str(s) for s in seasons)
            where_parts.append(f"season_id IN ({season_list})")
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
        refresh_cumulative: bool = True,
        force_rebuild_cumulative: bool = False,
    ) -> pd.DataFrame:
        """Load game data and apply full feature engineering.

        Main entry point for training pipelines.

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
    #  0. Per-game ORTG, DRTG, Pace (from box score)
    # ═══════════════════════════════════════════════════════════════════════════

    # Estimate offensive rebounds from total rebounds (league avg OReb% ≈ 24.5%)
    _OREB_EST = 0.245
    team_games["oreb_est"] = team_games["reb"] * _OREB_EST
    team_games["opp_oreb_est"] = team_games["opp_reb"] * _OREB_EST

    # Possessions = FGA - OREB + TOV + 0.44 × FTA
    # NOTE: TOV is NULL in DB, so we omit it. This inflates possession count
    #       (by ~13-14/game) but relative ORTG comparisons remain valid.
    team_games["poss"] = (
        team_games["fga"] - team_games["oreb_est"] + 0.44 * team_games["fta"]
    )
    team_games["opp_poss"] = (
        team_games["opp_fga"] - team_games["opp_oreb_est"] + 0.44 * team_games["opp_fta"]
    )

    # ORTG = points per 100 own possessions
    team_games["ortg"] = team_games["score_for"] / team_games["poss"].clip(lower=1) * 100
    # DRTG = points allowed per 100 opponent possessions
    team_games["drtg"] = team_games["score_against"] / team_games["opp_poss"].clip(lower=1) * 100
    # Net Rating = ORTG - DRTG
    team_games["net_rtg"] = team_games["ortg"] - team_games["drtg"]
    # Pace = average of both teams' possessions (approximates possessions per game)
    team_games["pace"] = (team_games["poss"] + team_games["opp_poss"]) / 2

    # ── Per-possession stats (only those computable from available box score data) ──
    # NOTE: TOV, steals, blocks, fouls are all NULL in the DB, so tov_rate is excluded
    team_games["ft_rate"] = team_games["fta"] / team_games["fga"].clip(lower=1)
    team_games["efg"] = (team_games["fgm"] + 0.5 * team_games["fgm3"]) / team_games["fga"].clip(lower=1)
    team_games["threep_rate"] = team_games["fga3"] / team_games["fga"].clip(lower=1)
    team_games["ast_ratio"] = team_games["ast"] / team_games["fgm"].clip(lower=1)

    # ═══════════════════════════════════════════════════════════════════════════
    #  1. Opponent-adjusted scoring
    # ═══════════════════════════════════════════════════════════════════════════

    season_avg = team_games.groupby("season_id")["score_for"].transform("mean")

    for window in (10,):
        # opp_def_avg = how many points teams typically score against this opponent (measures opponent's defense)
        def_col = f"opp_def_avg_{window}"
        team_games[def_col] = (
            team_games.groupby("opp_abbr")["score_for"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
        )
        # opp_off_avg = how many points this opponent typically scores (measures opponent's offense)
        off_col = f"opp_off_avg_{window}"
        team_games[off_col] = (
            team_games.groupby("opp_abbr")["score_against"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
        )

        adj_off_h = f"h_adj_off_{window}"
        adj_def_h = f"h_adj_def_{window}"
        adj_off_a = f"a_adj_off_{window}"
        adj_def_a = f"a_adj_def_{window}"

        # Both anchored to season_avg (~110): higher is better for both
        # adj_off: league avg + how much better/worse team scored vs opponent's defense
        team_games[adj_off_h] = np.where(
            team_games["is_home"] == 1,
            season_avg + (team_games["score_for"] - team_games[def_col]),
            np.nan,
        )
        # adj_def: league avg - how much more/less team allowed vs opponent's offense
        team_games[adj_def_h] = np.where(
            team_games["is_home"] == 1,
            season_avg - (team_games["score_against"] - team_games[off_col]),
            np.nan,
        )
        team_games[adj_off_a] = np.where(
            team_games["is_home"] == 0,
            season_avg + (team_games["score_for"] - team_games[def_col]),
            np.nan,
        )
        team_games[adj_def_a] = np.where(
            team_games["is_home"] == 0,
            season_avg - (team_games["score_against"] - team_games[off_col]),
            np.nan,
        )

        for col in [adj_off_h, adj_def_h, adj_off_a, adj_def_a]:
            team_games[col] = (
                team_games.groupby("team_abbr")[col]
                .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
            )

    # ═══════════════════════════════════════════════════════════════════════════
    #  1b. Rolling ORTG, DRTG, Net Rating, Pace (from Section 0 per-game)
    # ═══════════════════════════════════════════════════════════════════════════

    nba_adv_metrics = ["ortg", "drtg", "net_rtg", "pace"]
    nba_per_poss = ["ft_rate", "efg", "threep_rate", "ast_ratio"]

    team_games["won"] = (team_games["score_for"] > team_games["score_against"]).astype(int)

    for window in (5, 10):
        # Team-wide win count (ALL games, not split by venue)
        win_col = f"wins_{window}"
        team_games[win_col] = (
            team_games.groupby("team_abbr")["won"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).sum())
        )

        for metric in nba_adv_metrics + nba_per_poss:
            rolling_col = f"{metric}_r{window}"
            team_games[rolling_col] = (
                team_games.groupby("team_abbr")[metric]
                .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
            )

        # Compute rolling differences: ortg_diff = h_ortg - a_ortg
        h_ortg_r = f"h_ortg_r{window}"
        a_ortg_r = f"a_ortg_r{window}"
        h_drtg_r = f"h_drtg_r{window}"
        a_drtg_r = f"a_drtg_r{window}"
        team_games[h_ortg_r] = np.where(team_games["is_home"] == 1, team_games[f"ortg_r{window}"], np.nan)
        team_games[a_ortg_r] = np.where(team_games["is_home"] == 0, team_games[f"ortg_r{window}"], np.nan)
        team_games[h_drtg_r] = np.where(team_games["is_home"] == 1, team_games[f"drtg_r{window}"], np.nan)
        team_games[a_drtg_r] = np.where(team_games["is_home"] == 0, team_games[f"drtg_r{window}"], np.nan)

        for col in [h_ortg_r, a_ortg_r, h_drtg_r, a_drtg_r]:
            team_games[col] = (
                team_games.groupby("team_abbr")[col]
                .transform(lambda s: s.ffill())
            )

    # ── Carry to main df ────────────────────────────────────────────
    for metric in nba_adv_metrics + nba_per_poss:
        for window in (5, 10):
            rolling_col = f"{metric}_r{window}"
            df[f"h_{rolling_col}"] = team_games.loc[
                team_games["is_home"] == 1, rolling_col
            ].values
            df[f"a_{rolling_col}"] = team_games.loc[
                team_games["is_home"] == 0, rolling_col
            ].values

        # Also carry current-game non-rolling values for reference
        df[f"h_{metric}"] = team_games.loc[team_games["is_home"] == 1, metric].values
        df[f"a_{metric}"] = team_games.loc[team_games["is_home"] == 0, metric].values

    # ── Team-wide win count (from long-form team_games, NOT venue-split) ──
    for window in (5, 10):
        win_col = f"wins_{window}"
        df[f"h_{win_col}"] = team_games.loc[team_games["is_home"] == 1, win_col].values
        df[f"a_{win_col}"] = team_games.loc[team_games["is_home"] == 0, win_col].values

    # ── Net Rating differential ─────────────────────────────────────
    df["net_rtg_diff_5"] = df["h_net_rtg_r5"] - df["a_net_rtg_r5"]
    df["net_rtg_diff_10"] = df["h_net_rtg_r10"] - df["a_net_rtg_r10"]
    df["pace_diff_5"] = df["h_pace_r5"] - df["a_pace_r5"]

    # ═══════════════════════════════════════════════════════════════════════════
    #  1c. Star player tracking (season 35 only — player_game_stats sparse)
    # ═══════════════════════════════════════════════════════════════════════════
    _star_engine = create_engine(DEFAULT_DB_URL)
    try:
        # Identify top-3 scorers per team, per season (NOT hardcoded to one season)
        with _star_engine.connect() as _conn:
            _players_df = pd.read_sql("""
                SELECT pss.player_id, pss.team_id, pss.season_id, pss.points_per_game,
                       ROW_NUMBER() OVER (
                           PARTITION BY pss.team_id, pss.season_id ORDER BY pss.points_per_game DESC
                       ) AS star_rank
                FROM nba.player_season_stats pss
                WHERE pss.games_played >= 10
                  AND pss.team_id IS NOT NULL
            """, _conn)

        _star_players = _players_df[_players_df["star_rank"] <= 3].copy()

        if len(_star_players) > 0:
            _star_ids = list(_star_players["player_id"].unique())
            with _star_engine.connect() as _conn:
                # Build placeholders safely
                _placeholders = ",".join([str(pid) for pid in _star_ids])
                _game_logs = pd.read_sql(f"""
                    SELECT pgs.player_id, pgs.game_id, pgs.team_id,
                           pgs.points, pgs.minutes, g.date, g.season_id
                    FROM nba.player_game_stats pgs
                    JOIN nba.games g ON pgs.game_id = g.id
                    WHERE pgs.player_id IN ({_placeholders})
                    ORDER BY pgs.player_id, g.date
                """, _conn)

            _gl = _game_logs.copy()
            _gl["points"] = _gl["points"].fillna(0).astype(float)
            # minutes is VARCHAR — can be "32" (numeric string), "32:08" (MM:SS), "-", or NULL
            _gl["minutes"] = (
                _gl["minutes"]
                .replace("-", None)
                .fillna(0)
                .astype(str)
                .str.replace(r"^(\d+):(\d{2})$", lambda m: str(int(m.group(1)) + int(m.group(2)) / 60), regex=True)
                .astype(float)
            )
            

            # Rolling 5-game PPG per player (shifted — no look-ahead bias)
            _gl["ppg_r5"] = (
                _gl.groupby("player_id")["points"]
                .transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
            )
            _gl["active"] = (_gl["minutes"] > 0).astype(int)

            # Merge rank info — match by season_id so each game uses that season's top scorers
            _gl = _gl.merge(
                _star_players[["player_id", "team_id", "season_id", "star_rank"]],
                on=["player_id", "team_id", "season_id"],
                how="left",
            )

            # Filter to TOP 3 scorers per team-game
            _star_only = _gl[_gl["star_rank"].notna() & (_gl["star_rank"] <= 3)].copy()

            if len(_star_only) > 0:
                # Per-game team aggregates for star players only
                _game_summary = _star_only.groupby(["game_id", "team_id"]).agg(
                    star_ppg_5=("ppg_r5", "sum"),
                    stars_active=("active", "sum"),
                ).reset_index()

                # Top scorer per game per team
                _top_star = _gl[_gl["star_rank"] == 1].copy()
                _top_summary = _top_star.groupby(["game_id", "team_id"]).agg(
                    star1_ppg_5=("ppg_r5", "first"),
                    star1_active=("active", "first"),
                ).reset_index()

                _game_star = _game_summary.merge(_top_summary, on=["game_id", "team_id"], how="left")

                # Merge home side
                _home = _game_star.rename(columns={
                    "team_id": "home_team_id",
                    "star_ppg_5": "h_star_ppg_5",
                    "stars_active": "h_stars_active",
                    "star1_ppg_5": "h_star1_ppg_5",
                    "star1_active": "h_star1_active",
                })
                df = df.merge(
                    _home[["game_id", "home_team_id", "h_star_ppg_5", "h_stars_active",
                           "h_star1_ppg_5", "h_star1_active"]],
                    on=["game_id", "home_team_id"],
                    how="left",
                )

                # Merge away side
                _away = _game_star.rename(columns={
                    "team_id": "away_team_id",
                    "star_ppg_5": "a_star_ppg_5",
                    "stars_active": "a_stars_active",
                    "star1_ppg_5": "a_star1_ppg_5",
                    "star1_active": "a_star1_active",
                })
                df = df.merge(
                    _away[["game_id", "away_team_id", "a_star_ppg_5", "a_stars_active",
                           "a_star1_ppg_5", "a_star1_active"]],
                    on=["game_id", "away_team_id"],
                    how="left",
                )
    finally:
        _star_engine.dispose()

    # ═══════════════════════════════════════════════════════════════════════════
    #  2. Rest days & back-to-back
    # ═══════════════════════════════════════════════════════════════════════════

    team_games["prev_date"] = team_games.groupby("team_abbr")["date"].shift(1)
    team_games["rest_days"] = (
        team_games["date"] - team_games["prev_date"]
    ).dt.days
    team_games["b2b"] = (team_games["rest_days"] == 1).astype(int)

    df["rest_h"] = team_games.loc[team_games["is_home"] == 1, "rest_days"].values
    df["rest_a"] = team_games.loc[team_games["is_home"] == 0, "rest_days"].values
    df["home_b2b"] = team_games.loc[team_games["is_home"] == 1, "b2b"].values
    df["away_b2b"] = team_games.loc[team_games["is_home"] == 0, "b2b"].values

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

    df["h_three_in_four"] = team_games.loc[team_games["is_home"] == 1, "three_in_four"].values
    df["a_three_in_four"] = team_games.loc[team_games["is_home"] == 0, "three_in_four"].values
    df["h_four_in_five"] = team_games.loc[team_games["is_home"] == 1, "four_in_five"].values
    df["a_four_in_five"] = team_games.loc[team_games["is_home"] == 0, "four_in_five"].values
    df["h_five_in_eight"] = team_games.loc[team_games["is_home"] == 1, "five_in_eight"].values
    df["a_five_in_eight"] = team_games.loc[team_games["is_home"] == 0, "five_in_eight"].values

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

    # ── Surface opponent-adjusted efficiency to df ────────────────────────
    for window in (10,):
        home_adj = team_games.loc[
            team_games["is_home"] == 1,
            ["game_id"] + [f"h_{s}_{window}" for s in ("adj_off", "adj_def")],
        ].copy()
        away_adj = team_games.loc[
            team_games["is_home"] == 0,
            ["game_id"] + [f"a_{s}_{window}" for s in ("adj_off", "adj_def")],
        ].copy()
        df = df.merge(home_adj, on="game_id", how="left")
        df = df.merge(away_adj, on="game_id", how="left")

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

    df["ml_spread_mismatch"] = df["implied_margin"] - df["closing_spread"].abs()

    # ── Over/under implied probability (vig-free) ────────────────────────────
    _over_ip = _implied_prob(df["over_odds"])
    _under_ip = _implied_prob(df["under_odds"])
    df["over_implied_prob"] = _over_ip / (_over_ip + _under_ip)

    # ═══════════════════════════════════════════════════════════════════════════
    #  5. Form & streaks (ATS, win counts, cover margins)
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

    for team_prefix, abbr_col, cover_col, margin_col in [
        ("h_", "home_abbr", "home_ats_cover", "home_ats_margin"),
        ("a_", "away_abbr", "away_ats_cover", "away_ats_margin"),
    ]:
        df[f"{team_prefix}ats_wins_5"] = (
            df.groupby(abbr_col)[cover_col]
            .transform(lambda s: s.shift(1).rolling(5, min_periods=0).sum())
        )
        df[f"{team_prefix}ats_margin_5"] = (
            df.groupby(abbr_col)[margin_col]
            .transform(lambda s: s.shift(1).rolling(5, min_periods=0).mean())
        )

    # ── Over/under result ────────────────────────────────────────────────
    df["over_result"] = (
        (df["home_score"] + df["away_score"]) > df["closing_ou"]
    ).astype(float)

    # ── OU rolling records (over wins + margin, mirrors ATS pattern) ───
    df["ou_total"] = df["home_score"] + df["away_score"]
    df["ou_margin"] = df["ou_total"] - df["closing_ou"]

    for team_prefix, abbr_col in [("h_", "home_abbr"), ("a_", "away_abbr")]:
        df[f"{team_prefix}ou_wins_5"] = (
            df.groupby(abbr_col)["over_result"]
            .transform(lambda s: s.shift(1).rolling(5, min_periods=0).sum())
        )
        df[f"{team_prefix}ou_wins_10"] = (
            df.groupby(abbr_col)["over_result"]
            .transform(lambda s: s.shift(1).rolling(10, min_periods=0).sum())
        )
        df[f"{team_prefix}ou_margin_5"] = (
            df.groupby(abbr_col)["ou_margin"]
            .transform(lambda s: s.shift(1).rolling(5, min_periods=0).mean())
        )

    # ── Extended ATS windows (10-game) ─────────────────────────────────
    for team_prefix, abbr_col, cover_col, margin_col in [
        ("h_", "home_abbr", "home_ats_cover", "home_ats_margin"),
        ("a_", "away_abbr", "away_ats_cover", "away_ats_margin"),
    ]:
        df[f"{team_prefix}ats_wins_10"] = (
            df.groupby(abbr_col)[cover_col]
            .transform(lambda s: s.shift(1).rolling(10, min_periods=0).sum())
        )
        df[f"{team_prefix}ats_margin_10"] = (
            df.groupby(abbr_col)[margin_col]
            .transform(lambda s: s.shift(1).rolling(10, min_periods=0).mean())
        )

    # ═══════════════════════════════════════════════════════════════════════════
    #  6. Fill NaN / clean up
    # ═══════════════════════════════════════════════════════════════════════════

    for col in df.columns:
        if col in ("spread", "closing_spread", "closing_ou"):
            continue
        if df[col].dtype in (np.float64, np.int64) and df[col].isna().any():
            df[col] = df[col].fillna(0)

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
