#!/usr/bin/env python3
"""
nfl_betting_lines_consolidate.py

Consolidate nfl.betting_lines (opening + closing per-sportsbook rows) into
nfl.betting_lines_consolidated, mirroring the MLB pattern.

Tier 1 sportsbooks (>=99% game coverage, >=95% full market):
    fanduel, draftkings, betrivers, williamhill_us

Tier 2 sportsbooks (>=97% coverage, >=94% full market):
    betmgm, bovada, betonlineag, mybookieag

Tier 3 sportsbooks (>=88% coverage, >=87% full market):
    betus, lowvig

Priority fallback: Tier 1 → Tier 2 → Tier 3
"""

import argparse
import logging
from collections import defaultdict

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

TIER_1 = ["fanduel", "draftkings", "betrivers", "williamhill_us"]
TIER_2 = ["betmgm", "bovada", "betonlineag", "mybookieag"]
TIER_3 = ["betus", "lowvig"]

# Ordered priority list for fallback
PRIORITY = TIER_1 + TIER_2 + TIER_3


def get_conn():
    return psycopg2.connect(**DB_CONFIG)


def create_table_if_missing(conn):
    """Create nfl.betting_lines_consolidated if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nfl.betting_lines_consolidated (
                game_id INTEGER NOT NULL,
                game_time TIMESTAMPTZ,
                home_team TEXT,
                away_team TEXT,
                year INTEGER,
                home_score INTEGER,
                away_score INTEGER,
                venue TEXT,
                status TEXT,

                -- Closing columns
                closing_spread NUMERIC,
                closing_spread_sportsbook TEXT,
                closing_ou NUMERIC,
                closing_ou_sportsbook TEXT,
                closing_home_ml INTEGER,
                closing_home_ml_sportsbook TEXT,
                closing_away_ml INTEGER,
                closing_away_ml_sportsbook TEXT,

                -- Opening columns
                opening_ou NUMERIC,
                opening_ou_sportsbook TEXT,
                opening_spread NUMERIC,
                opening_spread_sportsbook TEXT,
                opening_home_ml INTEGER,
                opening_home_ml_sportsbook TEXT,
                opening_away_ml INTEGER,
                opening_away_ml_sportsbook TEXT,

                has_verified_ou BOOLEAN,

                -- Closing odds columns
                closing_over_odds INTEGER,
                closing_over_odds_sportsbook TEXT,
                closing_under_odds INTEGER,
                closing_under_odds_sportsbook TEXT,
                closing_spread_home_odds INTEGER,
                closing_spread_home_odds_sportsbook TEXT,
                closing_spread_away_odds INTEGER,
                closing_spread_away_odds_sportsbook TEXT,
                closing_home_implied_probability NUMERIC,
                closing_away_implied_probability NUMERIC,

                -- Opening odds columns
                opening_over_odds INTEGER,
                opening_over_odds_sportsbook TEXT,
                opening_under_odds INTEGER,
                opening_under_odds_sportsbook TEXT,
                opening_spread_home_odds INTEGER,
                opening_spread_home_odds_sportsbook TEXT,
                opening_spread_away_odds INTEGER,
                opening_spread_away_odds_sportsbook TEXT,
                opening_home_implied_probability NUMERIC,
                opening_away_implied_probability NUMERIC
            );
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS nfl_betting_lines_consolidated_pkey
            ON nfl.betting_lines_consolidated USING btree (game_id);
        """)
        conn.commit()
        logger.info("Table nfl.betting_lines_consolidated ready")


def fetch_closing_data(conn, game_ids=None):
    """Fetch per-sportsbook closing lines from nfl.betting_lines."""
    game_filter = ""
    params = []
    if game_ids:
        game_filter = "AND bl.game_id = ANY(%s)"
        params = [list(game_ids)]

    query = f"""
        SELECT
            LOWER(bl.sportsbook) AS sportsbook,
            bl.game_id,
            bl.spread       AS closing_spread,
            bl.over_under   AS closing_ou,
            bl.home_moneyline  AS closing_home_ml,
            bl.away_moneyline  AS closing_away_ml,
            bl.spread_home_odds  AS closing_spread_home_odds,
            bl.spread_away_odds  AS closing_spread_away_odds,
            bl.over_odds   AS closing_over_odds,
            bl.under_odds  AS closing_under_odds,
            bl.home_implied_probability AS closing_home_implied_prob,
            bl.away_implied_probability AS closing_away_implied_prob
        FROM nfl.betting_lines bl
        WHERE bl.source = 'the_odds_api_closing'
          AND bl.spread IS NOT NULL
          AND bl.over_under IS NOT NULL
          AND bl.home_moneyline IS NOT NULL
          AND bl.away_moneyline IS NOT NULL
          AND bl.spread_home_odds IS NOT NULL
          AND bl.over_odds IS NOT NULL
          {game_filter}
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(query, params)
    data = defaultdict(list)
    for row in cur.fetchall():
        data[row["game_id"]].append(dict(row))
    cur.close()
    return data


def fetch_opening_data(conn, game_ids=None):
    """Fetch per-sportsbook opening lines from nfl.betting_lines."""
    game_filter = ""
    params = []
    if game_ids:
        game_filter = "AND bl.game_id = ANY(%s)"
        params = [list(game_ids)]

    query = f"""
        SELECT
            LOWER(bl.sportsbook) AS sportsbook,
            bl.game_id,
            bl.spread       AS opening_spread,
            bl.over_under   AS opening_ou,
            bl.home_moneyline  AS opening_home_ml,
            bl.away_moneyline  AS opening_away_ml,
            bl.spread_home_odds  AS opening_spread_home_odds,
            bl.spread_away_odds  AS opening_spread_away_odds,
            bl.over_odds   AS opening_over_odds,
            bl.under_odds  AS opening_under_odds,
            bl.home_implied_probability AS opening_home_implied_prob,
            bl.away_implied_probability AS opening_away_implied_prob
        FROM nfl.betting_lines bl
        WHERE bl.source = 'the_odds_api_opening'
          AND bl.spread IS NOT NULL
          AND bl.over_under IS NOT NULL
          AND bl.home_moneyline IS NOT NULL
          AND bl.away_moneyline IS NOT NULL
          {game_filter}
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(query, params)
    data = defaultdict(list)
    for row in cur.fetchall():
        data[row["game_id"]].append(dict(row))
    cur.close()
    return data


def fetch_game_info(conn, game_ids):
    """Fetch game metadata for the given game_ids."""
    if not game_ids:
        return {}
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
        FROM nfl.games g
        JOIN nfl.teams home ON home.id = g.home_team_id
        JOIN nfl.teams away ON away.id = g.away_team_id
        JOIN nfl.seasons s ON s.id = g.season_id
        WHERE g.id = ANY(%s)
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(query, [list(game_ids)])
    result = {row["game_id"]: dict(row) for row in cur.fetchall()}
    cur.close()
    return result


def _pick_best(entries, preferred_books):
    """
    Given a list of sportsbook entries for one game/snapshot,
    find the best entry based on preferred_books priority.
    """
    if not entries:
        return None, None

    for book in preferred_books:
        for e in entries:
            if e["sportsbook"] == book:
                return e, book

    return entries[0], entries[0]["sportsbook"]


def process_game(game_id, closing_data, opening_data, game_info):
    """
    Consolidate one game into a dict ready for upsert.
    Returns None if no closing data is available.
    """
    closing_entries = closing_data.get(game_id, [])
    opening_entries = opening_data.get(game_id, [])

    if not closing_entries and not opening_entries:
        return None

    closing, closing_book = _pick_best(closing_entries, PRIORITY)
    opening, opening_book = _pick_best(opening_entries, PRIORITY)

    if closing is None:
        return None

    info = game_info.get(game_id, {})

    row = {
        "game_id": game_id,
        "game_time": info.get("game_time"),
        "home_team": info.get("home_team"),
        "away_team": info.get("away_team"),
        "year": info.get("year"),
        "home_score": info.get("home_score"),
        "away_score": info.get("away_score"),
        "venue": info.get("venue"),
        "status": info.get("status"),

        # ── Closing ──
        "closing_spread": closing["closing_spread"],
        "closing_spread_sportsbook": closing_book,
        "closing_ou": closing["closing_ou"],
        "closing_ou_sportsbook": closing_book,
        "closing_home_ml": closing["closing_home_ml"],
        "closing_home_ml_sportsbook": closing_book,
        "closing_away_ml": closing["closing_away_ml"],
        "closing_away_ml_sportsbook": closing_book,
        "closing_over_odds": closing["closing_over_odds"],
        "closing_over_odds_sportsbook": closing_book,
        "closing_under_odds": closing["closing_under_odds"],
        "closing_under_odds_sportsbook": closing_book,
        "closing_spread_home_odds": closing["closing_spread_home_odds"],
        "closing_spread_home_odds_sportsbook": closing_book,
        "closing_spread_away_odds": closing["closing_spread_away_odds"],
        "closing_spread_away_odds_sportsbook": closing_book,
        "closing_home_implied_probability": closing["closing_home_implied_prob"],
        "closing_away_implied_probability": closing["closing_away_implied_prob"],

        # ── Opening ──
        "opening_spread": opening["opening_spread"] if opening else None,
        "opening_spread_sportsbook": opening_book if opening else None,
        "opening_ou": opening["opening_ou"] if opening else None,
        "opening_ou_sportsbook": opening_book if opening else None,
        "opening_home_ml": opening["opening_home_ml"] if opening else None,
        "opening_home_ml_sportsbook": opening_book if opening else None,
        "opening_away_ml": opening["opening_away_ml"] if opening else None,
        "opening_away_ml_sportsbook": opening_book if opening else None,
        "opening_over_odds": opening["opening_over_odds"] if opening else None,
        "opening_over_odds_sportsbook": opening_book if opening else None,
        "opening_under_odds": opening["opening_under_odds"] if opening else None,
        "opening_under_odds_sportsbook": opening_book if opening else None,
        "opening_spread_home_odds": opening["opening_spread_home_odds"] if opening else None,
        "opening_spread_home_odds_sportsbook": opening_book if opening else None,
        "opening_spread_away_odds": opening["opening_spread_away_odds"] if opening else None,
        "opening_spread_away_odds_sportsbook": opening_book if opening else None,
        "opening_home_implied_probability": opening["opening_home_implied_prob"] if opening else None,
        "opening_away_implied_probability": opening["opening_away_implied_prob"] if opening else None,

        "has_verified_ou": None,
    }
    return row


def upsert_rows(conn, rows):
    """Upsert consolidated rows into nfl.betting_lines_consolidated."""
    if not rows:
        return 0

    cols = [
        "game_id", "game_time", "home_team", "away_team", "year",
        "home_score", "away_score", "venue", "status",
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

    placeholders = ", ".join(f"%({c})s" for c in cols)
    col_list = ", ".join(cols)

    upsert_sql = f"""
        INSERT INTO nfl.betting_lines_consolidated ({col_list})
        VALUES ({placeholders})
        ON CONFLICT (game_id) DO UPDATE SET
            closing_spread = EXCLUDED.closing_spread,
            closing_spread_sportsbook = EXCLUDED.closing_spread_sportsbook,
            closing_ou = EXCLUDED.closing_ou,
            closing_ou_sportsbook = EXCLUDED.closing_ou_sportsbook,
            closing_home_ml = EXCLUDED.closing_home_ml,
            closing_home_ml_sportsbook = EXCLUDED.closing_home_ml_sportsbook,
            closing_away_ml = EXCLUDED.closing_away_ml,
            closing_away_ml_sportsbook = EXCLUDED.closing_away_ml_sportsbook,
            closing_over_odds = EXCLUDED.closing_over_odds,
            closing_over_odds_sportsbook = EXCLUDED.closing_over_odds_sportsbook,
            closing_under_odds = EXCLUDED.closing_under_odds,
            closing_under_odds_sportsbook = EXCLUDED.closing_under_odds_sportsbook,
            closing_spread_home_odds = EXCLUDED.closing_spread_home_odds,
            closing_spread_home_odds_sportsbook = EXCLUDED.closing_spread_home_odds_sportsbook,
            closing_spread_away_odds = EXCLUDED.closing_spread_away_odds,
            closing_spread_away_odds_sportsbook = EXCLUDED.closing_spread_away_odds_sportsbook,
            closing_home_implied_probability = EXCLUDED.closing_home_implied_probability,
            closing_away_implied_probability = EXCLUDED.closing_away_implied_probability,
            -- Preserve opening (first seen)
            opening_spread = COALESCE(nfl.betting_lines_consolidated.opening_spread, EXCLUDED.opening_spread),
            opening_spread_sportsbook = COALESCE(nfl.betting_lines_consolidated.opening_spread_sportsbook, EXCLUDED.opening_spread_sportsbook),
            opening_ou = COALESCE(nfl.betting_lines_consolidated.opening_ou, EXCLUDED.opening_ou),
            opening_ou_sportsbook = COALESCE(nfl.betting_lines_consolidated.opening_ou_sportsbook, EXCLUDED.opening_ou_sportsbook),
            opening_home_ml = COALESCE(nfl.betting_lines_consolidated.opening_home_ml, EXCLUDED.opening_home_ml),
            opening_home_ml_sportsbook = COALESCE(nfl.betting_lines_consolidated.opening_home_ml_sportsbook, EXCLUDED.opening_home_ml_sportsbook),
            opening_away_ml = COALESCE(nfl.betting_lines_consolidated.opening_away_ml, EXCLUDED.opening_away_ml),
            opening_away_ml_sportsbook = COALESCE(nfl.betting_lines_consolidated.opening_away_ml_sportsbook, EXCLUDED.opening_away_ml_sportsbook),
            -- Score/status sync from games table
            home_score = EXCLUDED.home_score,
            away_score = EXCLUDED.away_score,
            status = EXCLUDED.status
    """

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, upsert_sql, rows, page_size=500)
    conn.commit()
    return len(rows)


def sync_scores_from_games(conn):
    """Sync game scores and status from nfl.games into consolidated."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE nfl.betting_lines_consolidated blc
            SET
                home_score = g.home_score,
                away_score = g.away_score,
                status = g.status::text
            FROM nfl.games g
            WHERE blc.game_id = g.id
              AND (
                  blc.home_score IS DISTINCT FROM g.home_score
                  OR blc.away_score IS DISTINCT FROM g.away_score
                  OR blc.status IS DISTINCT FROM g.status::text
              )
        """)
        synced = cur.rowcount
        conn.commit()
        logger.info(f"Synced scores/status for {synced} games from nfl.games")
        return synced


def run(rebuild_full=False, game_ids_filter=None):
    """Main consolidation run."""
    conn = get_conn()
    try:
        create_table_if_missing(conn)

        if rebuild_full:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE nfl.betting_lines_consolidated")
            conn.commit()
            logger.info("Truncated nfl.betting_lines_consolidated for full rebuild")
            game_ids_filter = None

        closing_data = fetch_closing_data(conn, game_ids_filter)
        opening_data = fetch_opening_data(conn, game_ids_filter)

        all_game_ids = set(closing_data.keys()) | set(opening_data.keys())
        if not all_game_ids:
            logger.info("No games to process")
            return

        game_info = fetch_game_info(conn, all_game_ids)

        rows = []
        for game_id in sorted(all_game_ids):
            row = process_game(game_id, closing_data, opening_data, game_info)
            if row:
                rows.append(row)

        inserted = upsert_rows(conn, rows)
        logger.info(f"Consolidated {inserted} rows into nfl.betting_lines_consolidated")

        sync_scores_from_games(conn)

    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Consolidate NFL betting lines")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Full rebuild (truncates and re-inserts all)",
    )
    parser.add_argument(
        "--games",
        nargs="*",
        type=int,
        help="Specific game_ids to consolidate (incremental). If omitted, processes all.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)

    run(rebuild_full=args.rebuild, game_ids_filter=set(args.games) if args.games else None)


if __name__ == "__main__":
    main()
