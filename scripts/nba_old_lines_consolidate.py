#!/usr/bin/env python3
"""
nba_old_lines_consolidate.py

Consolidate historical lines from nba.betting_lines_old into
nba.betting_lines_consolidated.

betting_lines_old contains pre-2021 lines (2007-2020) from a single aggregated
source. Unlike NFL/MLB, NBA has BOTH opening and closing lines per game
(3,295 games × 2 rows), with the moneyline values often identical but
spreads differing ~82% of the time.

No spread odds or over/under odds are available in this dataset — those
columns will be NULL in the consolidated table.

This script:
  1. Joins opening and closing rows per game_id
  2. Corrects implied probabilities (data has ×100 scaling for positive odds)
  3. Maps opening data → opening_* columns, closing → closing_* columns
  4. Upserts into betting_lines_consolidated with source = 'nba_old'

Usage:
    python scripts/nba_old_lines_consolidate.py [--dry-run]
    python scripts/nba_old_lines_consolidate.py [--rebuild]
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

SOURCE = "nba_old"


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
    Fetch opening + closing rows from nba.betting_lines_old, joined per game_id.
    Only returns games not yet in betting_lines_consolidated.
    Returns a dict: {game_id: {"opening": row, "closing": row}}
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get opening rows
    open_query = """
        SELECT blo.*
        FROM nba.betting_lines_old blo
        LEFT JOIN nba.betting_lines_consolidated blc
            ON blc.game_id = blo.game_id
        WHERE blo.is_opening = 't' AND blc.game_id IS NULL
        ORDER BY blo.game_id
    """
    cur.execute(open_query)
    opening_rows = {row["game_id"]: dict(row) for row in cur.fetchall()}

    # Get closing rows
    close_query = """
        SELECT blo.*
        FROM nba.betting_lines_old blo
        LEFT JOIN nba.betting_lines_consolidated blc
            ON blc.game_id = blo.game_id
        WHERE blo.is_opening = 'f' AND blc.game_id IS NULL
        ORDER BY blo.game_id
    """
    cur.execute(close_query)
    closing_rows = {row["game_id"]: dict(row) for row in cur.fetchall()}

    cur.close()

    # Merge into per-game dicts
    game_ids = set(opening_rows.keys()) | set(closing_rows.keys())
    result = {}
    for gid in sorted(game_ids):
        result[gid] = {
            "opening": opening_rows.get(gid),
            "closing": closing_rows.get(gid),
        }

    logger.info(
        f"Fetched {len(result)} games from nba.betting_lines_old "
        f"({len(opening_rows)} opening, {len(closing_rows)} closing, "
        f"{len(game_ids)} unique games not yet in consolidated)"
    )
    return result


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
        FROM nba.games g
        JOIN nba.teams home ON home.id = g.home_team_id
        JOIN nba.teams away ON away.id = g.away_team_id
        JOIN nba.seasons s ON s.id = g.season_id
        WHERE g.id = ANY(%s)
    """
    cur.execute(query, [list(game_ids)])
    result = {row["game_id"]: dict(row) for row in cur.fetchall()}
    cur.close()
    return result


def process_rows(games, game_info):
    """
    Convert old_data into upsert-ready dicts for betting_lines_consolidated.

    NBA spread convention matches ours (negative = home favored), no negation needed.
    No spread odds or OU odds available — those will be NULL.
    """
    result = []
    for game_id in sorted(games.keys()):
        info = game_info.get(game_id, {})
        opening = games[game_id].get("opening", {}) or {}
        closing = games[game_id].get("closing", {}) or {}

        # Correct opening implied probabilities
        open_home_ip = american_to_implied_probability(opening.get("home_moneyline"))
        open_away_ip = american_to_implied_probability(opening.get("away_moneyline"))

        # Correct closing implied probabilities
        close_home_ip = american_to_implied_probability(closing.get("home_moneyline"))
        close_away_ip = american_to_implied_probability(closing.get("away_moneyline"))

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

            # ── Opening ──
            "opening_spread": opening.get("spread"),
            "opening_spread_sportsbook": SOURCE,
            "opening_ou": opening.get("over_under"),
            "opening_ou_sportsbook": SOURCE,
            "opening_home_ml": opening.get("home_moneyline"),
            "opening_home_ml_sportsbook": SOURCE,
            "opening_away_ml": opening.get("away_moneyline"),
            "opening_away_ml_sportsbook": SOURCE,

            # Spread/OU odds not in source — use standard -110 juice
            "opening_over_odds": -110,
            "opening_over_odds_sportsbook": SOURCE,
            "opening_under_odds": -110,
            "opening_under_odds_sportsbook": SOURCE,
            "opening_spread_home_odds": -110,
            "opening_spread_home_odds_sportsbook": SOURCE,
            "opening_spread_away_odds": -110,
            "opening_spread_away_odds_sportsbook": SOURCE,

            "opening_home_implied_probability": open_home_ip,
            "opening_away_implied_probability": open_away_ip,

            # ── Closing ──
            "closing_spread": closing.get("spread"),
            "closing_spread_sportsbook": SOURCE,
            "closing_ou": closing.get("over_under"),
            "closing_ou_sportsbook": SOURCE,
            "closing_home_ml": closing.get("home_moneyline"),
            "closing_home_ml_sportsbook": SOURCE,
            "closing_away_ml": closing.get("away_moneyline"),
            "closing_away_ml_sportsbook": SOURCE,

            # Spread/OU odds not in source — use standard -110 juice
            "closing_over_odds": -110,
            "closing_over_odds_sportsbook": SOURCE,
            "closing_under_odds": -110,
            "closing_under_odds_sportsbook": SOURCE,
            "closing_spread_home_odds": -110,
            "closing_spread_home_odds_sportsbook": SOURCE,
            "closing_spread_away_odds": -110,
            "closing_spread_away_odds_sportsbook": SOURCE,

            "closing_home_implied_probability": close_home_ip,
            "closing_away_implied_probability": close_away_ip,
        }
        result.append(entry)

    return result


def upsert_rows(conn, rows):
    """Bulk upsert rows into nba.betting_lines_consolidated."""
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
        INSERT INTO nba.betting_lines_consolidated ({cols_str})
        VALUES ({placeholders})
        ON CONFLICT (game_id) DO UPDATE SET {update_str}
    """

    tuples = [tuple(r[c] for c in columns) for r in rows]

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, insert_sql, tuples, page_size=200)
    conn.commit()

    logger.info(f"Upserted {len(rows)} rows into nba.betting_lines_consolidated")
    return len(rows)


def run(dry_run=False, rebuild=False):
    """Main consolidation run."""
    conn = get_conn()
    try:
        if rebuild:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM nba.betting_lines_consolidated WHERE opening_spread_sportsbook = 'nba_old'")
            conn.commit()
            logger.info("Cleared existing nba_old entries from betting_lines_consolidated")

        games = fetch_old_data(conn)

        if not games:
            logger.info("No new games to process from betting_lines_old")
            return 0

        game_ids = list(games.keys())
        game_info = fetch_game_info(conn, game_ids)

        rows = process_rows(games, game_info)

        if dry_run:
            logger.info(f"DRY RUN: Would upsert {len(rows)} rows")
            for r in rows[:5]:
                logger.info(
                    f"  DRY: game_id={r['game_id']}, "
                    f"open_spread={r['opening_spread']}, close_spread={r['closing_spread']}, "
                    f"ou={r['opening_ou']}, home_ml={r['opening_home_ml']}, "
                    f"home_ip={r['opening_home_implied_probability']:.2f}"
                )
            return len(rows)

        count = upsert_rows(conn, rows)
        return count

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Consolidate nba_old historical lines into betting_lines_consolidated"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done without writing"
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="Remove existing nba_old entries before re-inserting"
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
