"""
Incremental or full MLB betting line consolidation.

Takes raw betting_lines rows (opening/closing pairs) and builds
mlb.betting_lines_consolidated with one row per game.

Designed for the lines-and-picks task to run after each lines snapshot.
"""

import os
import math
import logging
import argparse
import sys
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

VALID_OU_MIN = 5.0
VALID_OU_MAX = 14.0
VALID_ML_MAX_ABS = 500

SYNC_DB = os.environ.get(
    "SYNC_DATABASE_URL",
    "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)


def consolidate(game_ids: list[int] | None = None):
    """Consolidate lines into mlb.betting_lines_consolidated.

    Args:
        game_ids: If set, only process these games (incremental).
                  If None, process all games (full rebuild).
    """
    engine = create_engine(SYNC_DB, pool_size=2)

    game_filter = ""
    if game_ids:
        ids_str = ",".join(str(gid) for gid in game_ids)
        game_filter = f"AND bl.game_id IN ({ids_str})"
        logger.info(f"Incremental consolidation for {len(game_ids)} games")
    else:
        logger.info("Full consolidation of all games")

    logger.info("Loading MLB betting lines...")
    df = pd.read_sql(f"""
        SELECT
            bl.game_id,
            bl.is_opening,
            bl.source,
            bl.spread,
            bl.over_under,
            bl.home_moneyline,
            bl.away_moneyline,
            bl.home_implied_probability,
            bl.away_implied_probability,
            bl.spread_home_odds,
            bl.spread_away_odds,
            bl.over_odds,
            bl.under_odds,
            bl.recorded_at,
            g.date as game_time
        FROM mlb.betting_lines bl
        JOIN mlb.games g ON g.id = bl.game_id
        JOIN mlb.seasons s ON s.id = g.season_id
        WHERE bl.is_opening IN ('false', 'f', 'true', 't')
          AND bl.source IN ('the_odds_api_current', 'the_odds_api_historical')
          {game_filter}
    """, engine)
    logger.info(f"  Loaded {len(df)} lines for {df['game_id'].nunique()} games")

    if df.empty:
        logger.info("No lines to consolidate")
        return {"updated": 0, "games": 0}

    # Validate numerical fields
    valid_ou = df["over_under"].between(VALID_OU_MIN, VALID_OU_MAX) | df["over_under"].isna()
    valid_ml_home = (df["home_moneyline"].fillna(0).abs() <= VALID_ML_MAX_ABS) | df["home_moneyline"].isna()
    valid_ml_away = (df["away_moneyline"].fillna(0).abs() <= VALID_ML_MAX_ABS) | df["away_moneyline"].isna()
    df["valid"] = valid_ou & valid_ml_home & valid_ml_away
    invalid_count = (~df["valid"]).sum()
    df = df[df["valid"]].copy()
    if invalid_count:
        logger.info(f"  Dropped {invalid_count} invalid lines")

    # Split into opening and closing
    opening = df[df["is_opening"].isin(["true", "t"])].copy()
    closing = df[df["is_opening"].isin(["false", "f"])].copy()

    # For each game, pick best opening and best closing (by source quality)
    def source_quality(s):
        if s == "the_odds_api_current":
            return 1
        return 9

    if not opening.empty:
        opening["quality"] = opening["source"].apply(source_quality)
        opening_idx = opening.groupby("game_id")["quality"].idxmin()
        opening = opening.loc[opening_idx]
    if not closing.empty:
        closing["quality"] = closing["source"].apply(source_quality)
        closing_idx = closing.groupby("game_id")["quality"].idxmin()
        closing = closing.loc[closing_idx]

    # Merge opening and closing on game_id
    merged = closing.merge(
        opening,
        on="game_id",
        how="left",
        suffixes=("_closing", "_opening"),
    )

    logger.info(f"  Merged {len(merged)} games")

    # Map columns to the consolidated table schema
    col_map = {
        "closing_spread": "spread_closing",
        "closing_ou": "over_under_closing",
        "closing_home_ml": "home_moneyline_closing",
        "closing_away_ml": "away_moneyline_closing",
        "closing_home_implied_probability": "home_implied_probability_closing",
        "closing_away_implied_probability": "away_implied_probability_closing",
        "closing_over_odds": "over_odds_closing",
        "closing_under_odds": "under_odds_closing",
        "closing_spread_home_odds": "spread_home_odds_closing",
        "closing_spread_away_odds": "spread_away_odds_closing",
        "opening_spread": "spread_opening",
        "opening_ou": "over_under_opening",
        "opening_home_ml": "home_moneyline_opening",
        "opening_away_ml": "away_moneyline_opening",
        "opening_home_implied_probability": "home_implied_probability_opening",
        "opening_away_implied_probability": "away_implied_probability_opening",
        "opening_over_odds": "over_odds_opening",
        "opening_under_odds": "under_odds_opening",
        "opening_spread_home_odds": "spread_home_odds_opening",
        "opening_spread_away_odds": "spread_away_odds_opening",
    }

    # Build the records for upsert
    records = []
    for col_name, src_col in col_map.items():
        if src_col in merged.columns:
            merged.rename(columns={src_col: col_name}, inplace=True)

    merged["game_time"] = merged["game_time_closing"]
    merged["has_verified_ou"] = True

    cols = [
        "game_id", "game_time",
        "closing_spread", "closing_ou", "closing_home_ml", "closing_away_ml",
        "closing_home_implied_probability", "closing_away_implied_probability",
        "closing_over_odds", "closing_under_odds",
        "closing_spread_home_odds", "closing_spread_away_odds",
        "opening_spread", "opening_ou", "opening_home_ml", "opening_away_ml",
        "opening_home_implied_probability", "opening_away_implied_probability",
        "opening_over_odds", "opening_under_odds",
        "opening_spread_home_odds", "opening_spread_away_odds",
        "has_verified_ou",
    ]

    existing_cols = [c for c in cols if c in merged.columns]
    records = merged[existing_cols].to_dict("records")

    # Convert pandas types to Python native for psycopg2 compatibility
    for rec in records:
        for k, v in rec.items():
            if hasattr(v, "to_pydatetime"):
                rec[k] = v.to_pydatetime()
            elif isinstance(v, float) and (pd.isna(v) or math.isnan(v)):
                rec[k] = None

    # Build the SET clause dynamically from the cols we have
    update_fields = [
        "closing_spread", "closing_ou", "closing_home_ml", "closing_away_ml",
        "closing_home_implied_probability", "closing_away_implied_probability",
        "closing_over_odds", "closing_under_odds",
        "closing_spread_home_odds", "closing_spread_away_odds",
        "opening_spread", "opening_ou", "opening_home_ml", "opening_away_ml",
        "opening_home_implied_probability", "opening_away_implied_probability",
        "opening_over_odds", "opening_under_odds",
        "opening_spread_home_odds", "opening_spread_away_odds",
        "has_verified_ou",
    ]
    set_clauses = ", ".join(
        f"{f} = EXCLUDED.{f}"
        for f in update_fields
        if f in merged.columns
    )

    insert_cols = ", ".join(existing_cols)
    param_placeholders = ", ".join(f":{c}" for c in existing_cols)

    sql = f"""
        INSERT INTO mlb.betting_lines_consolidated
            ({insert_cols})
        VALUES ({param_placeholders})
        ON CONFLICT (game_id) DO UPDATE SET
            {set_clauses}
    """

    with engine.begin() as conn:
        result = conn.execute(text(sql), records)
        logger.info(f"  {result.rowcount} rows affected")
        return {"updated": result.rowcount, "games": len(records)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=str, help="Comma-separated game IDs")
    args = parser.parse_args()

    game_ids = None
    if args.games:
        game_ids = [int(x.strip()) for x in args.games.split(",") if x.strip()]

    result = consolidate(game_ids)
    print(f"Result: {result}")
