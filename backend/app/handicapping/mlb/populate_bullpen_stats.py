"""
Populate mlb.bullpen_game_stats from pitcher_game_stats.

Aggregates relief pitchers (is_starter=FALSE) per team per game,
storing bullpen ER, IP (in outs), and pitcher count.

Can be run stand-alone for backfill or incremental (recent games only).
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text

logger = logging.getLogger("earl.bullpen_stats")

CREATE_TABLE_SQL = text("""
CREATE TABLE IF NOT EXISTS mlb.bullpen_game_stats (
    id SERIAL PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES mlb.games(id) ON DELETE CASCADE,
    team_id INTEGER NOT NULL REFERENCES mlb.teams(id),
    bullpen_er NUMERIC DEFAULT 0,
    bullpen_ip_outs NUMERIC DEFAULT 0,
    num_pitchers INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(game_id, team_id)
);
""")
CREATE_UNIQUE_IDX = text("""
CREATE UNIQUE INDEX IF NOT EXISTS idx_bullpen_game_stats_game_team
ON mlb.bullpen_game_stats(game_id, team_id);
""")

BULLPEN_INSERT = text("""
INSERT INTO mlb.bullpen_game_stats (game_id, team_id, bullpen_er, bullpen_ip_outs, num_pitchers, updated_at)
WITH bullpen_totals AS (
    SELECT
        pgs.game_id,
        t.id AS team_id,
        COALESCE(SUM(pgs.er), 0) AS bullpen_er,
        COALESCE(SUM(
            -- Convert baseball IP (6.1 = 6 1/3) to outs
            FLOOR(pgs.ip) * 3 + ROUND((pgs.ip - FLOOR(pgs.ip)) * 10)
        ), 0) AS bullpen_ip_outs,
        COUNT(*) AS num_pitchers
    FROM mlb.pitcher_game_stats pgs
    JOIN mlb.teams t ON (
        CASE pgs.team_abbr
            WHEN 'ATH' THEN 'OAK'
            WHEN 'AZ' THEN 'ARI'
            ELSE pgs.team_abbr
        END = t.abbreviation
        AND t.id IN (
            SELECT g.home_team_id FROM mlb.games g WHERE g.id = pgs.game_id
            UNION
            SELECT g.away_team_id FROM mlb.games g WHERE g.id = pgs.game_id
        )
    )
    WHERE pgs.is_starter = FALSE
      AND pgs.ip IS NOT NULL
      AND pgs.er IS NOT NULL
      AND pgs.game_id IN :game_ids
    GROUP BY pgs.game_id, team_id
)
SELECT
    game_id, team_id, bullpen_er, bullpen_ip_outs, num_pitchers, NOW()
FROM bullpen_totals
ON CONFLICT (game_id, team_id)
DO UPDATE SET
    bullpen_er = EXCLUDED.bullpen_er,
    bullpen_ip_outs = EXCLUDED.bullpen_ip_outs,
    num_pitchers = EXCLUDED.num_pitchers,
    updated_at = NOW();
""")

# Also create a table for season-long bullpen stats (faster access)
BULLPEN_SEASONAL_SQL = text("""
INSERT INTO mlb.bullpen_game_stats (game_id, team_id, bullpen_er, bullpen_ip_outs, num_pitchers, updated_at)
-- Season-long: sum all bullpen stats for the team's season up to (not including) this game
WITH game_info AS (
    SELECT g.id AS game_id, g.season_id, g.date, g.home_team_id, g.away_team_id
    FROM mlb.games g
    WHERE g.id IN :game_ids
),
team_pairs AS (
    SELECT game_id, home_team_id AS team_id FROM game_info
    UNION
    SELECT game_id, away_team_id AS team_id FROM game_info
)
-- Actually, season-long bullpen stats are already in the per-game table
-- Just need to ensure per-game rows exist first
SELECT null LIMIT 0
""")


def populate_bullpen_stats(engine, game_ids=None, days_back=None, full_backfill=False):
    """Populate mlb.bullpen_game_stats."""
    with engine.begin() as conn:
        conn.execute(CREATE_TABLE_SQL)
        conn.execute(CREATE_UNIQUE_IDX)

        if full_backfill:
            # Get all game_ids that have pitcher_game_stats
            result = conn.execute(text("""
                SELECT DISTINCT pgs.game_id
                FROM mlb.pitcher_game_stats pgs
                WHERE pgs.is_starter = FALSE AND pgs.ip IS NOT NULL
                ORDER BY pgs.game_id
            """)).fetchall()
            ids = [r[0] for r in result]
            logger.info(f"Full backfill: {len(ids)} game_ids to process")
        elif days_back:
            cut = (datetime.now() - timedelta(days=days_back)).date()
            result = conn.execute(text("""
                SELECT DISTINCT pgs.game_id
                FROM mlb.pitcher_game_stats pgs
                JOIN mlb.games g ON g.id = pgs.game_id
                WHERE pgs.is_starter = FALSE AND pgs.ip IS NOT NULL
                  AND g.date >= :cut
                ORDER BY pgs.game_id
            """), {"cut": cut}).fetchall()
            ids = [r[0] for r in result]
            logger.info(f"Recent {days_back} days: {len(ids)} game_ids")
        elif game_ids:
            ids = game_ids
            logger.info(f"Specific game_ids: {len(ids)}")
        else:
            # Incremental: only games that have pitcher_game_stats but NOT bullpen_game_stats
            result = conn.execute(text("""
                SELECT DISTINCT pgs.game_id
                FROM mlb.pitcher_game_stats pgs
                LEFT JOIN mlb.bullpen_game_stats bgs ON bgs.game_id = pgs.game_id
                WHERE pgs.is_starter = FALSE AND pgs.ip IS NOT NULL
                  AND bgs.game_id IS NULL
                ORDER BY pgs.game_id
            """)).fetchall()
            ids = [r[0] for r in result]
            logger.info(f"Incremental: {len(ids)} game_ids missing bullpen_stats")

        if not ids:
            logger.info("No game_ids to process")
            return 0

        # Process in batches
        batch_size = 500
        total_upserted = 0
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            result = conn.execute(BULLPEN_INSERT, {"game_ids": tuple(batch)})
            total_upserted += result.rowcount
            if (i + batch_size) % 5000 == 0:
                logger.info(f"  Processed {i + batch_size}/{len(ids)} game_ids...")

        logger.info(f"Total upserted: {total_upserted}")
        return total_upserted


def main():
    parser = argparse.ArgumentParser(description="Populate mlb.bullpen_game_stats")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--backfill", action="store_true", help="Full backfill all seasons")
    group.add_argument("--days", type=int, default=None, help="Process recent N days")
    group.add_argument("--incremental", action="store_true", help="Only missing games (default)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # PSYCOPG2_DATABASE_URL already reflects .env DATABASE_URL (asyncpg suffix stripped)
    db_url = PSYCOPG2_DATABASE_URL
    engine = create_engine(db_url)

    if args.backfill:
        populate_bullpen_stats(engine, full_backfill=True)
    elif args.days:
        populate_bullpen_stats(engine, days_back=args.days)
    else:
        populate_bullpen_stats(engine)


if __name__ == "__main__":
    main()
