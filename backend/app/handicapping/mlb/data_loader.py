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


# ── Master game-level SQL query ──────────────────────────────────────────────

GAME_QUERY = """
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
    c.opening_ou                                            AS opening_total,
    c.opening_home_ml                                       AS opening_home_ml,
    c.opening_away_ml                                       AS opening_away_ml,
    c.opening_spread_sportsbook,
    c.closing_home_implied_probability,
    c.closing_away_implied_probability,
    c.opening_home_implied_probability,
    c.opening_away_implied_probability

FROM mlb.games g
LEFT JOIN mlb.teams h         ON h.id = g.home_team_id
LEFT JOIN mlb.teams a         ON a.id = g.away_team_id
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
    # ── Pitcher-derived ──
    "h_pitcher_era_l20": "Home pitcher ERA over last 20 appearances",
    "a_pitcher_era_l20": "Away pitcher ERA over last 20 appearances",
    "h_pitcher_era_l5": "Home pitcher ERA over last 5 appearances",
    "a_pitcher_era_l5": "Away pitcher ERA over last 5 appearances",
    "h_pitcher_k9_l20": "Home pitcher K/9 over last 20 appearances",
    "a_pitcher_k9_l20": "Away pitcher K/9 over last 20 appearances",
    "h_pitcher_whip_l20": "Home pitcher WHIP over last 20 appearances",
    "a_pitcher_whip_l20": "Away pitcher WHIP over last 20 appearances",
    "h_pitcher_kbb_rate_l20": "Home pitcher K/BB rate over last 20 appearances",
    "a_pitcher_kbb_rate_l20": "Away pitcher K/BB rate over last 20 appearances",
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



# ── Feature engineering (consolidated build_features) ────────────────────────


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
    ]].copy()

    # Team-level roll-up: one row per team-game
    home = pivot_df.rename(columns={"ha": "team", "aa": "opp", "home_score": "rf", "away_score": "ra"})
    home["home_ind"] = 1
    away = pivot_df.rename(columns={"aa": "team", "ha": "opp", "away_score": "rf", "home_score": "ra"})
    away["home_ind"] = 0

    tg = pd.concat([home, away], ignore_index=True)
    tg = tg.sort_values(["team", "game_date"]).reset_index(drop=True)
    tg["year"] = tg["season_year"].fillna(tg["game_date"].dt.year.fillna(2024)).astype(int)

    log("  Pivoted to %d team-game rows", len(tg))

    # ── 3. Rolling stats per team ──

    def rolling_mean_safe(series: pd.Series, window: int) -> pd.Series:
        """Expanding mean for early season (first ``window`` games),
        then rolling mean after that, all shift(1) on a per-team basis."""
        expanded = series.expanding(min_periods=1).mean().shift(1)
        rolled = series.rolling(window=window, min_periods=1).mean().shift(1)
        # Use expanding until we have enough games, then rolling
        n = series.groupby(tg["team"] if series.name == tg["rf"].name else ...).cumcount() + 1
        result = pd.Series(index=series.index, dtype=float)
        for i in range(len(series)):
            if n.iloc[i] <= window:
                result.iloc[i] = expanded.iloc[i]
            else:
                result.iloc[i] = rolled.iloc[i]
        return result

    # We'll compute these manually with groupby + expanding/rolling
    log("  Computing rolling team stats (rf, ra)...")

    # Add season_game_no for team/season
    tg["season_game_no"] = tg.groupby(["team", "year"]).cumcount() + 1

    # Per-team expanding/rolling averages
    tg["rf_avg"] = tg.groupby("team")["rf"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )
    tg["ra_avg"] = tg.groupby("team")["ra"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )
    tg["rf10"] = tg.groupby("team")["rf"].transform(
        lambda s: s.rolling(10, min_periods=1).mean().shift(1)
    )
    tg["ra10"] = tg.groupby("team")["ra"].transform(
        lambda s: s.rolling(10, min_periods=1).mean().shift(1)
    )
    tg["rf5"] = tg.groupby("team")["rf"].transform(
        lambda s: s.rolling(5, min_periods=1).mean().shift(1)
    )
    tg["ra5"] = tg.groupby("team")["ra"].transform(
        lambda s: s.rolling(5, min_periods=1).mean().shift(1)
    )

    # Home-only and away-only splits
    tg["rf_home"] = tg.groupby("team")["rf"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )
    tg["rf_away"] = tg.groupby("team")["rf"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )
    # Home/away splits using the home_ind
    home_games = tg[tg["home_ind"] == 1].groupby("team")["rf"]
    away_games = tg[tg["home_ind"] == 0].groupby("team")["rf"]
    # Map back
    tg["h_home_rf"] = tg.groupby("team")["rf"].transform(
        lambda s: (
            tg.loc[s.index, "rf"]
            .where(tg.loc[s.index, "home_ind"] == 1, None)
            .expanding(min_periods=1).mean().shift(1)
        )
    )
    tg["a_away_rf"] = tg.groupby("team")["rf"].transform(
        lambda s: (
            tg.loc[s.index, "rf"]
            .where(tg.loc[s.index, "home_ind"] == 0, None)
            .expanding(min_periods=1).mean().shift(1)
        )
    )

    log("  Rolling team stats computed")

    # ── 3b. Prior-season smoothing for early-season games ──

    log("  Computing prior-season averages for early-season smoothing...")
    team_season_avg = tg.groupby(["team", "year"]).agg(
        prior_pf=("rf", "mean"),
        prior_pa=("ra", "mean"),
    ).reset_index()

    team_prior = {}
    for _, row in team_season_avg.iterrows():
        team_prior[(row["team"], row["year"])] = (row["prior_pf"], row["prior_pa"])

    BLEND_WINDOW = 10
    log("  Blending rolling stats with prior-season averages...")

    for window in [5, 10, 20]:
        early_mask = tg["season_game_no"] <= BLEND_WINDOW

        # Compute expanding avg for this window
        col_name = f"rf{window}" if window != 20 else "rf_avg"

        # For early games, blend: 0.7 * current_avg + 0.3 * prior_season_avg
        for idx in tg[early_mask].index:
            row = tg.loc[idx]
            prior = team_prior.get((row["team"], row["year"] - 1))
            if prior is not None and window != 20:
                cur_rf = tg.loc[idx, f"rf{window}"] if f"rf{window}" in tg.columns else row["rf_avg"]
                cur_ra = tg.loc[idx, f"ra{window}"] if f"ra{window}" in tg.columns else row["ra_avg"]
                if pd.notna(cur_rf):
                    tg.loc[idx, f"rf{window}"] = 0.7 * cur_rf + 0.3 * prior[0]
                if pd.notna(cur_ra):
                    tg.loc[idx, f"ra{window}"] = 0.7 * cur_ra + 0.3 * prior[1]

    log("  Prior-season blending applied")

    # ── 3c. Win percentage (also with prior-season smoothing) ──

    log("  Computing win percentages...")
    # Remap result: 1 for win, 0 for loss
    tg["win"] = (tg["rf"] > tg["ra"]).astype(int)
    tg["winpct"] = tg.groupby("team")["win"].transform(
        lambda s: s.expanding(min_periods=1).mean().shift(1)
    )

    # Blend win% with prior season
    for idx in tg[tg["season_game_no"] <= BLEND_WINDOW].index:
        row = tg.loc[idx]
        prior = team_prior.get((row["team"], row["year"] - 1))
        if prior is not None:
            cur_wp = tg.loc[idx, "winpct"]
            # prior win% ~ prior_pf / (prior_pf + prior_pa)
            prior_wp = prior[0] / (prior[0] + prior[1]) if (prior[0] + prior[1]) > 0 else 0.5
            if pd.notna(cur_wp):
                tg.loc[idx, "winpct"] = 0.7 * cur_wp + 0.3 * prior_wp

    # L10 win percentage (raw rolling, no blend)
    tg["winpct_l10"] = tg.groupby("team")["win"].transform(
        lambda s: s.rolling(10, min_periods=1).mean().shift(1)
    )

    # ── 3d. Form (exponential moving average win %) ──

    log("  Computing form (exponential MA of wins)...")
    tg["form_l10"] = tg.groupby("team")["win"].transform(
        lambda s: s.ewm(span=10, min_periods=1).mean().shift(1)
    )

    log("  Rolling stats done — %d team-game rows", len(tg))

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
        "form_l10": "h_form_l10",
    })[["game_id", "ha", "h_winpct", "h_winpct_l10",
        "h_rf_avg", "h_ra_avg", "h_rf10", "h_ra10",
        "h_rf5", "h_ra5", "h_form_l10",
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
        "form_l10": "a_form_l10",
    })[["game_id", "aa", "a_winpct", "a_winpct_l10",
        "a_rf_avg", "a_ra_avg", "a_rf10", "a_ra10",
        "a_rf5", "a_ra5", "a_form_l10"]]

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

    # Rest days
    for team_col, rest_col in [("ha", "rest_h"), ("aa", "rest_a")]:
        te = feats[["game_id", team_col, "game_date"]].copy()
        te = te.sort_values([team_col, "game_date"])
        te["next_date"] = te.groupby(team_col)["game_date"].shift(1)
        te["rest"] = (te["game_date"] - te["next_date"]).dt.days
        feats[rest_col] = te["rest"].values

    feats["rest_diff"] = feats["rest_h"] - feats["rest_a"]

    # Division
    feats["is_div"] = (feats["hdiv"] == feats["adiv"]).astype(int)

    # Dome
    if "roof_type" in feats.columns:
        feats["is_dome"] = feats["roof_type"].fillna("").str.lower().isin(
            ["dome", "retractable", "dome (closed)", "dome (open)", "retractable (closed)", "retractable (open)"]
        ).astype(int)
    else:
        feats["is_dome"] = 0

    # Travel miles & TZ diff — simplified proxy using division
    # True travel distance needs a venue → lat/lon mapping; for now use division as proxy
    feats["travel_miles"] = feats.apply(
        lambda r: 0 if r.get("hdiv") == r.get("adiv") else 500 if r.get("hdiv") and r.get("adiv") else 0,
        axis=1,
    )
    feats["tz_diff"] = feats.apply(
        lambda r: 0 if r.get("hdiv") == r.get("adiv") else 1 if r.get("hdiv") and r.get("adiv") else 0,
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

    feats["home_implied"] = feats["home_implied_probability"]
    feats["away_implied"] = feats["away_implied_probability"]
    feats["h_implied"] = feats["home_implied_probability"]
    feats["a_implied"] = feats["away_implied_probability"]

    # ── 4.5. Alias columns to match ATS_FEATURES naming ──
    # These aliases ensure the ATS feature set (", \"ATS_FEATURES\", ") from the model file
    # can find the columns it expects

    # h_home_ra: home team's runs-allowed when at home (home-split RA expanding mean)
    if "ha" in feats.columns and "h_ra_avg" in feats.columns:
        # We already have home team RA average (h_ra_avg), which includes all games.
        # For h_home_ra we want a proper home-split. We'll approximate it as h_ra_avg
        # since we don't have home-split RA in feats yet.
        feats["h_home_ra"] = feats["h_ra_avg"]

    # a_home_rf: alias for a_away_rf (away team's RF on the road)
    if "a_away_rf" in feats.columns:
        feats["a_home_rf"] = feats["a_away_rf"]

    # a_home_ra: alias for a_ra_avg (away team RA average)
    if "a_ra_avg" in feats.columns:
        feats["a_home_ra"] = feats["a_ra_avg"]

    # _20 window aliases (using _10 since we don't have 20-game windows)
    for a, b in [("h_ra20", "h_ra10"), ("a_ra20", "a_ra10"),
                  ("h_rf20", "h_rf10"), ("a_rf20", "a_rf10")]:
        if b in feats.columns:
            feats[a] = feats[b]

    # Implied total (blended rolling runs-for + runs-allowed for both teams)
    # This matches the OU model definition: avg of home rf, home ra, away rf, away ra
    h_rf = feats.get("h_rf10", feats.get("h_rf_avg", 4.5))
    h_ra = feats.get("h_ra10", feats.get("h_ra_avg", 4.5))
    a_rf = feats.get("a_rf10", feats.get("a_rf_avg", 4.5))
    a_ra = feats.get("a_ra10", feats.get("a_ra_avg", 4.5))
    feats["implied_total"] = (h_rf.fillna(4.5) + h_ra.fillna(4.5) + a_rf.fillna(4.5) + a_ra.fillna(4.5)) / 2
    feats["implied_total"] = feats["implied_total"].clip(lower=3, upper=16)

    # ── 8. Line movement features ──

    if "over_under" in feats.columns and "opening_total" in feats.columns:
        feats["ou_movement"] = feats["over_under"] - feats["opening_total"]
    else:
        feats["ou_movement"] = 0.0

    feats["ml_implied_movement"] = (
        feats["home_implied_probability"] - feats["opening_home_implied"]
    )

    # ── 9. Pitcher features (rolling windows) ──

    # Pitcher features are tricky because we need pitcher-change context.
    # For now we use team-level pitcher appearance data as a proxy.
    # In a future iteration, this will use the dedicated pitcher_appearances table.
    # Best approximation: the last 20/5 team games reflect the current pitcher's context.
    # We'll create stub columns that can be filled by the actual pitcher pipeline.

    # These columns will be populated by the actual pitcher feature builder
    # when run in production.  For now, compute from team rolling averages.
    pitcher_windows = [5, 20]
    for pw in pitcher_windows:
        # Team combined ERA proxy
        r_col = f"ra{pw}" if pw != 20 else "ra"
        for side, team_col in [("h", "ha"), ("a", "aa")]:
            era_col = f"{side}_pitcher_era_l{pw}"
            k9_col = f"{side}_pitcher_k9_l{pw}"
            whip_col = f"{side}_pitcher_whip_l{pw}"
            kbb_col = f"{side}_pitcher_kbb_rate_l{pw}"

            # ERA ~ (RA*9) per game (runs per 9 = RA/team's avg IP/9 proxy ≈ 9)
            if r_col in tg.columns:
                temp = tg.rename(columns={"team": team_col, r_col: "ra_tmp"})[["game_id", team_col, "ra_tmp"]]
                temp = temp[temp["ra_tmp"].notna()]
                feats = feats.merge(
                    temp.rename(columns={"ra_tmp": era_col}),
                    on=["game_id", team_col],
                    how="left",
                )
            else:
                feats[era_col] = 4.0

            # K/9, WHIP, K/BB — default league-average stubs
            feats[k9_col] = 8.5
            feats[whip_col] = 1.30
            feats[kbb_col] = 2.5

            # Team-specific pitcher from name (basic split)
            pit_col = f"{side}_pitcher_name" if pw == 5 else f"{side}_pitcher_name"

    # Bullpen features (baseline values)
    feats["h_bullpen_era_l5"] = 4.0
    feats["a_bullpen_era_l5"] = 4.0
    feats["h_bullpen_ip_l5"] = 5.0
    feats["a_bullpen_ip_l5"] = 5.0

    log("  Pitcher stubs added — full pitcher pipeline TBD")

    # ── 10. Park factor ──

    if "venue" in feats.columns:
        venue_games = feats[feats["game_type"] == "Regular Season"].copy()
        venue_games["venue"] = venue_games["venue"].fillna("Unknown")
        venue_total = venue_games.groupby("venue")["over_under"].agg(["mean", "count"])
        venue_total = venue_total[venue_total["count"] >= 20]  # minimum sample
        league_avg_ou = venue_games["over_under"].mean() if "over_under" in venue_games.columns else 8.5
        if league_avg_ou > 0 and not venue_total.empty:
            venue_total["factor"] = venue_total["mean"] / league_avg_ou
            feats["park_factor"] = feats["venue"].map(venue_total["factor"]).fillna(1.0)
        else:
            feats["park_factor"] = 1.0
    else:
        feats["park_factor"] = 1.0

    log("  Park factors computed")

    # ── 11. Team average total, combo ERA, combo ERA diff ──

    # Total runs per game involving this team
    tg["total_runs"] = tg["rf"] + tg["ra"]

    # Average total per team over rolling 10
    total_team_avg = tg.groupby("team")["total_runs"].transform(
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

    # ── 13. Fill NaNs ──
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

        Returns
        -------
        pd.DataFrame
            One row per game, with all columns from GAME_QUERY.
        """
        engine = create_engine(self._db_url)
        try:
            return self._query(engine, seasons=seasons, status=status,
                               limit=limit, include_upcoming=include_upcoming)
        finally:
            engine.dispose()

    async def load_games_async(
        self,
        engine: AsyncEngine,
        seasons: Optional[List[int]] = None,
        status: str = "FINAL",
        limit: Optional[int] = None,
        include_upcoming: bool = False,
    ) -> pd.DataFrame:
        """Load game data as a pandas DataFrame (async, using an existing engine)."""
        return await self._query_async(engine, seasons=seasons, status=status,
                                       limit=limit, include_upcoming=include_upcoming)

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
    ) -> str:
        """Build the SQL query with filters."""
        conditions: List[str] = []

        if seasons:
            placeholders = ", ".join(str(s) for s in seasons)
            conditions.append(f"s.year IN ({placeholders})")

        if status is not None and not include_upcoming:
            conditions.append(f"g.status = '{status}'")
        elif include_upcoming:
            conditions.append("g.status IS NOT NULL")

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
    ) -> pd.DataFrame:
        sql = self._build_query(seasons, status, limit, include_upcoming)
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
    ) -> pd.DataFrame:
        sql = self._build_query(seasons, status, limit, include_upcoming)
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
