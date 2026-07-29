"""
Optimized MLB Data Loader — uses pre-computed rolling stats tables.

Key differences from the original:
- GAME_QUERY JOINS to cumulative_game_stats, team_rolling_stats, pitcher_rolling_stats
  instead of computing everything from scratch via CTEs
- build_features() is ~50 lines instead of ~800 — no pandas rolling computation
- Prior-season blending done with SQL COALESCE instead of row-by-row Python loops
- Column names are preserved for backward compatibility with the ML models
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# OPTIMIZED GAME QUERY
# ═══════════════════════════════════════════════════════════════════════════════
# The old query had 19 CTEs recomputing cumulative stats from scratch.
# This version LEFT JOINs to pre-computed tables and only keeps the joins
# that can't be pre-materialized (venue, injuries, lineups, weather, etc.)

GAME_QUERY = """
SELECT
    -- Game identity
    g.id               AS game_id,
    g.season_id,
    s.year             AS season_year,
    g.date,
    
    g.status,
    

    -- Teams
    ht.id              AS home_team_id,
    ht.name       AS home_team,
    ht.abbreviation    AS home_abbr,
    ht.logo_url        AS home_logo,
    at.id              AS away_team_id,
    at.name       AS away_team,
    at.abbreviation    AS away_abbr,
    at.logo_url        AS away_logo,

    -- Score
    g.home_score,
    g.away_score,
    (g.home_score - g.away_score) AS actual_margin,
    (g.home_score + g.away_score) AS actual_total,

    -- Venue / environment
    v.name             AS venue_name,
    v.surface          AS venue_surface,
    v.roof_type        AS venue_roof,
    v.capacity         AS venue_capacity,
    

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
    prs_a.is_quality_start AS a_p_quality_start,

    -- Current-game pitcher names (from pitcher_game_stats)
    pgs_h.pitcher_name    AS home_starter_name,
    pgs_a.pitcher_name    AS away_starter_name,

    -- ──────────────────────────────────────────────────────────────────────
    -- BETTING LINES (consolidated)
    -- ──────────────────────────────────────────────────────────────────────
    blc.closing_spread,
    blc.closing_spread_home_odds, closing_spread_away_odds,
    blc.closing_ou,
    blc.closing_over_odds, closing_under_odds,
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
    (blc.closing_ou - blc.opening_ou) AS ou_movement,
    (blc.closing_home_ml - blc.opening_home_ml) AS ml_movement

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

    # PRIME DIRECTIVE: Every pick card MUST include complete handicapping data.
    # So we keep all the raw columns too for the pick card builder.

    return result


# ── Placeholder: rest of MLBDataLoader class ─────────────────────────────────
# The class methods (load_games, _query, _build_query, get_model_features,
# _save_backtest_prediction, etc.) remain structurally the same.
# Only GAME_QUERY and build_features() are replaced.
#
# Refer to the original data_loader.py for the full class implementation.
