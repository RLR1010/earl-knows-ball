"""
Backfill MLB boxscores (batting + pitching) for games missing boxscore data
(2016-2019: 808 games with no batting OR pitching; 2024: 1 pitching-only gap).

Reuses the authoritative MLB StatsAPI fetch + validation from boxscore_ingest
(process_game for batting, process_pitchers for pitching), so boxscore summing
to the final score is enforced the same way as the live path.

Usage:
    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    ./venv/bin/python app/scripts/backfill_mlb_boxscores.py --limit 10        # test
    ./venv/bin/python app/scripts/backfill_mlb_boxscores.py                    # full
    ./venv/bin/python app/scripts/backfill_mlb_boxscores.py --batting-only     # skip pitching
"""
import asyncio
import sys
import os
import time
import logging
import argparse

# repo root on path: backend/  (parent of app/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import asyncpg

from app.ingestion.boxscore_ingest import (
    DB,
    fetch_boxscore,
    process_game,
    process_pitchers,
    create_table_if_not_exists,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("earl.boxscore_backfill")

SELECT_SQL = """
    SELECT g.id, g.mlb_game_id, g.date::date AS date,
           ht.abbreviation AS ha, at.abbreviation AS aa
    FROM mlb.games g
    JOIN mlb.teams ht ON ht.id = g.home_team_id
    JOIN mlb.teams at ON at.id = g.away_team_id
    WHERE g.status = 'FINAL'
      AND g.home_score IS NOT NULL
      AND g.mlb_game_id IS NOT NULL
      AND EXTRACT(YEAR FROM g.date)::int >= {min_year}
      AND (
        NOT EXISTS (SELECT 1 FROM mlb.batting_game_stats b WHERE b.game_id = g.id)
        OR NOT EXISTS (SELECT 1 FROM mlb.pitcher_game_stats p WHERE p.game_id = g.id)
      )
    ORDER BY g.date
"""


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-year", type=int, default=2016,
                        help="Minimum calendar year to backfill (default 2016, per scope)")
    parser.add_argument("--batting-only", action="store_true",
                        help="Backfill batting only (skip pitching)")
    args = parser.parse_args()

    conn = await asyncpg.connect(DB)
    try:
        await create_table_if_not_exists(conn)
        games = await conn.fetch(SELECT_SQL.format(min_year=args.min_year))
        games = [dict(g) for g in games]
        logger.info(f"Discovered {len(games)} games missing boxscore data")

        if args.limit > 0:
            games = games[: args.limit]

        total_bat = 0
        total_pit = 0
        errors = 0
        start = time.time()

        for i, game in enumerate(games):
            bat_rows = 0
            pit_rows = 0
            try:
                bat_rows = await process_game(conn, game)
                if not args.batting_only:
                    pit_rows = await process_pitchers(conn, game)
            except Exception as e:
                errors += 1
                logger.warning(f"  ERROR game {game['id']} ({game['date']} {game['ha']}@{game['aa']}): {e}")

            total_bat += bat_rows
            total_pit += pit_rows

            if (i + 1) % 10 == 0 or bat_rows == 0 or (not args.batting_only and pit_rows == 0):
                elapsed = time.time() - start
                rate = (i + 1) / elapsed if elapsed else 0
                logger.info(
                    f"  [{i+1}/{len(games)}] {game['date']} {game['ha']}@{game['aa']} "
                    f"bat={bat_rows} pitch={pit_rows} (total bat={total_bat} pitch={total_pit}, "
                    f"errors={errors}, {rate:.1f} games/s)"
                )

        elapsed = time.time() - start
        logger.info(
            f"\nDONE: {len(games)} games, batting rows={total_bat}, pitching rows={total_pit}, "
            f"errors={errors} in {elapsed:.0f}s ({len(games)/elapsed:.2f} games/s)"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
