#!/usr/bin/env python3
"""
mlb_old_lines_consolidate.py

Consolidate historical opening lines from mlb.betting_lines_old into
mlb.betting_lines_consolidated.

betting_lines_old contains pre-2021 opening lines (run lines, totals, moneylines)
from a single aggregated source (no sportsbook distinction).

This script:
  1. Reads all rows from betting_lines_old (not yet in consolidated)
  2. Corrects implied probabilities (data has them scaled ×100 for positive odds)
  3. Maps data to opening_* columns, mirrors to closing_*
  4. Upserts into betting_lines_consolidated with source = 'mlbverse'

Usage:
    python scripts/mlb_old_lines_consolidate.py [--dry-run]
    python scripts/mlb_old_lines_consolidate.py [--rebuild]
"""

import argparse
import logging
import math

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "earl_knows_football",
    "user": "earl",
    "password": "earl2025",
}

SOURCE = "mlbverse"


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def american_to_implied_probability(american_odds: int) -> float:
    """
    Convert American odds to implied probability as a percentage (0-100).
    Positive odds: 100 / (odds + 100) * 100
    Negative odds: abs(odds) / (abs(odds) + 100) * 100
    """
    if american_odds is None:
        return None
    if american_odds > 0:
        return 100 / (american_odds + 100) * 100
    else:
        return abs(american_odds) / (abs(american_odds) + 100) * 100


def fetch_old_data(conn):
    """
    Fetch all rows from mlb.betting_lines_old not yet in consolidated.
    MLB data is all opening lines (is_opening=true).
    Returns a dict: {game_id: row_dict}
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT blo.*
        FROM mlb.betting_lines_old blo
        LEFT JOIN mlb.betting_lines_consolidated blc
            ON blc.game_id = blo.game_id
        WHERE blc.game_id IS NULL
        ORDER BY blo.game_id
    """
    cur.execute(query)
    rows = {row["game_id"]: dict(row) for row in cur.fetchall()}
    cur.close()
    logger.info(f"Fetched {len(rows)} rows from mlb.betting_lines_old (not yet in consolidated)")
    return rows


def fetch_game_info(conn, game_ids):
    """Fetch game metadata for the given game_ids."""
    if not game_ids:
        return {}
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    query = """
        SELECT
            g.id AS game_id,
            g.date AS game_time,
            home.name AS home_team,
            away.name AS away_team,
            s.year,
            g.home_score,
            g.away_score,
            g.venue,
            g.status::text
        FROM mlb.games g
        JOIN mlb.teams home ON home.id = g.home_team_id
        JOIN mlb.teams away ON away.id = g.away_team_id
        JOIN mlb.seasons s ON s.id = g.season_id
        WHERE g.id = ANY(%s)
    """
    cur.execute(query, [list(game_ids)])
    result = {row["game_id"]: dict(row) for row in cur.fetchall()}
    cur.close()
    return result


def process_rows(old_data, game_info):
    """
    Convert old_data rows into upsert-ready dicts for betting_lines_consolidated.
    MLB data is opening lines only (is_opening=true). The spread (run line)
    convention already matches ours (negative = home favored), no negation needed.
    Corrects implied probabilities and mirrors opening → closing.
    """
    result = []
    for game_id, row in sorted(old_data.items()):
        info = game_info.get(game_id, {})

        # Correct implied probabilities from raw odds
        home_ip = american_to_implied_probability(row.get("home_moneyline"))
        away_ip = american_to_implied_probability(row.get("away_moneyline"))

        # MLB spread (run line) uses our convention: negative = home favored.
        # No negation needed.
        spread_val = row.get("spread")

        entry = {
            "game_id": game_id,
            "game_time": info.get("game_time"),
            "home_team": info.get("home_team"),
            "away_team": info.get("away_team"),
            "year": info.get("year"),
            "home_score": info.get("home_score"),
            "away_score": info.get("away_score"),
            "venue": info.get("venue"),
            "status": info.get("status"),

            # ── Opening (actual data from betting_lines_old) ──
            "opening_spread": spread_val,
            "opening_spread_sportsbook": SOURCE,
            "opening_ou": row.get("over_under"),
            "opening_ou_sportsbook": SOURCE,
            "opening_home_ml": row.get("home_moneyline"),
            "opening_home_ml_sportsbook": SOURCE,
            "opening_away_ml": row.get("away_moneyline"),
            "opening_away_ml_sportsbook": SOURCE,

            "opening_over_odds": row.get("over_odds"),
            "opening_over_odds_sportsbook": SOURCE,
            "opening_under_odds": row.get("under_odds"),
            "opening_under_odds_sportsbook": SOURCE,
            "opening_spread_home_odds": row.get("spread_home_odds"),
            "opening_spread_home_odds_sportsbook": SOURCE,
            "opening_spread_away_odds": row.get("spread_away_odds"),
            "opening_spread_away_odds_sportsbook": SOURCE,
            "opening_home_implied_probability": home_ip,
            "opening_away_implied_probability": away_ip,

            # ── Closing — mirror opening (no separate closing data) ──
            "closing_spread": spread_val,
            "closing_spread_sportsbook": SOURCE,
            "closing_ou": row.get("over_under"),
            "closing_ou_sportsbook": SOURCE,
            "closing_home_ml": row.get("home_moneyline"),
            "closing_home_ml_sportsbook": SOURCE,
            "closing_away_ml": row.get("away_moneyline"),
            "closing_away_ml_sportsbook": SOURCE,

            "closing_over_odds": row.get("over_odds"),
            "closing_over_odds_sportsbook": SOURCE,
            "closing_under_odds": row.get("under_odds"),
            "closing_under_odds_sportsbook": SOURCE,
            "closing_spread_home_odds": row.get("spread_home_odds"),
            "closing_spread_home_odds_sportsbook": SOURCE,
            "closing_spread_away_odds": row.get("spread_away_odds"),
            "closing_spread_away_odds_sportsbook": SOURCE,
            "closing_home_implied_probability": home_ip,
            "closing_away_implied_probability": away_ip,
        }
        result.append(entry)

    return result


def upsert_rows(conn, rows):
    """Bulk upsert rows into betting_lines_consolidated."""
    if not rows:
        logger.info("No rows to upsert")
        return 0

    columns = [
        "game_id", "game_time", "home_team", "away_team", "year",
        "home_score", "away_score", "venue", "status",

        "opening_spread", "opening_spread_sportsbook",
        "opening_ou", "opening_ou_sportsbook",
        "opening_home_ml", "opening_home_ml_sportsbook",
        "opening_away_ml", "opening_away_ml_sportsbook",
        "opening_over_odds", "opening_over_odds_sportsbook",
        "opening_under_odds", "opening_under_odds_sportsbook",
        "opening_spread_home_odds", "opening_spread_home_odds_sportsbook",
        "opening_spread_away_odds", "opening_spread_away_odds_sportsbook",
        "opening_home_implied_probability", "opening_away_implied_probability",

        "closing_spread", "closing_spread_sportsbook",
        "closing_ou", "closing_ou_sportsbook",
        "closing_home_ml", "closing_home_ml_sportsbook",
        "closing_away_ml", "closing_away_ml_sportsbook",
        "closing_over_odds", "closing_over_odds_sportsbook",
        "closing_under_odds", "closing_under_odds_sportsbook",
        "closing_spread_home_odds", "closing_spread_home_odds_sportsbook",
        "closing_spread_away_odds", "closing_spread_away_odds_sportsbook",
        "closing_home_implied_probability", "closing_away_implied_probability",
    ]

    placeholders = ", ".join(["%s"] * len(columns))
    cols_str = ", ".join(columns)

    update_parts = [f"{c} = EXCLUDED.{c}" for c in columns if c != "game_id"]
    update_str = ", ".join(update_parts)

    insert_sql = f"""
        INSERT INTO mlb.betting_lines_consolidated ({cols_str})
        VALUES ({placeholders})
        ON CONFLICT (game_id) DO UPDATE SET {update_str}
    """

    tuples = [tuple(r[c] for c in columns) for r in rows]

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, insert_sql, tuples, page_size=200)
    conn.commit()

    logger.info(f"Upserted {len(rows)} rows into mlb.betting_lines_consolidated")
    return len(rows)


def run(dry_run=False, rebuild=False):
    """Main consolidation run."""
    conn = get_conn()
    try:
        if rebuild:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mlb.betting_lines_consolidated WHERE opening_spread_sportsbook = 'mlbverse'")
            conn.commit()
            logger.info("Cleared existing mlbverse entries from betting_lines_consolidated")

        old_data = fetch_old_data(conn)

        if not old_data:
            logger.info("No new rows to process from betting_lines_old")
            return 0

        game_ids = list(old_data.keys())
        game_info = fetch_game_info(conn, game_ids)

        rows = process_rows(old_data, game_info)

        if dry_run:
            logger.info(f"DRY RUN: Would upsert {len(rows)} rows")
            for r in rows[:5]:
                logger.info(
                    f"  DRY: game_id={r['game_id']}, spread={r['opening_spread']}, "
                    f"ou={r['opening_ou']}, home_ml={r['opening_home_ml']}, "
                    f"home_ip={r['opening_home_implied_probability']}"
                )
            return len(rows)

        count = upsert_rows(conn, rows)
        return count

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Consolidate mlbverse historical opening lines into betting_lines_consolidated"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done without writing"
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Remove existing mlbverse entries before re-inserting"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    count = run(dry_run=args.dry_run, rebuild=args.rebuild)

    if args.dry_run:
        logger.info(f"DRY RUN complete — would upsert {count} rows")
    else:
        logger.info(f"Done — upserted {count} rows")


if __name__ == "__main__":
    main()
