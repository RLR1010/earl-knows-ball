#!/usr/bin/env python3
"""
Fix corrupted cumulative_game_stats rows.

Issue: certain games have pitch_* cumulative stats = 0 even though the
preceding game's cumulative stats were non-zero. This happens when a
boxscore was ingested for the batting side but not the pitching side.

Fix: for any row where pitch_* cumulative = 0 but the previous game had
> 0, copy the previous game's cumulative values forward.

This ensures LAG-based per-game deltas produce correct results.
"""

import logging
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import execute_values

from app.core.config import settings

logger = logging.getLogger(__name__)

# Columns to fix (pitch-side cumulative stats that can be zeroed out)
PITCH_COLS = [
    "pitch_ip",
    "pitch_er",
    "pitch_hits_allowed",
    "pitch_walks_allowed",
    "pitch_strikeouts",
    "pitch_home_runs_allowed",
    "pitch_hit_by_pitch",
    "pitch_batters_faced",
]


def get_conn():
    url = urlparse(settings.database_url_sync)
    return psycopg2.connect(
        host=url.hostname,
        port=url.port or 5432,
        dbname=url.path.lstrip("/"),
        user=url.username,
        password=***,
    )


def find_corrupted(conn) -> list:
    """Find rows where pitch_* = 0 after valid previous data."""
    cur = conn.cursor()

    conditions = " OR ".join(
        f"(cgs.{col} = 0 AND LAG(cgs.{col}) OVER w > 0)"
        for col in PITCH_COLS
    )

    cur.execute(f"""
        SELECT cgs.game_id, cgs.team_id, cgs.team_side, cgs.game_date,
               {', '.join(f'cgs.{col}' for col in PITCH_COLS)},
               {', '.join(f'LAG(cgs.{col}) OVER w AS prev_{col}' for col in PITCH_COLS)}
        FROM mlb.cumulative_game_stats cgs
        WINDOW w AS (PARTITION BY cgs.team_id, cgs.season_id
                     ORDER BY cgs.game_date, cgs.game_id)
        HAVING {conditions}
    """)

    rows = cur.fetchall()
    cur.close()
    return rows


def fix_corrupted(conn, dry_run: bool = True) -> int:
    """Update corrupted rows with previous game's cumulative values."""
    cur = conn.cursor()

    # First, get all rows with their previous values
    cur.execute(f"""
        SELECT cgs.game_id, cgs.team_id, cgs.team_side, cgs.game_date,
               {', '.join(f'cgs.{col}' for col in PITCH_COLS)},
               {', '.join(f'LAG(cgs.{col}) OVER w AS prev_{col}' for col in PITCH_COLS)},
               ROW_NUMBER() OVER w AS rn
        FROM mlb.cumulative_game_stats cgs
        WINDOW w AS (PARTITION BY cgs.team_id, cgs.season_id
                     ORDER BY cgs.game_date, cgs.game_id)
    """)

    rows = cur.fetchall()
    col_names = [desc[0] for desc in cur.description]
    logger.info("Examining %d cumulative stats rows", len(rows))

    # Find corrupted rows and build fix values
    fixes = []
    for row in rows:
        row_dict = dict(zip(col_names, row))
        needs_fix = False
        update_set = {}

        for col in PITCH_COLS:
            val = row_dict[col]
            prev_val = row_dict.get(f"prev_{col}")
            if val == 0 and prev_val is not None and prev_val > 0:
                # The cumulative value is 0 but the previous game had valid data
                # This means the boxscore wasn't ingested for this game
                # Fix: copy the previous cumulative value forward
                update_set[col] = prev_val
                needs_fix = True

        if needs_fix:
            fixes.append((row_dict["game_id"], row_dict["team_id"], row_dict["team_side"], update_set))

    logger.info("Found %d corrupted rows", len(fixes))

    if dry_run:
        logger.info("DRY RUN — no changes made")
        # Show sample
        for game_id, team_id, team_side, updates in fixes[:5]:
            logger.info(
                "  Game %d team=%s side=%s: %s",
                game_id, team_id, team_side,
                {k: f"0 -> {v}" for k, v in updates.items()},
            )
        return len(fixes)

    # Apply fixes
    fixed_count = 0
    for game_id, team_id, team_side, updates in fixes:
        set_clause = ", ".join(f"{col} = {v}" for col, v in updates.items())
        cur.execute(f"""
            UPDATE mlb.cumulative_game_stats
            SET {set_clause}
            WHERE game_id = {game_id} AND team_id = {team_id} AND team_side = '{team_side}'
        """)
        fixed_count += 1

        if fixed_count % 500 == 0:
            conn.commit()
            logger.info("  Fixed %d rows...", fixed_count)

    conn.commit()
    logger.info("Fixed %d rows total", fixed_count)
    cur.close()
    return fixed_count


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply fixes (default: dry run)")
    args = parser.parse_args()

    conn = get_conn()
    try:
        n = fix_corrupted(conn, dry_run=not args.apply)
        if n > 0 and not args.apply:
            logger.info("Run with --apply to apply fixes")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
