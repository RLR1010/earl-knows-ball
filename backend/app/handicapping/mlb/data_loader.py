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


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two lat/lon points."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Master game-level SQL query ──────────────────────────────────────────────

GAME_QUERY = """
WITH team_ops AS (
    SELECT
        bgs.game_id,
        bgs.team_side,
        -- Team OBP = (H + BB + HBP) / (AB + BB + HBP + SF)
        (SUM(bgs.hits) + SUM(bgs.base_on_balls) + SUM(bgs.hit_by_pitch))::numeric /
            NULLIF(SUM(bgs.at_bats) + SUM(bgs.base_on_balls) + SUM(bgs.hit_by_pitch) + SUM(bgs.sacrifice_flies), 0)
        * 1.0 AS avg_team_obp,
        -- Team SLG = TB / AB
        SUM(bgs.total_bases)::numeric / NULLIF(SUM(bgs.at_bats), 0) * 1.0 AS avg_team_slg,
        -- Team OPS = OBP + SLG
        ((SUM(bgs.hits) + SUM(bgs.base_on_balls) + SUM(bgs.hit_by_pitch))::numeric /
            NULLIF(SUM(bgs.at_bats) + SUM(bgs.base_on_balls) + SUM(bgs.hit_by_pitch) + SUM(bgs.sacrifice_flies), 0) * 1.0)
        +
        (SUM(bgs.total_bases)::numeric / NULLIF(SUM(bgs.at_bats), 0) * 1.0) AS avg_team_ops,
        -- Raw batting stats for feature engineering
        SUM(bgs.at_bats)::numeric AS team_at_bats,
        SUM(bgs.hits)::numeric AS team_hits,
        SUM(bgs.base_on_balls)::numeric AS team_walks,
        SUM(bgs.total_bases)::numeric AS team_total_bases,
        SUM(bgs.plate_appearances)::numeric AS team_pa
    FROM mlb.batting_game_stats bgs
    WHERE bgs.plate_appearances IS NOT NULL AND bgs.team_side IS NOT NULL
    GROUP BY bgs.game_id, bgs.team_side
),
game_starter_stats AS (
    SELECT
        pgs.game_id,
        pgs.team_abbr,
        pgs.pitcher_name,
        pgs.ip::numeric AS ip,
        pgs.er::numeric AS er,
        pgs.h::numeric AS hits,
        pgs.bb::numeric AS walks,
        pgs.k::numeric AS strikeouts,
        pgs.hr::numeric AS homeruns,
        -- WHIP = (H + BB) / IP
        (pgs.h::numeric + pgs.bb::numeric) / NULLIF(pgs.ip::numeric, 0) AS whip,
        -- K/9 = (K / IP) * 9
        (pgs.k::numeric / NULLIF(pgs.ip::numeric, 0)) * 9.0 AS k_per_9,
        -- K/BB = K / BB
        pgs.k::numeric / NULLIF(pgs.bb::numeric, 0) AS k_per_bb,
        -- ERA = (ER / IP) * 9
        (pgs.er::numeric / pgs.ip::numeric) * 9 AS pitcher_era,
        pgs.runs_allowed::numeric AS runs_allowed,
        pgs.hit_by_pitch::numeric AS hit_by_pitch
    FROM mlb.pitcher_game_stats pgs
    WHERE pgs.is_starter = true AND pgs.er IS NOT NULL AND pgs.ip IS NOT NULL AND pgs.ip > 0
),
starter_era AS (
    SELECT
        s.game_id,
        MAX(CASE WHEN s.team_abbr = h_t.abbreviation THEN s.pitcher_era ELSE NULL END) AS home_starter_era,
        MAX(CASE WHEN s.team_abbr = h_t.abbreviation THEN s.pitcher_name ELSE NULL END) AS home_starter_name,
        MAX(CASE WHEN s.team_abbr = h_t.abbreviation THEN s.ip ELSE NULL END) AS home_starter_ip,
        MAX(CASE WHEN s.team_abbr = h_t.abbreviation THEN s.er ELSE NULL END) AS home_starter_er,
        MAX(CASE WHEN s.team_abbr = a_t.abbreviation THEN s.pitcher_era ELSE NULL END) AS away_starter_era,
        MAX(CASE WHEN s.team_abbr = a_t.abbreviation THEN s.pitcher_name ELSE NULL END) AS away_starter_name,
        MAX(CASE WHEN s.team_abbr = a_t.abbreviation THEN s.ip ELSE NULL END) AS away_starter_ip,
        MAX(CASE WHEN s.team_abbr = a_t.abbreviation THEN s.er ELSE NULL END) AS away_starter_er
    FROM game_starter_stats s
    JOIN mlb.games g ON g.id = s.game_id
    JOIN mlb.teams h_t ON h_t.id = g.home_team_id
    JOIN mlb.teams a_t ON a_t.id = g.away_team_id
    GROUP BY s.game_id
)
SELECT
    g.id                                                    AS game_id,
    g.mlb_game_id,
    g.season_id,
    g.game_type,
    g.game_number,
    g.status,
    g.date                                                  AS game_date,
    g.home_team_id,
    g.away_team_id,
    g.home_score,
    g.away_score,
    g.venue,
    g.venue_id,
    g.roof_type,
    g.surface,
    g.temperature,
    g.wind_speed,
    g.wind_direction,
    g.weather_condition,
    g.scheduled_innings,
    g.actual_innings,
    g.duration_minutes,
    g.attendance,
    g.day_night,
    g.home_wins,
    g.home_losses,
    g.away_wins,
    g.away_losses,
    g.home_pitcher_name,
    g.away_pitcher_name,

    h.abbreviation                                          AS ha,
    a.abbreviation                                          AS aa,
    h.division                                              AS hdiv,
    a.division                                              AS adiv,
    h.league                                                AS hleague,
    a.league                                                AS aleague,
    h.name                                                  AS home_team_name,
    a.name                                                  AS away_team_name,

    s.year                                                  AS season_year,

    (g.home_score - g.away_score)                           AS margin,

    -- Betting lines from consolidated table (closing is preferred)
    c.closing_spread                                        AS spread,
    c.closing_home_ml                                       AS home_moneyline,
    c.closing_away_ml                                       AS away_moneyline,
    c.closing_ou                                            AS over_under,
    c.closing_ou_sportsbook                                 AS sportsbook,
    c.has_verified_ou,
    c.opening_spread                                        AS opening_spread,
    c.opening_ou                                            AS opening_ou,
    c.opening_home_ml                                       AS opening_home_ml,
    c.opening_away_ml                                       AS opening_away_ml,
    c.opening_spread_sportsbook,
    c.closing_home_implied_probability,
    c.closing_away_implied_probability,
    c.opening_home_implied_probability,
    c.opening_away_implied_probability,
    c.closing_spread_home_odds,
    c.closing_spread_away_odds,
    c.closing_over_odds,
    c.closing_under_odds,

    -- Team OPS from batting_game_stats
    toh.avg_team_ops                                         AS h_ops,
    toa.avg_team_ops                                         AS a_ops,
    toh.avg_team_slg                                        AS h_slg,
    toa.avg_team_slg                                        AS a_slg,

    -- Starter ERA from pitcher_game_stats
    gse.home_starter_era                                    AS h_starter_era,
    gse.away_starter_era                                    AS a_starter_era,
    gse.home_starter_name                                   AS h_starter_name,
    gse.away_starter_name                                   AS a_starter_name,
    gse.home_starter_ip                                    AS h_starter_ip,
    gse.home_starter_er                                    AS h_starter_er,
    gse.away_starter_ip                                    AS a_starter_ip,
    gse.away_starter_er                                    AS a_starter_er,
    -- Batting stat columns
    toh.team_at_bats                                        AS home_at_bats,
    toh.team_hits                                           AS home_hits,
    toh.team_walks                                          AS home_walks,
    toh.team_total_bases                                    AS home_total_bases,
    toh.team_pa                                             AS home_pa,
    toa.team_at_bats                                        AS away_at_bats,
    toa.team_hits                                           AS away_hits,
    toa.team_walks                                          AS away_walks,
    toa.team_total_bases                                    AS away_total_bases,
    toa.team_pa                                             AS away_pa

FROM mlb.games g
LEFT JOIN mlb.teams h         ON h.id = g.home_team_id
LEFT JOIN mlb.teams a         ON a.id = g.away_team_id
LEFT JOIN team_ops toh        ON toh.game_id = g.id AND toh.team_side = 'home'
LEFT JOIN team_ops toa        ON toa.game_id = g.id AND toa.team_side = 'away'
LEFT JOIN starter_era gse ON gse.game_id = g.id
LEFT JOIN mlb.seasons s       ON s.id = g.season_id
LEFT JOIN mlb.betting_lines_consolidated c ON c.game_id = g.id
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
    "h_pitcher_k_bb_l20": "Home pitcher K/BB rate over last 20 appearances",
    "a_pitcher_k_bb_l20": "Away pitcher K/BB rate over last 20 appearances",
    "h_pitcher_home_team_l20": "Home pitcher ERA with this team (last 20)",
    "a_pitcher_home_team_l20": "Away pitcher ERA with this team (last 20)",
    # ── Bullpen ──
    "h_bullpen_era_l5": "Home bullpen ERA over last 5 appearances",
    "a_bullpen_era_l5": "Away bullpen ERA over last 5 appearances",
    "h_bullpen_ip_l5": "Home bullpen IP over last 5 appearances",
    "a_bullpen_ip_l5": "Away bullpen IP over last 5 appearances",
    # ── Form ──
    "h_form_l10": "Home winning percentage last 10 games (exponential MA, shift(1))",
    "a_form_l10": "Away winning percentage last 10 games (exponential MA, shift(1))",
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


def build_features(df: pd.DataFrame, log_fn=None) -> pd.DataFrame:
    """Apply all MLB feature engineering to a raw game DataFrame.

    This is the single consolidated version of the ``build_features`` functions
    previously duplicated across ``mlb_xgb_model_ats.py``, ``mlb_xgb_model_ou.py``,
    and ``mlb_xgb_model_ml.py``.  It handles:

      * Situational features (rest days, dome, travel, tz diff, division)
      * Team-quality features (win % blended with prior-season smoothing)
      * Home/away scoring splits (expanding mean, shift(1))
      * Pitcher features (ERA, K/9, WHIP, K/BB over rolling windows)
      * Bullpen features (ERA, IP over rolling windows)
      * Form features (exponential moving average win %)
      * Line movement features (opening vs current comparison)
      * Park factors (venue-level run environment)
      * Season-game-number tracking for overlap-free rolling stats

    Parameters
    ----------
    df : pd.DataFrame
        Raw game data from ``MLBDataLoader.load_games()``.  Must contain at
        minimum ``game_id``, ``ha``, ``aa``, ``game_date``, ``home_score``,
        ``away_score``, ``season_year`` (and ideally betting lines).
    log_fn : callable, optional
        Function to call for progress logs (e.g. ``print`` or ``logger.info``).
        Default is no-op.

    Returns
    -------
    pd.DataFrame
        Original DataFrame with all derived feature columns appended.
    """
    if log_fn is None:
        def log_fn(*args, **kwargs): pass
    log = log_fn

    feats = df.copy()
    log("build_features: starting on %d rows × %d cols", len(feats), len(feats.columns))

    # ── 1. Basic parsing & date features ──

    if "game_date" in feats.columns:
        feats["game_date"] = pd.to_datetime(feats["game_date"])
        feats["month"] = feats["game_date"].dt.month
        feats["is_summer"] = feats["month"].isin([6, 7, 8]).astype(int)
    else:
        feats["month"] = 0
        feats["is_summer"] = 0

    log("  Parsed dates, month, is_summer")

    # Fill game_type for filtering
    feats["game_type"] = feats.get("game_type", "Regular Season").fillna("Regular Season")

    # ── 2. Short-list columns for the pivot (keep only what we need) ──

    pivot_df = feats[[
        "game_id", "ha", "aa", "game_date", "season_year", "game_type",
        "home_score", "away_score",
        "h_ops", "a_ops",
        "h_slg", "a_slg",
        "h_starter_era", "a_starter_era",
        "h_starter_er", "h_starter_ip",
        "a_starter_er", "a_starter_ip",
        "home_at_bats", "away_at_bats",
        "home_hits", "away_hits",
        "home_walks", "away_walks",
        "home_total_bases", "away_total_bases",
        "home_pa", "away_pa",
    ]].copy()

    # Team-level roll-up: one row per team-game
    home = pivot_df.rename(columns={"ha": "team", "aa": "opp", "home_score": "rf", "away_score": "ra",
        "h_ops": "team_ops", "h_slg": "team_slg",
        "h_starter_era": "starter_era",
        "h_starter_er": "starter_er", "h_starter_ip": "starter_ip",
        "home_at_bats": "ab", "home_hits": "hits",
        "home_walks": "bb", "home_total_bases": "tb", "home_pa": "pa"})
    home["home_ind"] = 1
    away = pivot_df.rename(columns={"aa": "team", "ha": "opp", "away_score": "rf", "home_score": "ra",
        "a_ops": "team_ops", "a_slg": "team_slg",
        "a_starter_era": "starter_era",
        "a_starter_er": "starter_er", "a_starter_ip": "starter_ip",
        "away_at_bats": "ab", "away_hits": "hits",
        "away_walks": "bb", "away_total_bases": "tb", "away_pa": "pa"})
    away["home_ind"] = 0

    tg = pd.concat([home, away], ignore_index=True)
    tg = tg.sort_values(["team", "game_date"]).reset_index(drop=True)

    tg["year"] = tg["season_year"].fillna(tg["game_date"].dt.year.fillna(2024)).astype(int)
    current_year = int(tg["year"].max())

    log("  Pivoted to %d team-game rows", len(tg))

    # ── 3. Rolling stats per team ──

    
    # We'll compute these manually with groupby + expanding/rolling
    log("  Computing rolling team stats (rf, ra)...")

    # Add season_game_no for team/season
    tg["season_game_no"] = tg.groupby(["team", "year"]).cumcount() + 1

    # Per-team expanding/rolling averages
    tg["rf_avg"] = tg.groupby("team")["rf"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )
    tg["ra_avg"] = tg.groupby(["team", "year"])["ra"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )
    tg["rf10"] = tg.groupby(["team", "year"])["rf"].transform(
        lambda s: s.rolling(10, min_periods=1).mean().shift(1)
    )
    tg["ra10"] = tg.groupby(["team", "year"])["ra"].transform(
        lambda s: s.rolling(10, min_periods=1).mean().shift(1)
    )
    tg["rf5"] = tg.groupby(["team", "year"])["rf"].transform(
        lambda s: s.rolling(5, min_periods=1).mean().shift(1)
    )
    tg["ra5"] = tg.groupby(["team", "year"])["ra"].transform(
        lambda s: s.rolling(5, min_periods=1).mean().shift(1)
    )

    # Home-only and away-only splits
    tg["rf_home"] = tg.groupby(["team", "year"])["rf"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )
    tg["rf_away"] = tg.groupby(["team", "year"])["rf"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )
    # Home/away splits using the home_ind
    home_games = tg[tg["home_ind"] == 1].groupby(["team", "year"])["rf"]
    away_games = tg[tg["home_ind"] == 0].groupby(["team", "year"])["rf"]
    # Map back
    tg["h_home_rf"] = tg.groupby(["team", "year"])["rf"].transform(
        lambda s: (
            tg.loc[s.index, "rf"]
            .where(tg.loc[s.index, "home_ind"] == 1, None)
            .expanding(min_periods=1).mean().shift(1)
        )
    )
    tg["a_away_rf"] = tg.groupby(["team", "year"])["rf"].transform(
        lambda s: (
            tg.loc[s.index, "rf"]
            .where(tg.loc[s.index, "home_ind"] == 0, None)
            .expanding(min_periods=1).mean().shift(1)
        )
    )

    # Rolling OPS stats (L10 and L20)
    tg["ops_l10"] = tg.groupby(["team", "year"])["team_ops"].transform(
        lambda s: s.rolling(10, min_periods=1).mean().shift(1)
    )
    tg["ops_l20"] = tg.groupby(["team", "year"])["team_ops"].transform(
        lambda s: s.rolling(20, min_periods=1).mean().shift(1)
    )
    # Rolling SLG stats (L10 and L20)  — same windows as OPS
    tg["slg_l10"] = tg.groupby(["team", "year"])["team_slg"].transform(
        lambda s: s.rolling(10, min_periods=1).mean().shift(1)
    )
    tg["slg_l20"] = tg.groupby(["team", "year"])["team_slg"].transform(
        lambda s: s.rolling(20, min_periods=1).mean().shift(1)
    )

    log("  Rolling team stats computed")

    # ── Compute win for the game ──
    tg["win"] = (tg["rf"] > tg["ra"]).astype(int)

    # ── 3b. Prior-season averages for expanding stat game-1 fill ──

    prior_season = tg[tg["year"] == current_year - 1].copy()
    prior_map = {}
    if len(prior_season) > 0:
        # Compute over_flag temporally if over_under is available
        if "over_under" in prior_season.columns:
            prior_season["over_flag"] = ((prior_season["rf"] + prior_season["ra"]) > prior_season["over_under"]).astype(float)
        elif "over_flag" not in prior_season.columns:
            prior_season["over_flag"] = 0.0
        for team, grp in prior_season.groupby("team"):
            prior_map[team] = {
                "rf": grp["rf"].mean(),
                "ra": grp["ra"].mean(),
                "win": grp["win"].mean(),
                "over_flag": grp["over_flag"].mean(),
                "rf_home": grp[grp["home_ind"] == 1]["rf"].mean() if grp["home_ind"].sum() > 0 else None,
                "rf_away": grp[grp["home_ind"] == 0]["rf"].mean() if (~grp["home_ind"]).sum() > 0 else None,
            }

    # Fill game-1 NaN in expanding stats
    for col, prior_key in [
        ("rf_avg", "rf"),
        ("ra_avg", "ra"),
        ("winpct", "win"),
        ("over_freq", "over_flag"),
        ("h_home_rf", "rf_home"),
        ("a_away_rf", "rf_away"),
    ]:
        if col in tg.columns:
            mask = tg[col].isna()
            for idx in tg.index[mask]:
                team = tg.loc[idx, "team"]
                if team in prior_map and prior_key in prior_map[team]:
                    pv = prior_map[team][prior_key]
                    if pv is not None and not pd.isna(pv):
                        tg.at[idx, col] = pv

    log("  Expanding stat game-1 NaN filled from prior-season averages")
# ── 3c. Rolling stats with prior-season seeding ──

    log("  Computing rolling stats with prior-season seeding...")

    # prior_map was built in section 3b: team -> {col: val}

    def prior_for(team, year, col):
        """Get prior-season stat for a team, or None."""
        if team in prior_map and col in prior_map[team]:
            return prior_map[team][col]
        return None

    def seed_prior(series, team_series, prior_col):
        """Fill NaN values in a stat with the team's prior-season average.

        Legacy — only used for first-game NaN fill. For proper smoothing
        across the early season, use blend_expanding_prior instead.
        """
        result = series.copy()
        nulls = result.isna()
        if nulls.any():
            for idx in result[nulls].index:
                team = team_series.loc[idx]
                year_loc = tg.loc[idx, "year"]
                pv = prior_for(team, year_loc, prior_col)
                if pv is not None:
                    result.loc[idx] = pv
        return result

    def blend_expanding_prior(series, team_series, prior_col, ramp_games: int = 40):
        """Blend an expanding (YTD) stat with prior-season average, ramping
        from full prior-avg weight down to 0 over `ramp_games` games.

        weight_prior = max(0, 1 - K/ramp_games)

        This means:
        - Game 1: ~100% prior (shift(1) → NaN → filled with prior)
        - Game 20: ~50% actual avg, ~50% prior avg
        - Game 40+: 100% actual expanding avg, prior dropped entirely
        """
        result = series.copy()
        # Step 1: Fill NaN (game 1) with pure prior
        nulls = result.isna()
        if nulls.any():
            for idx in result[nulls].index:
                team = team_series.loc[idx]
                year_loc = tg.loc[idx, "year"]
                pv = prior_for(team, year_loc, prior_col)
                if pv is not None:
                    result.loc[idx] = pv
        # Step 2: Blend partial-season rows
        if ramp_games and ramp_games > 1:
            # Count games within the current season only (reset per year)
            count_series = (
                tg.groupby(["team", "year"])["game_date"]
                .transform(lambda s: s.expanding(min_periods=1).count().shift(1))
            )
            blend_needed = count_series < ramp_games
            if blend_needed.any():
                for idx in result[blend_needed].index:
                    team = team_series.loc[idx]
                    year_loc = tg.loc[idx, "year"]
                    pv = prior_for(team, year_loc, prior_col)
                    if pv is not None:
                        k = count_series.loc[idx]
                        if pd.isna(k):
                            k = 0
                        if k >= ramp_games:
                            continue
                        if k == 0:
                            pass  # step 1 handled it
                        else:
                            weight_prior = 1.0 - (k / ramp_games)
                            if not pd.isna(result.loc[idx]):
                                result.loc[idx] = result.loc[idx] * (1.0 - weight_prior) + pv * weight_prior
        return result

    def blend_rolling_prior(series, team_series, prior_col, window_size):
        """Blend a rolling window stat with prior-season average until the window fills.

        For each row where the window has K < window_size actual games,
        the result is:
            (current_avg * K/window_size) + (prior_season_avg * (window_size-K)/window_size)

        Once K >= window_size, no blend is needed.
        Uses expanding().count().shift(1) per team+year to determine how many
        games each rolling value is based on (since all rolling stats use shift(1)).
        """
        result = series.copy()
        # Step 1: Fill NaN rows (game 1 of season — shift(1) produces NaN)
        # with the pure prior-season average
        nulls = result.isna()
        if nulls.any():
            for idx in result[nulls].index:
                team = team_series.loc[idx]
                year_loc = tg.loc[idx, "year"]
                pv = prior_for(team, year_loc, prior_col)
                if pv is not None:
                    result.loc[idx] = pv
        # Step 2: Blend rows where the available game count < window_size
        if window_size and window_size > 1:
            # Count games within the current season only (reset per year)
            count_series = (
                tg.groupby(["team", "year"])["game_date"]
                .transform(lambda s: s.expanding(min_periods=1).count().shift(1))
            )
            blend_needed = count_series < window_size
            if blend_needed.any():
                for idx in result[blend_needed].index:
                    team = team_series.loc[idx]
                    year_loc = tg.loc[idx, "year"]
                    pv = prior_for(team, year_loc, prior_col)
                    if pv is not None:
                        k = count_series.loc[idx]
                        if pd.isna(k):
                            k = 0
                        if k >= window_size:
                            continue  # shouldn't happen but be safe
                        if k == 0:
                            # No actual games, just use prior (step 1 handled this)
                            pass
                        else:
                            weight = k / window_size
                            if not pd.isna(result.loc[idx]):
                                result.loc[idx] = result.loc[idx] * weight + pv * (1.0 - weight)
        return result

    # ---- Run rolling stat computations ----

    # Runs scored rolling (rf_avg ~ 20-game season average, rf10, rf5)
    tg["rf_avg"] = blend_expanding_prior(
        tg.groupby(["team", "year"])["rf"].transform(lambda s: s.expanding(min_periods=1).mean().shift(1)),
        tg["team"], "prior_pf", 10
    )
    tg["ra_avg"] = blend_expanding_prior(
        tg.groupby(["team", "year"])["ra"].transform(lambda s: s.expanding(min_periods=1).mean().shift(1)),
        tg["team"], "prior_pa", 10
    )
    tg["rf10"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["rf"].transform(lambda s: s.rolling(10, min_periods=1).mean().shift(1)),
        tg["team"], "prior_pf", 10
    )
    tg["ra10"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["ra"].transform(lambda s: s.rolling(10, min_periods=1).mean().shift(1)),
        tg["team"], "prior_pa", 10
    )
    tg["rf5"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["rf"].transform(lambda s: s.rolling(5, min_periods=1).mean().shift(1)),
        tg["team"], "prior_pf", 5
    )
    tg["ra5"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["ra"].transform(lambda s: s.rolling(5, min_periods=1).mean().shift(1)),
        tg["team"], "prior_pa", 5
    )

    # 20-game rolling run stats
    tg["rf20"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["rf"].transform(lambda s: s.rolling(20, min_periods=1).mean().shift(1)),
        tg["team"], "prior_pf", 20
    )
    tg["ra20"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["ra"].transform(lambda s: s.rolling(20, min_periods=1).mean().shift(1)),
        tg["team"], "prior_pa", 20
    )

    # Starter ERA rolling stats (L20 and L5)
    tg["pitcher_era_l20"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["starter_era"].transform(
            lambda s: s.rolling(20, min_periods=1).mean().shift(1)
        ),
        tg["team"], "prior_era", 20
    )
    tg["pitcher_era_l5"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["starter_era"].transform(
            lambda s: s.rolling(5, min_periods=1).mean().shift(1)
        ),
        tg["team"], "prior_era", 5
    )

    # Home / away run production splits
    # h_home_rf = avg rf when this team is home, a_away_rf = avg rf when this team is away
    tg["h_home_rf"] = blend_expanding_prior(
        tg.groupby(["team", "year"])["rf"].apply(
            lambda g: g.where(tg.loc[g.index, "home_ind"] == 1)
                      .expanding(min_periods=1).mean().shift(1)
        ).reset_index(level=[0, 1], drop=True),
        tg["team"], "prior_pf_home", 10
    )
    tg["a_away_rf"] = blend_expanding_prior(
        tg.groupby(["team", "year"])["rf"].apply(
            lambda g: g.where(tg.loc[g.index, "home_ind"] == 0)
                      .expanding(min_periods=1).mean().shift(1)
        ).reset_index(level=[0, 1], drop=True),
        tg["team"], "prior_pf_away", 10
    )

    # Rolling OPS stats (L10 and L20)
    tg["ops_l10"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["team_ops"].transform(
            lambda s: s.rolling(10, min_periods=1).mean().shift(1)
        ),
        tg["team"], "prior_ops", 10
    )
    tg["ops_l20"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["team_ops"].transform(
            lambda s: s.rolling(20, min_periods=1).mean().shift(1)
        ),
        tg["team"], "prior_ops", 20
    )

    # Rolling SLG stats (seeded)
    tg["slg_l10"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["team_slg"].transform(
            lambda s: s.rolling(10, min_periods=1).mean().shift(1)
        ),
        tg["team"], "prior_slg", 10
    )
    tg["slg_l20"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["team_slg"].transform(
            lambda s: s.rolling(20, min_periods=1).mean().shift(1)
        ),
        tg["team"], "prior_slg", 20
    )

    # Win percentage
    tg["winpct"] = blend_expanding_prior(
        tg.groupby(["team", "year"])["win"].transform(
            lambda s: s.expanding(min_periods=1).mean().shift(1)
        ),
        tg["team"], "prior_winpct", 10
    )
    tg["winpct_l10"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["win"].transform(
            lambda s: s.rolling(10, min_periods=1).mean().shift(1)
        ),
        tg["team"], "prior_winpct", 10
    )

    # Form (exponential moving average win %)
    tg["form_l10"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["win"].transform(
            lambda s: s.ewm(span=10, min_periods=1).mean().shift(1)
        ),
        tg["team"], "prior_winpct", 10
    )

    # Over/under frequency
    ou_map = feats[["game_id", "over_under"]].copy()
    tg = tg.merge(ou_map, on="game_id", how="left")
    tg["over_flag"] = ((tg["rf"] + tg["ra"]) > tg["over_under"]).astype(float)
    tg["over_freq"] = blend_expanding_prior(
        tg.groupby(["team", "year"])["over_flag"]
        .apply(lambda s: s.expanding(min_periods=1).mean().shift(1))
        .reset_index(level=[0, 1], drop=True),
        tg["team"], "prior_winpct", 10
    )
    tg["over_freq5"] = blend_rolling_prior(
        tg.groupby(["team", "year"])["over_flag"]
        .apply(lambda s: s.rolling(5, min_periods=1).mean().shift(1))
        .reset_index(level=[0, 1], drop=True),
        tg["team"], "prior_winpct", 5
    )
    tg.drop(columns=["over_under", "over_flag"], inplace=True)

    log("  Rolling stats done — %d team-game rows, all seeded with prior-season averages", len(tg))

    # ── 4. Merge team-game features back onto game-level feats ──

    # Home-team features
    home_feats = tg.rename(columns={
        "team": "ha",
        "winpct": "h_winpct",
        "winpct_l10": "h_winpct_l10",
        "rf_avg": "h_rf_avg",
        "ra_avg": "h_ra_avg",
        "rf10": "h_rf10",
        "ra10": "h_ra10",
        "rf5": "h_rf5",
        "ra5": "h_ra5",
        "rf20": "h_rf20",
        "ra20": "h_ra20",
        "form_l10": "h_form_l10",
        "over_freq": "h_over_freq",
        "over_freq5": "h_over_freq5",
        "ops_l10": "h_ops_l10",
        "ops_l20": "h_ops_l20",
        "slg_l10": "h_slg_l10",
        "slg_l20": "h_slg_l20",
        "pitcher_era_l20": "h_pitcher_era_l20",
        "pitcher_era_l5": "h_pitcher_era_l5",
    })[["game_id", "ha", "h_winpct", "h_winpct_l10",
        "h_rf_avg", "h_ra_avg", "h_rf10", "h_ra10",
        "h_rf5", "h_ra5", "h_rf20", "h_ra20",
        "h_form_l10", "h_over_freq", "h_over_freq5",
        "h_ops_l10", "h_ops_l20",
        "h_slg_l10", "h_slg_l20",
        "h_pitcher_era_l20", "h_pitcher_era_l5",
        "h_home_rf", "a_away_rf"]]

    # Away-team features
    away_feats = tg.rename(columns={
        "team": "aa",
        "winpct": "a_winpct",
        "winpct_l10": "a_winpct_l10",
        "rf_avg": "a_rf_avg",
        "ra_avg": "a_ra_avg",
        "rf10": "a_rf10",
        "ra10": "a_ra10",
        "rf5": "a_rf5",
        "ra5": "a_ra5",
        "rf20": "a_rf20",
        "ra20": "a_ra20",
        "form_l10": "a_form_l10",
        "over_freq": "a_over_freq",
        "over_freq5": "a_over_freq5",
        "ops_l10": "a_ops_l10",
        "ops_l20": "a_ops_l20",
        "slg_l10": "a_slg_l10",
        "slg_l20": "a_slg_l20",
        "pitcher_era_l20": "a_pitcher_era_l20",
        "pitcher_era_l5": "a_pitcher_era_l5",
    })[["game_id", "aa", "a_winpct", "a_winpct_l10",
        "a_rf_avg", "a_ra_avg", "a_rf10", "a_ra10",
        "a_rf5", "a_ra5", "a_rf20", "a_ra20",
        "a_form_l10", "a_over_freq", "a_over_freq5",
        "a_ops_l10", "a_ops_l20",
        "a_slg_l10", "a_slg_l20",
        "a_pitcher_era_l20", "a_pitcher_era_l5"]]

    # Drop old h_home_rf, a_away_rf from tg — they'll come through merge
    home_feats = home_feats.drop(columns=["h_home_rf", "a_away_rf"], errors="ignore")
    home_feats["h_home_rf"] = tg["h_home_rf"]
    away_feats["a_away_rf"] = tg["a_away_rf"]

    feats = feats.merge(home_feats, on=["game_id", "ha"], how="left")
    feats = feats.merge(away_feats, on=["game_id", "aa"], how="left")

    log("  Team-level rolling features merged back onto games")

    # ── 5. Derived team-quality features ──

    feats["winpct_diff"] = feats["h_winpct"] - feats["a_winpct"]
    feats["winpct_l10_diff"] = feats["h_winpct_l10"] - feats["a_winpct_l10"]

    # ── 6. Situational features ──

    # Convert game_date from UTC to Chicago local time before computing rest days
    # so overnight games are credited to the correct calendar day
    CHI_TZ = "America/Chicago"
    if feats["game_date"].dt.tz is not None:
        feats["game_date_ct"] = feats["game_date"].dt.tz_convert(CHI_TZ)
    else:
        feats["game_date_ct"] = feats["game_date"].dt.tz_localize("UTC", ambiguous="NaT").dt.tz_convert(CHI_TZ)

    # Rest days
    for team_col, rest_col in [("ha", "rest_h"), ("aa", "rest_a")]:
        te = feats[["game_id", team_col, "game_date_ct"]].copy()
        te["game_date"] = te["game_date_ct"].dt.floor("D")  # strip time, compare by date only
        te = te.sort_values([team_col, "game_date"])
        te["next_date"] = te.groupby(team_col)["game_date"].shift(1)
        te["rest"] = (te["game_date"] - te["next_date"]).dt.days
        # Merge back by game_id so rest values go to the right rows
        feats = feats.drop(columns=[rest_col], errors="ignore")
        feats = feats.merge(te[["game_id", "rest"]], on="game_id", how="left")
        feats = feats.rename(columns={"rest": rest_col})

    feats["rest_diff"] = feats["rest_h"] - feats["rest_a"]

    # Rest hours — same approach but using full datetime (time of day preserved)
    for team_col, rest_col in [("ha", "rest_h_hours"), ("aa", "rest_a_hours")]:
        te = feats[["game_id", team_col, "game_date_ct"]].copy()
        te["game_date"] = te["game_date_ct"]
        te = te.sort_values([team_col, "game_date"])
        te["next_date"] = te.groupby(team_col)["game_date"].shift(1)
        te["rest_hours"] = (te["game_date"] - te["next_date"]).dt.total_seconds() / 3600
        feats = feats.drop(columns=[rest_col], errors="ignore")
        feats = feats.merge(te[["game_id", "rest_hours"]], on="game_id", how="left")
        feats = feats.rename(columns={"rest_hours": rest_col})

    feats["rest_diff_hours"] = feats["rest_h_hours"] - feats["rest_a_hours"]

    # Division
    feats["is_div"] = (feats["hdiv"] == feats["adiv"]).astype(int)

    # Dome
    if "roof_type" in feats.columns:
        feats["is_dome"] = feats["roof_type"].fillna("").str.lower().isin(
            ["dome", "retractable", "dome (closed)", "dome (open)", "retractable (closed)", "retractable (open)"]
        ).astype(int)
    else:
        feats["is_dome"] = 0

    # Travel miles & TZ diff — real distance + timezone offset between home cities
    feats["travel_miles"] = feats.apply(
        lambda r: (
            haversine_miles(
                TEAM_LOCATIONS[r["ha"]]["lat"], TEAM_LOCATIONS[r["ha"]]["lon"],
                TEAM_LOCATIONS[r["aa"]]["lat"], TEAM_LOCATIONS[r["aa"]]["lon"],
            )
            if r.get("ha") in TEAM_LOCATIONS and r.get("aa") in TEAM_LOCATIONS
            else 0
        ),
        axis=1,
    )
    feats["tz_diff"] = feats.apply(
        lambda r: (
            abs(TEAM_LOCATIONS[r["ha"]]["tz"] - TEAM_LOCATIONS[r["aa"]]["tz"])
            if r.get("ha") in TEAM_LOCATIONS and r.get("aa") in TEAM_LOCATIONS
            else 0
        ),
        axis=1,
    )

    # ── 7. Implied probabilities from moneyline ──

    def ml_to_implied(ml: float) -> float:
        if pd.isna(ml) or ml == 0:
            return 0.5
        if ml < 0:
            return -ml / (-ml + 100)
        else:
            return 100 / (ml + 100)

    for target, source in [
        ("home_implied_probability", "home_moneyline"),
        ("away_implied_probability", "away_moneyline"),
        ("opening_home_implied", "opening_home_ml"),
        ("opening_away_implied", "opening_away_ml"),
    ]:
        if source in feats.columns:
            feats[target] = feats[source].apply(
                lambda x: ml_to_implied(x) if pd.notna(x) else 0.5
            )
        else:
            feats[target] = 0.5
    feats["h_implied"] = feats["home_implied_probability"]
    feats["a_implied"] = feats["away_implied_probability"]

    # ── 4.5. Alias columns to match ATS_FEATURES naming ──
    # These aliases ensure the ATS feature set (", \"ATS_FEATURES\", ") from the model file
    # can find the columns it expects

    # h_home_ra: home team's runs-allowed when at home (approximated as h_ra_avg)
    # a_home_rf: away team's runs scored on the road (approximated as a_away_rf)
    # a_home_ra: away team's runs-allowed on the road (approximated as a_ra_avg)
    # h_ra20, a_ra20, h_rf20, a_rf20 — real 20-game rolling windows, already in feats from rename map

    # Implied total (blended rolling runs-for + runs-allowed for both teams)
    # This matches the OU model definition: avg of home rf, home ra, away rf, away ra
    h_rf = feats.get("h_rf10", feats.get("h_rf_avg", 4.5))
    h_ra = feats.get("h_ra10", feats.get("h_ra_avg", 4.5))
    a_rf = feats.get("a_rf10", feats.get("a_rf_avg", 4.5))
    a_ra = feats.get("a_ra10", feats.get("a_ra_avg", 4.5))
    feats["implied_total"] = (h_rf.fillna(4.5) + h_ra.fillna(4.5) + a_rf.fillna(4.5) + a_ra.fillna(4.5)) / 2
    feats["implied_total"] = feats["implied_total"].clip(lower=3, upper=16)

    # ── 8. Line movement features ──

    if "over_under" in feats.columns and "opening_ou" in feats.columns:
        feats["ou_movement"] = feats["over_under"] - feats["opening_ou"]
    else:
        feats["ou_movement"] = 0.0

    feats["ml_implied_movement"] = (
        feats["home_implied_probability"] - feats["opening_home_implied"]
    )

    # ── 8.5 OU-specific features: total10, over_freq, over_freq5 ──
    # h_total10 / a_total10: 10-game sum of runs scored/allowed (= h_rf10/a_ra10 * 10)
    if "h_rf10" in feats.columns and feats["h_rf10"].notna().any():
        feats["h_total10"] = (feats["h_rf10"] * 10).fillna(45).clip(lower=0)
    else:
        feats["h_total10"] = (feats.get("h_rf_avg", pd.Series(4.5, index=feats.index)) * 10).fillna(45).clip(lower=0)
    if "a_ra10" in feats.columns and feats["a_ra10"].notna().any():
        feats["a_total10"] = (feats["a_ra10"] * 10).fillna(45).clip(lower=0)
    else:
        feats["a_total10"] = (feats.get("a_ra_avg", pd.Series(4.5, index=feats.index)) * 10).fillna(45).clip(lower=0)

    # h_home_rf / a_away_rf — already merged from tg in section 4
    if "h_home_rf" not in feats.columns:
        feats["h_home_rf"] = feats.get("h_rf_avg", 4.5)
    if "a_away_rf" not in feats.columns:
        feats["a_away_rf"] = feats.get("a_rf_avg", 4.5)

    # ── Over percentage aliases (run #13 naming) ──
    # ── 9. Pitcher features (rolling windows per specific pitcher) ──
    # Load all starter appearances, compute per-pitcher rolling stats (ERA, K/9, WHIP, K/BB),
    # then join by (game_id, pitcher_name) lookup. No merge — use direct map via
    # h_starter_name/a_starter_name from the GAME_QUERY.
    try:
        from sqlalchemy import create_engine, text as sa_text
        _pitcher_engine = create_engine(DEFAULT_DB_URL)
        with _pitcher_engine.connect() as _pconn:
            _sp_df = pd.read_sql(sa_text("""
                SELECT
                    pgs.game_id,
                    pgs.pitcher_name,
                    pgs.team_abbr,
                    g.date,
                    (pgs.er::numeric / pgs.ip::numeric) * 9 AS era,
                    (pgs.h::numeric + pgs.bb::numeric) / NULLIF(pgs.ip::numeric, 0) AS whip,
                    (pgs.k::numeric / NULLIF(pgs.ip::numeric, 0)) * 9.0 AS k9,
                    pgs.k::numeric / NULLIF(pgs.bb::numeric, 0) AS kbb
                FROM mlb.pitcher_game_stats pgs
                JOIN mlb.games g ON g.id = pgs.game_id
                WHERE pgs.is_starter = true AND pgs.ip IS NOT NULL AND pgs.ip > 0
                ORDER BY pgs.pitcher_name, g.date
            """), _pconn)
    except Exception:
        _sp_df = pd.DataFrame()

    # Build a lookup: (game_id, pitcher_name) -> {metric: value}
    # Using vectorized expanding+rolling per pitcher group (much faster than row iteration).
    _pitcher_lookup = {}  # (game_id, pitcher_name) -> {metric: value}
    if not _sp_df.empty:
        log(f"  Loaded {len(_sp_df)} starter appearances for {_sp_df['pitcher_name'].nunique()} pitchers")
        _sp_df = _sp_df.sort_values(["pitcher_name", "date"]).reset_index(drop=True)
        for name, grp in _sp_df.groupby("pitcher_name"):
            grp = grp.reset_index(drop=True)
            # Expanding (career-to-date) roll — shift(1) to exclude current game
            eras_exp = grp["era"].expanding().mean().shift(1)
            k9s_exp = grp["k9"].expanding().mean().shift(1)
            whips_exp = grp["whip"].expanding().mean().shift(1)
            kbbs_exp = grp["kbb"].expanding().mean().shift(1)
            # Rolling windows — shift(1) to exclude current
            eras_l20 = grp["era"].rolling(20, min_periods=1).mean().shift(1)
            eras_l5 = grp["era"].rolling(5, min_periods=1).mean().shift(1)
            k9s_l20 = grp["k9"].rolling(20, min_periods=1).mean().shift(1)
            k9s_l5 = grp["k9"].rolling(5, min_periods=1).mean().shift(1)
            whips_l20 = grp["whip"].rolling(20, min_periods=1).mean().shift(1)
            whips_l5 = grp["whip"].rolling(5, min_periods=1).mean().shift(1)
            kbbs_l20 = grp["kbb"].rolling(20, min_periods=1).mean().shift(1)
            kbbs_l5 = grp["kbb"].rolling(5, min_periods=1).mean().shift(1)

            for idx, gid in enumerate(grp["game_id"]):
                _pitcher_lookup[(int(gid), name)] = {
                    "era_l20": eras_l20.iloc[idx] if pd.notna(eras_l20.iloc[idx]) else eras_exp.iloc[idx],
                    "era_l5": eras_l5.iloc[idx] if pd.notna(eras_l5.iloc[idx]) else eras_exp.iloc[idx],
                    "k9_l20": k9s_l20.iloc[idx] if pd.notna(k9s_l20.iloc[idx]) else k9s_exp.iloc[idx],
                    "k9_l5": k9s_l5.iloc[idx] if pd.notna(k9s_l5.iloc[idx]) else k9s_exp.iloc[idx],
                    "whip_l20": whips_l20.iloc[idx] if pd.notna(whips_l20.iloc[idx]) else whips_exp.iloc[idx],
                    "whip_l5": whips_l5.iloc[idx] if pd.notna(whips_l5.iloc[idx]) else whips_exp.iloc[idx],
                    "kbb_l20": kbbs_l20.iloc[idx] if pd.notna(kbbs_l20.iloc[idx]) else kbbs_exp.iloc[idx],
                    "kbb_l5": kbbs_l5.iloc[idx] if pd.notna(kbbs_l5.iloc[idx]) else kbbs_exp.iloc[idx],
                }
    else:
        log("  Starter appearance data not available from DB")

    # Build fallback: most recent game per pitcher (for SCHEDULED/LIVE)
    _by_pitcher = {}
    for (gid, pname), pstats in _pitcher_lookup.items():
        if pname not in _by_pitcher or gid > _by_pitcher[pname]["_gid"]:
            _by_pitcher[pname] = {**pstats, "_gid": gid}
    _by_pitcher = {k: {kk: vv for kk, vv in v.items() if kk != "_gid"} for k, v in _by_pitcher.items()}

    # Apply per-pitcher rolling stats by looking up (game_id, starter_name)
    if _sp_df.empty or "h_starter_name" not in feats.columns:
        log("  Starter name data not available on query; keeping team-rolling pitcher ERA")
    else:
        gids = feats["game_id"].astype(int).tolist()
        h_names_raw = feats["h_starter_name"].tolist()
        a_names_raw = feats["a_starter_name"].tolist()
        h_fallback = feats["home_pitcher_name"].tolist() if "home_pitcher_name" in feats.columns else [None]*len(feats)
        a_fallback = feats["away_pitcher_name"].tolist() if "away_pitcher_name" in feats.columns else [None]*len(feats)
        h_names = [n if pd.notna(n) else fb for n, fb in zip(h_names_raw, h_fallback)]
        a_names = [n if pd.notna(n) else fb for n, fb in zip(a_names_raw, a_fallback)]
        for side, names in [("h", h_names), ("a", a_names)]:
            eras_l20, eras_l5, k9s_l20, k9s_l5, whips_l20, whips_l5, kbbs_l20, kbbs_l5 = [], [], [], [], [], [], [], []
            for gid, pname in zip(gids, names):
                if pname is not None and pd.notna(pname) and (int(gid), pname) in _pitcher_lookup:
                    pstats = _pitcher_lookup[(int(gid), pname)]
                elif pname is not None and pd.notna(pname):
                    pstats = _by_pitcher.get(pname)
                else:
                    pstats = None
                if pstats:
                    eras_l20.append(pstats.get("era_l20"))
                    eras_l5.append(pstats.get("era_l5"))
                    k9s_l20.append(pstats.get("k9_l20"))
                    k9s_l5.append(pstats.get("k9_l5"))
                    whips_l20.append(pstats.get("whip_l20"))
                    whips_l5.append(pstats.get("whip_l5"))
                    kbbs_l20.append(pstats.get("kbb_l20"))
                    kbbs_l5.append(pstats.get("kbb_l5"))
                else:
                    eras_l20.append(None)
                    eras_l5.append(None)
                    k9s_l20.append(None)
                    k9s_l5.append(None)
                    whips_l20.append(None)
                    whips_l5.append(None)
                    kbbs_l20.append(None)
                    kbbs_l5.append(None)

            # Fill per-pitcher columns; if per-pitcher is NaN, fall back to team rolling
            orig_era_l20 = feats.get(f"{side}_pitcher_era_l20", pd.Series([None]*len(feats), index=feats.index))
            orig_era_l5 = feats.get(f"{side}_pitcher_era_l5", pd.Series([None]*len(feats), index=feats.index))
            feats[f"{side}_pitcher_era_l20"] = pd.Series(eras_l20, index=feats.index).fillna(orig_era_l20)
            feats[f"{side}_pitcher_era_l5"] = pd.Series(eras_l5, index=feats.index).fillna(orig_era_l5)
            feats[f"{side}_pitcher_k9_l20"] = pd.Series(k9s_l20, index=feats.index).fillna(0.0)
            feats[f"{side}_pitcher_k9_l5"] = pd.Series(k9s_l5, index=feats.index).fillna(0.0)
            feats[f"{side}_pitcher_whip_l20"] = pd.Series(whips_l20, index=feats.index).fillna(0.0)
            feats[f"{side}_pitcher_whip_l5"] = pd.Series(whips_l5, index=feats.index).fillna(0.0)
            feats[f"{side}_pitcher_kbb_l20"] = pd.Series(kbbs_l20, index=feats.index).fillna(0.0)
            feats[f"{side}_pitcher_kbb_l5"] = pd.Series(kbbs_l5, index=feats.index).fillna(0.0)

    # --- Bullpen / combo ERA features ---
    log("  Computing combo ERA features...")
    # ── 10. Park factor ──
    # Historical: avg total runs scored in this venue / avg total runs scored across all MLB
    # Uses ALL available historical completed games for a robust per-venue factor,
    # not just the seasons being trained/predicted on.

    if "venue" in feats.columns and "home_score" in feats.columns:
        # Load all historical game data for park factor computation
        try:
            park_hist = _load_park_history()
            completed = park_hist[
                (park_hist["game_type"] == "R")
                & (park_hist["home_score"].notna())
                & (park_hist["away_score"].notna())
            ].copy()
        except Exception:
            # Fall back to the current feats
            completed = feats[
                (feats["game_type"] == "R")
                & (feats["home_score"].notna())
                & (feats["away_score"].notna())
            ].copy()

        completed["total_runs"] = completed["home_score"] + completed["away_score"]
        completed["venue"] = completed["venue"].fillna("Unknown")

        venue_stats = completed.groupby("venue")["total_runs"].agg(["mean", "count"])
        venue_stats = venue_stats[venue_stats["count"] >= 20]  # minimum sample
        league_avg_runs = completed["total_runs"].mean()

        if league_avg_runs > 0 and not venue_stats.empty:
            venue_stats["factor"] = venue_stats["mean"] / league_avg_runs
            feats["park_factor"] = feats["venue"].map(venue_stats["factor"]).fillna(1.0)
        else:
            feats["park_factor"] = 1.0
    else:
        feats["park_factor"] = 1.0

    log("  Park factors computed")

    # ── 10b. Bullpen features (per-team rolling ERA & IP over L5) ──
    try:
        _bp_engine = create_engine(DEFAULT_DB_URL)
        with _bp_engine.connect() as _bpconn:
            _bp_df = pd.read_sql(text("""
                SELECT
                    pgs.game_id,
                    pgs.team_abbr,
                    pgs.pitcher_name,
                    g.date,
                    (pgs.er::numeric / NULLIF(pgs.ip::numeric, 0)) * 9 AS era,
                    pgs.ip::numeric AS ip
                FROM mlb.pitcher_game_stats pgs
                JOIN mlb.games g ON g.id = pgs.game_id
                WHERE (pgs.is_starter = false OR pgs.is_starter IS NULL)
                  AND pgs.ip IS NOT NULL AND pgs.ip > 0
                ORDER BY pgs.team_abbr, g.date
            """), _bpconn)
    except Exception:
        _bp_df = pd.DataFrame()

    if not _bp_df.empty:
        _bp_df = _bp_df.sort_values(["team_abbr", "date"]).reset_index(drop=True)
        # Per-team: aggregate all bullpen appearances in each game
        _bp_game = _bp_df.groupby(["team_abbr", "game_id", "date"], as_index=False).agg(
            bullpen_era=("era", "mean"),
            bullpen_ip=("ip", "sum")
        )
        _bp_game = _bp_game.sort_values(["team_abbr", "date"]).reset_index(drop=True)

        # Rolling L5 per team
        team_bullpen = []
        for team, grp in _bp_game.groupby("team_abbr"):
            grp = grp.reset_index(drop=True)
            grp["bp_era_l5"] = grp["bullpen_era"].rolling(5, min_periods=1).mean().shift(1)
            grp["bp_ip_l5"] = grp["bullpen_ip"].rolling(5, min_periods=1).sum().shift(1)
            team_bullpen.append(grp)
        if team_bullpen:
            _bp_rolled = pd.concat(team_bullpen, ignore_index=True)
            _bp_rolled["game_id"] = _bp_rolled["game_id"].astype(int)

            # Build lookup: team_abbr -> {game_id: {era_l5, ip_l5}}
            _bp_lookup = {}
            for _, r in _bp_rolled.iterrows():
                _bp_lookup[(int(r["game_id"]), r["team_abbr"])] = {
                    "era_l5": r["bp_era_l5"],
                    "ip_l5": r["bp_ip_l5"]
                }
            # Fallback: most recent bullpen per team (for SCHEDULED/LIVE)
            _bp_by_team = {}
            for (gid, team_abbr), stats in _bp_lookup.items():
                if team_abbr not in _bp_by_team or gid > _bp_by_team[team_abbr]["_gid"]:
                    _bp_by_team[team_abbr] = {**stats, "_gid": gid}
            _bp_by_team = {k: {kk: vv for kk, vv in v.items() if kk != "_gid"} for k, v in _bp_by_team.items()}

            # Map to home/away using team_abbr from game query
            gids = feats["game_id"].astype(int).tolist()
            try:
                h_teams = feats["ha"].tolist()
                a_teams = feats["aa"].tolist()
            except KeyError:
                h_teams = feats["home_team_abbreviation"].tolist() if "home_team_abbreviation" in feats else [""] * len(feats)
                a_teams = feats["away_team_abbreviation"].tolist() if "away_team_abbreviation" in feats else [""] * len(feats)

            for side, teams in [("h", h_teams), ("a", a_teams)]:
                eras_l5, ips_l5 = [], []
                for gid, team_abbr in zip(gids, teams):
                    key = (gid, team_abbr)
                    if key in _bp_lookup:
                        eras_l5.append(_bp_lookup[key]["era_l5"])
                        ips_l5.append(_bp_lookup[key]["ip_l5"])
                    elif team_abbr in _bp_by_team:
                        bps = _bp_by_team[team_abbr]
                        eras_l5.append(bps.get("era_l5"))
                        ips_l5.append(bps.get("ip_l5"))
                    else:
                        eras_l5.append(None)
                        ips_l5.append(None)
                feats[f"{side}_bullpen_era_l5"] = pd.Series(eras_l5, index=feats.index).fillna(4.50)
                feats[f"{side}_bullpen_ip_l5"] = pd.Series(ips_l5, index=feats.index).fillna(0.0)
    else:
        for side in ["h", "a"]:
            feats[f"{side}_bullpen_era_l5"] = 4.50
            feats[f"{side}_bullpen_ip_l5"] = 0.0

    # ── 11. Wind calculated ──

    def _wind_direction_factor(direction: str) -> int:
        """Return 1 for out-blowing, -1 for in-blowing, 0 otherwise."""
        if direction is None:
            return 0
        d = str(direction).strip().lower()
        if d == "out":
            return 1
        if d == "in":
            return -1
        return 0

    feats["wind_calculated"] = feats.apply(
        lambda r: _wind_direction_factor(r.get("wind_direction")) * (
            float(r.get("wind_speed")) if r.get("wind_speed") is not None and not pd.isna(r.get("wind_speed")) else 0.0
        ),
        axis=1,
    )

    # ── 12. Team average total, combo ERA, combo ERA diff ──

    # Total runs per game involving this team
    tg["total_runs"] = tg["rf"] + tg["ra"]

    # Average total per team over rolling 10
    total_team_avg = tg.groupby(["team", "year"])["total_runs"].transform(
        lambda s: s.rolling(10, min_periods=1).mean().shift(1)
    )

    # Map back to home and away
    home_total_map = tg[tg["home_ind"] == 1].copy()
    home_total_map["total_avg_team_r10"] = total_team_avg[tg["home_ind"] == 1]
    away_total_map = tg[tg["home_ind"] == 0].copy()
    away_total_map["total_avg_team_r10"] = total_team_avg[tg["home_ind"] == 0]

    # Merge back — use both sides
    home_total = home_total_map[["game_id", "team", "total_avg_team_r10"]].rename(
        columns={"team": "ha"})
    away_total = away_total_map[["game_id", "team", "total_avg_team_r10"]].rename(
        columns={"team": "aa"})

    feats = feats.merge(home_total, on=["game_id", "ha"], how="left")
    feats = feats.merge(away_total, on=["game_id", "aa"], how="left", suffixes=(None, "_away"))

    # Combo: average of home + away total_avg_team_r10
    h_col = "total_avg_team_r10"
    a_col = "total_avg_team_r10_away"
    if h_col in feats.columns and a_col in feats.columns:
        h = feats[h_col].fillna(0)
        a = feats[a_col].fillna(0)
        feats["combo_era_r10"] = (h + a) / 2
        feats["combo_era_r10_diff"] = h - a
    else:
        feats["combo_era_r10"] = 0.0
        feats["combo_era_r10_diff"] = 0.0

    # Drop the doubled-up columns
    feats.drop(columns=["total_avg_team_r10_away"], errors="ignore", inplace=True)

    # ── 12. Misc aliases ──

    feats["ou_line"] = feats.get("over_under", 8.5)
    feats["closing_ou"] = feats.get("over_under", 8.5)
    feats["is_home_fav"] = (feats.get("spread", 0) < 0).astype(int)
    feats["margin"] = feats["home_score"] - feats["away_score"]
    feats["actual_margin"] = feats["margin"]
    feats["actual_total"] = feats["home_score"] + feats["away_score"]

    # ── 13. Drop rows without betting data ──
    # Games missing closing OU have no betting context for ATS/OU modeling.
    before = len(feats)
    feats = feats[feats["over_under"].notna() & (feats["over_under"] > 0)].copy()
    after = len(feats)
    if before != after:
        log("  Dropped %d rows without valid closing OU (%d remaining)", before - after, after)

    # ── 14. Fill NaNs ──
    float_cols = feats.select_dtypes(include=["float64", "float32"]).columns
    feats[float_cols] = feats[float_cols].fillna(0.0)

    log("build_features complete: %d rows × %d cols", len(feats), len(feats.columns))
    return feats

# ── Data Loader class ────────────────────────────────────────────────────────


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

    def get_features_catalog(self) -> Dict[str, str]:
        """Return the full feature catalog (raw + computed)."""
        merged = dict(FEATURES_CATALOG)
        merged.update(COMPUTED_FEATURES_CATALOG)
        return merged

    def get_feature_names(self) -> List[str]:
        """Return the list of all known feature names."""
        return list(self.get_features_catalog().keys())

    def get_feature_description(self, name: str) -> Optional[str]:
        """Return the description for a single feature, or None."""
        return self.get_features_catalog().get(name)

    def get_display_name(self, name: str) -> str:
        """Return the customer-facing display name for a feature.

        Falls back to title-casing the snake_case name if not in the catalog.
        """
        return DISPLAY_NAMES.get(name, name.replace("_", " ").title())

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
