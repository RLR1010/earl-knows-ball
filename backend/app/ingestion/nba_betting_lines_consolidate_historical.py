"""
Consolidate historical NBA betting lines (pre-odds-API era) into
nba.betting_lines_consolidated.

Source of truth for seasons < 30 (before 2020-21): nba.betting_lines_old,
which is populated by backfill_nba_lines_old.py from the Kaggle
"Basketball Betting Dataset" (visualize25) — opening + closing lines for
every game back to 2007-08.

This script ONLY touches games with season_id < 30. Modern seasons (30+)
are managed by nba_odds_consolidated.py (The Odds API). Run order:
1) nba_odds_consolidated.py   (modern seasons)
2) nba_betting_lines_consolidate_historical.py   (old seasons)

Usage:
    python -m backend.app.ingestion.nba_betting_lines_consolidate_historical [--dry-run]
"""

import logging
import sys

import pandas as pd
from sqlalchemy import create_engine, text as sa_text

from backend.app.core.config import settings

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DB_URL = settings.database_url_sync
HISTORICAL_SPORTSBOOK = "nba_old"

# Mirror the exact column list used by nba_odds_consolidated.py
DB_COLS = [
    "game_id", "year", "game_time", "home_team", "away_team",
    "home_score", "away_score", "venue",
    "closing_spread", "closing_spread_sportsbook",
    "closing_ou", "closing_ou_sportsbook",
    "closing_home_ml", "closing_home_ml_sportsbook",
    "closing_away_ml", "closing_away_ml_sportsbook",
    "opening_ou", "opening_ou_sportsbook",
    "opening_spread", "opening_spread_sportsbook",
    "opening_home_ml", "opening_home_ml_sportsbook",
    "opening_away_ml", "opening_away_ml_sportsbook",
    "has_verified_ou",
    "closing_over_odds", "closing_over_odds_sportsbook",
    "closing_under_odds", "closing_under_odds_sportsbook",
    "closing_spread_home_odds", "closing_spread_home_odds_sportsbook",
    "closing_spread_away_odds", "closing_spread_away_odds_sportsbook",
    "closing_home_implied_probability", "closing_away_implied_probability",
    "opening_over_odds", "opening_over_odds_sportsbook",
    "opening_under_odds", "opening_under_odds_sportsbook",
    "opening_spread_home_odds", "opening_spread_home_odds_sportsbook",
    "opening_spread_away_odds", "opening_spread_away_odds_sportsbook",
    "opening_home_implied_probability", "opening_away_implied_probability",
]


def _is_opening(v):
    if v is None or pd.isna(v):
        return None
    return str(v).strip().lower() in ("t", "true", "1")


def consolidate_historical(engine, dry_run: bool = False) -> pd.DataFrame:
    """Rebuild consolidated rows for season_id < 30 from betting_lines_old."""
    # 1) Load raw historical lines with game context
    query = sa_text(
        """
        SELECT
            blo.game_id,
            g.season_id,
            s.year,
            g.date AS game_time,
            ht.name AS home_team,
            at.name AS away_team,
            g.home_score,
            g.away_score,
            g.venue,
            blo.sportsbook,
            blo.is_opening,
            blo.spread,
            blo.spread_home_odds,
            blo.spread_away_odds,
            blo.over_under,
            blo.over_odds,
            blo.under_odds,
            blo.home_moneyline,
            blo.away_moneyline,
            blo.home_implied_probability,
            blo.away_implied_probability
        FROM nba.betting_lines_old blo
        JOIN nba.games g ON g.id = blo.game_id
        JOIN nba.seasons s ON s.id = g.season_id
        JOIN nba.teams ht ON ht.id = g.home_team_id
        JOIN nba.teams at ON at.id = g.away_team_id
        WHERE g.season_id < 30
        ORDER BY blo.game_id, blo.is_opening
        """
    )
    df = pd.read_sql(query, engine)
    logger.info(f"Loaded {len(df)} historical line rows ({df['game_id'].nunique()} games)")

    if df.empty:
        logger.warning("No historical lines to consolidate")
        return df

    df["is_opening"] = df["is_opening"].apply(_is_opening)

    opening = df[df["is_opening"] == True].copy()
    closing = df[df["is_opening"] == False].copy()
    logger.info(f"  Opening rows: {len(opening)}, Closing rows: {len(closing)}")

    # 2) Game-level metadata
    meta_cols = ["game_id", "year", "game_time", "home_team", "away_team",
                 "home_score", "away_score", "venue"]
    game_meta = df[meta_cols].drop_duplicates("game_id").set_index("game_id")

    def _row_map(sub, prefix):
        """Return {game_id: {col: value}} for a set of line columns."""
        cols = {
            "spread": f"{prefix}_spread",
            "over_under": f"{prefix}_ou",
            "home_moneyline": f"{prefix}_home_ml",
            "away_moneyline": f"{prefix}_away_ml",
            "over_odds": f"{prefix}_over_odds",
            "under_odds": f"{prefix}_under_odds",
            "spread_home_odds": f"{prefix}_spread_home_odds",
            "spread_away_odds": f"{prefix}_spread_away_odds",
            "home_implied_probability": f"{prefix}_home_implied_probability",
            "away_implied_probability": f"{prefix}_away_implied_probability",
        }
        out = {}
        for gid, g in sub.groupby("game_id"):
            row = g.iloc[0]
            out[gid] = {out_col: row[src_col] for src_col, out_col in cols.items()}
        return out

    open_map = _row_map(opening, "opening")
    close_map = _row_map(closing, "closing")

    # 3) Assemble consolidated rows
    results = []
    for gid in game_meta.index:
        meta = game_meta.loc[gid]
        o = open_map.get(gid, {})
        c = close_map.get(gid, {})
        if not c and not o:
            continue
        row = {"game_id": int(gid)}
        for k, v in meta.items():
            row[k] = v
        for k, v in {**o, **c}.items():
            row[k] = v
        row["has_verified_ou"] = c.get("closing_ou") is not None and o.get("opening_ou") is not None
        results.append(row)

    result = pd.DataFrame(results)
    logger.info(f"Built {len(result)} historical consolidated games")

    if result.empty or dry_run:
        return result

    # 4) Write — wipe only the historical range, keep modern seasons intact
    with engine.begin() as conn:
        conn.execute(
            sa_text(
                "DELETE FROM nba.betting_lines_consolidated "
                "WHERE game_id IN (SELECT id FROM nba.games WHERE season_id < 30)"
            )
        )
        logger.info("Deleted old-season consolidated rows")

        write_df = result.copy()
        for c in DB_COLS:
            if c not in write_df.columns:
                write_df[c] = None
        write_df = write_df[DB_COLS]
        write_df["status"] = "final"
        write_df["game_time"] = pd.to_datetime(write_df["game_time"], utc=True)

        prov_cols = [c for c in write_df.columns if c.endswith("_sportsbook")]
        for c in prov_cols:
            write_df[c] = write_df[c].where(write_df[c].notna(), HISTORICAL_SPORTSBOOK)

        rows = write_df.to_dict("records")
        for i in range(0, len(rows), 500):
            batch = rows[i : i + 500]
            conn.execute(
                sa_text(
                    """
                    INSERT INTO nba.betting_lines_consolidated
                        (game_id, year, game_time, home_team, away_team,
                         home_score, away_score, venue, status,
                         closing_spread, closing_spread_sportsbook,
                         closing_ou, closing_ou_sportsbook,
                         closing_home_ml, closing_home_ml_sportsbook,
                         closing_away_ml, closing_away_ml_sportsbook,
                         opening_ou, opening_ou_sportsbook,
                         opening_spread, opening_spread_sportsbook,
                         opening_home_ml, opening_home_ml_sportsbook,
                         opening_away_ml, opening_away_ml_sportsbook,
                         has_verified_ou,
                         closing_over_odds, closing_over_odds_sportsbook,
                         closing_under_odds, closing_under_odds_sportsbook,
                         closing_spread_home_odds, closing_spread_home_odds_sportsbook,
                         closing_spread_away_odds, closing_spread_away_odds_sportsbook,
                         closing_home_implied_probability, closing_away_implied_probability,
                         opening_over_odds, opening_over_odds_sportsbook,
                         opening_under_odds, opening_under_odds_sportsbook,
                         opening_spread_home_odds, opening_spread_home_odds_sportsbook,
                         opening_spread_away_odds, opening_spread_away_odds_sportsbook,
                         opening_home_implied_probability, opening_away_implied_probability)
                    VALUES (
                        :game_id, :year, :game_time, :home_team, :away_team,
                        :home_score, :away_score, :venue, :status,
                        :closing_spread, :closing_spread_sportsbook,
                        :closing_ou, :closing_ou_sportsbook,
                        :closing_home_ml, :closing_home_ml_sportsbook,
                        :closing_away_ml, :closing_away_ml_sportsbook,
                        :opening_ou, :opening_ou_sportsbook,
                        :opening_spread, :opening_spread_sportsbook,
                        :opening_home_ml, :opening_home_ml_sportsbook,
                        :opening_away_ml, :opening_away_ml_sportsbook,
                        :has_verified_ou,
                        :closing_over_odds, :closing_over_odds_sportsbook,
                        :closing_under_odds, :closing_under_odds_sportsbook,
                        :closing_spread_home_odds, :closing_spread_home_odds_sportsbook,
                        :closing_spread_away_odds, :closing_spread_away_odds_sportsbook,
                        :closing_home_implied_probability, :closing_away_implied_probability,
                        :opening_over_odds, :opening_over_odds_sportsbook,
                        :opening_under_odds, :opening_under_odds_sportsbook,
                        :opening_spread_home_odds, :opening_spread_home_odds_sportsbook,
                        :opening_spread_away_odds, :opening_spread_away_odds_sportsbook,
                        :opening_home_implied_probability, :opening_away_implied_probability)
                    ON CONFLICT (game_id) DO NOTHING
                    """
                ),
                batch,
            )
    logger.info(f"Inserted {len(rows)} historical consolidated rows")
    return result


def main():
    dry_run = "--dry-run" in sys.argv
    engine = create_engine(DB_URL)
    try:
        result = consolidate_historical(engine, dry_run=dry_run)
        if dry_run:
            logger.info("DRY RUN — no changes written")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
