"""NBA odds consolidation — picks one sportsbook per game consistently.

Rule:
  - Prefer FanDuel if it has complete data (opening + closing spread, OU, ML).
  - Otherwise use DraftKings if complete.
  - If both complete with matching values, prefer FanDuel.
  - If they disagree (different values), log a warning and use FanDuel.
  - Fallback: whichever book has more data.
"""

import logging
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text as sa_text
from datetime import datetime

logger = logging.getLogger(__name__)

SYNC_DATABASE_URL = "postgresql+psycopg2://earl:earl_dev_pass@localhost:5432/earl_knows_football"
DROP_AND_REPLACE = True


def is_opening(val):
    """Normalize the is_opening column to boolean."""
    if isinstance(val, str):
        return val.lower() in ("true", "t", "1", "yes")
    if isinstance(val, bool):
        return val
    return False


def run():
    engine = create_engine(SYNC_DATABASE_URL)

    # ── 1. Load raw data from nba.betting_lines ──
    logger.info("Loading NBA betting lines...")
    query = sa_text("""
        SELECT
            bl.game_id, bl.source, bl.sportsbook,
            bl.spread, bl.spread_home_odds, bl.spread_away_odds,
            bl.over_under, bl.over_odds, bl.under_odds,
            bl.home_moneyline, bl.away_moneyline,
            bl.home_implied_probability, bl.away_implied_probability,
            bl.is_opening,
            g.date AS game_time, g.home_score, g.away_score,
            ht.name AS home_team, at.name AS away_team,
            s.year, g.venue
        FROM nba.betting_lines bl
        JOIN nba.games g ON g.id = bl.game_id
        JOIN nba.teams ht ON ht.id = g.home_team_id
        JOIN nba.teams at ON at.id = g.away_team_id
        JOIN nba.seasons s ON s.id = g.season_id
        ORDER BY bl.game_id, bl.sportsbook, bl.is_opening
    """)

    df = pd.read_sql(query, engine)
    logger.info(f"Loaded {len(df)} rows from nba.betting_lines")

    if df.empty:
        logger.warning("No data to consolidate")
        return

    # Normalize is_opening
    df["is_opening"] = df["is_opening"].apply(is_opening)

    # Separate opening & closing
    opening = df[df["is_opening"] == True].copy()
    closing = df[df["is_opening"] == False].copy()
    logger.info(f"  Opening rows: {len(opening)}, Closing rows: {len(closing)}")

    # Game metadata lookup
    meta_cols = ["game_id", "year", "game_time", "home_team", "away_team",
                  "home_score", "away_score", "venue"]
    game_meta = df[meta_cols].drop_duplicates("game_id").set_index("game_id")

    # ── 2. Per-game sportsbook selection ──
    logger.info("Selecting best sportsbook per game...")

    def _complete(row):
        """Row has non-null spread AND over_under (i.e. is a full line set)."""
        return not pd.isna(row.get("spread")) and not pd.isna(row.get("over_under"))

    def _has_ml(row):
        """Row has at least one moneyline."""
        return not pd.isna(row.get("home_moneyline"))

    results = []
    fd_preferred = 0
    dk_fallback = 0
    disagreements = 0

    for gid in sorted(df["game_id"].unique()):
        fd_o = opening[(opening["game_id"] == gid) & (opening["sportsbook"] == "fanduel")]
        fd_c = closing[(closing["game_id"] == gid) & (closing["sportsbook"] == "fanduel")]
        dk_o = opening[(opening["game_id"] == gid) & (opening["sportsbook"] == "draftkings")]
        dk_c = closing[(closing["game_id"] == gid) & (closing["sportsbook"] == "draftkings")]

        fd_open_row = fd_o.iloc[0] if len(fd_o) > 0 else None
        fd_close_row = fd_c.iloc[0] if len(fd_c) > 0 else None
        dk_open_row = dk_o.iloc[0] if len(dk_o) > 0 else None
        dk_close_row = dk_c.iloc[0] if len(dk_c) > 0 else None

        # Completeness checks
        fd_complete = (
            fd_open_row is not None and fd_close_row is not None
            and _complete(fd_open_row) and _complete(fd_close_row)
        )
        dk_complete = (
            dk_open_row is not None and dk_close_row is not None
            and _complete(dk_open_row) and _complete(dk_close_row)
        )

        # ── Decision ──
        if fd_complete and dk_complete:
            # Both complete — prefer FD, log if values disagree
            close_agree = (
                fd_close_row["spread"] == dk_close_row["spread"]
                and fd_close_row["over_under"] == dk_close_row["over_under"]
            )
            open_agree = (
                fd_open_row["spread"] == dk_open_row["spread"]
                and fd_open_row["over_under"] == dk_open_row["over_under"]
            )
            if not (close_agree and open_agree):
                disagreements += 1
                logger.debug(
                    f"Game {gid}: FD & DK disagree — using FD "
                    f"(FD sp={fd_close_row['spread']} ou={fd_close_row['over_under']} | "
                    f"DK sp={dk_close_row['spread']} ou={dk_close_row['over_under']})"
                )
            chosen = "fanduel"
            open_row, close_row = fd_open_row, fd_close_row
            fd_preferred += 1

        elif fd_complete:
            chosen = "fanduel"
            open_row, close_row = fd_open_row, fd_close_row
            fd_preferred += 1

        elif dk_complete:
            chosen = "draftkings"
            open_row, close_row = dk_open_row, dk_close_row
            dk_fallback += 1

        elif fd_open_row is not None or fd_close_row is not None or dk_open_row is not None or dk_close_row is not None:
            # Incomplete — pick whichever has more data
            fd_score = 0
            if fd_open_row is not None:
                fd_score += int(_complete(fd_open_row)) * 3 + int(_has_ml(fd_open_row)) * 1
            if fd_close_row is not None:
                fd_score += int(_complete(fd_close_row)) * 3 + int(_has_ml(fd_close_row)) * 1

            dk_score = 0
            if dk_open_row is not None:
                dk_score += int(_complete(dk_open_row)) * 3 + int(_has_ml(dk_open_row)) * 1
            if dk_close_row is not None:
                dk_score += int(_complete(dk_close_row)) * 3 + int(_has_ml(dk_close_row)) * 1

            chosen = "fanduel" if fd_score >= dk_score else "draftkings"
            if chosen == "fanduel":
                open_row = fd_open_row
                close_row = fd_close_row
                fd_preferred += 1
            else:
                open_row = dk_open_row
                close_row = dk_close_row
                dk_fallback += 1
        else:
            continue  # no data at all

        # ── 3. Build consolidated row ──
        meta = game_meta.loc[gid]
        row = {
            "game_id": gid,
            "year": meta["year"],
            "game_time": meta["game_time"],
            "home_team": meta["home_team"],
            "away_team": meta["away_team"],
            "home_score": meta["home_score"],
            "away_score": meta["away_score"],
            "venue": meta["venue"],
            "sportsbook": chosen,
        }

        # Helper: set a column if the source row exists and value is not null
        def _set(db_col, val):
            row[db_col] = val if val is not None and not pd.isna(val) else None

        if close_row is not None:
            _set("closing_spread", close_row["spread"])
            _set("closing_ou", close_row["over_under"])
            _set("closing_home_ml", close_row["home_moneyline"])
            _set("closing_away_ml", close_row["away_moneyline"])
            _set("closing_over_odds", close_row["over_odds"])
            _set("closing_under_odds", close_row["under_odds"])
            _set("closing_spread_home_odds", close_row["spread_home_odds"])
            _set("closing_spread_away_odds", close_row["spread_away_odds"])
            _set("closing_home_implied_probability", close_row["home_implied_probability"])
            _set("closing_away_implied_probability", close_row["away_implied_probability"])

        if open_row is not None:
            _set("opening_spread", open_row["spread"])
            _set("opening_ou", open_row["over_under"])
            _set("opening_home_ml", open_row["home_moneyline"])
            _set("opening_away_ml", open_row["away_moneyline"])
            _set("opening_over_odds", open_row["over_odds"])
            _set("opening_under_odds", open_row["under_odds"])
            _set("opening_spread_home_odds", open_row["spread_home_odds"])
            _set("opening_spread_away_odds", open_row["spread_away_odds"])
            _set("opening_home_implied_probability", open_row["home_implied_probability"])
            _set("opening_away_implied_probability", open_row["away_implied_probability"])

        # Set sportsbook source columns (same book for all)
        _set("closing_spread_sportsbook", chosen)
        _set("closing_ou_sportsbook", chosen)
        _set("closing_home_ml_sportsbook", chosen)
        _set("closing_away_ml_sportsbook", chosen)
        _set("opening_ou_sportsbook", chosen)
        _set("opening_spread_sportsbook", chosen)
        _set("opening_home_ml_sportsbook", chosen)
        _set("opening_away_ml_sportsbook", chosen)
        _set("closing_over_odds_sportsbook", chosen)
        _set("closing_under_odds_sportsbook", chosen)
        _set("closing_spread_home_odds_sportsbook", chosen)
        _set("closing_spread_away_odds_sportsbook", chosen)
        _set("opening_over_odds_sportsbook", chosen)
        _set("opening_under_odds_sportsbook", chosen)
        _set("opening_spread_home_odds_sportsbook", chosen)
        _set("opening_spread_away_odds_sportsbook", chosen)

        has_verified = row.get("closing_ou") is not None and row.get("opening_ou") is not None
        row["has_verified_ou"] = has_verified

        results.append(row)

    logger.info(f"  FD preferred: {fd_preferred} games")
    logger.info(f"  DK fallback:   {dk_fallback} games")
    logger.info(f"  Disagreements logged: {disagreements}")

    result = pd.DataFrame(results)
    logger.info(f"Built consolidated dataset: {len(result)} games")

    if result.empty:
        logger.warning("No results to write")
        return

    # ── 4. Summary ──
    verified = result[result["has_verified_ou"] == True]
    missing_ou = result[result["closing_ou"].notna() & result["opening_ou"].isna()]
    no_ou = result[result["closing_ou"].isna()]

    logger.info(f"  Games with verified opening+closing OU: {len(verified)}")
    logger.info(f"  Games with closing only (no opening OU): {len(missing_ou)}")
    logger.info(f"  Games with no OU at all: {len(no_ou)}")

    # ── 5. Write ──
    logger.info("Writing to nba.betting_lines_consolidated...")

    db_cols = [
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

    # Only keep columns that exist in the DataFrame
    write_cols = [c for c in db_cols if c in result.columns]
    insert_df = result[write_cols].copy()

    # Set status = "final" for all rows
    insert_df["status"] = "final"

    if DROP_AND_REPLACE:
        logger.info("  Dropping and replacing table contents...")
        with engine.begin() as conn:
            conn.execute(sa_text("TRUNCATE TABLE nba.betting_lines_consolidated"))

    # Write in batches to avoid OOM
    batch_size = 500
    for i in range(0, len(insert_df), batch_size):
        batch = insert_df.iloc[i:i+batch_size]
        batch.to_sql(
            "betting_lines_consolidated",
            engine,
            schema="nba",
            if_exists="append",
            index=False,
            method="multi",
        )
        logger.info(f"  Wrote batch {i//batch_size + 1}/{(len(insert_df)-1)//batch_size + 1} ({len(batch)} rows)")

    logger.info(f"✅ Wrote {len(insert_df)} rows to nba.betting_lines_consolidated")
    return result
