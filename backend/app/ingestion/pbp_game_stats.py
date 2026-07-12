"""
Aggregate NFL play-by-play data into nfl.game_stats columns.

Computes first downs, 3rd/4th down conversions, red zone stats,
explosive plays, three-and-outs, and interceptions from PBP data.

Can be called via: POST /ingest/nfl/pbp-game-stats?seasons=2025&seasons=2024
"""

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("earl.pbp_game_stats")

# Single UPDATE query per season — aggregates PBP and joins to game_stats
UPDATE_SQL = """
UPDATE nfl.game_stats gs
SET
    first_downs = agg.first_downs,
    third_down_attempts = agg.third_down_attempts,
    third_down_conversions = agg.third_down_conversions,
    fourth_down_attempts = agg.fourth_down_attempts,
    fourth_down_conversions = agg.fourth_down_conversions,
    interceptions_thrown = agg.interceptions_thrown,
    explosive_plays = agg.explosive_plays,
    red_zone_trips = agg.red_zone_trips,
    red_zone_tds = agg.red_zone_tds,
    three_and_outs = agg.three_and_outs
FROM (
    SELECT
        SPLIT_PART(pbp.game_id, '_', 3) AS home_team,
        SPLIT_PART(pbp.game_id, '_', 4) AS away_team,
        pbp.season,
        pbp.week,
        pbp.posteam AS team_abbr,

        SUM(pbp.first_down) AS first_downs,
        SUM(pbp.third_down_attempted) AS third_down_attempts,
        SUM(pbp.third_down_converted) AS third_down_conversions,
        SUM(pbp.fourth_down_attempted) AS fourth_down_attempts,
        SUM(pbp.fourth_down_converted) AS fourth_down_conversions,
        SUM(pbp.interception) AS interceptions_thrown,

        -- 20+ yard plays
        COUNT(*) FILTER (WHERE pbp.yards_gained >= 20 AND pbp.play_type NOT IN ('timeout', '')) AS explosive_plays,

        -- Red zone: drives with yardline_100 <= 20
        COUNT(DISTINCT pbp.drive) FILTER (
            WHERE pbp.yardline_100 <= 20 AND pbp.yardline_100 > 0
        ) AS red_zone_trips,

        -- Red zone TDs
        SUM(pbp.touchdown) FILTER (
            WHERE pbp.yardline_100 <= 20 AND pbp.yardline_100 > 0
        ) AS red_zone_tds,

        -- Three-and-outs: drives with 3 or fewer offensive plays (not scoring)
        (
            SELECT COUNT(*)
            FROM (
                SELECT
                    d.drive,
                    COUNT(*) AS plays_in_drive,
                    MAX(d.scoring_play) AS drive_scored
                FROM nfl.play_by_play d
                WHERE d.game_id = pbp.game_id
                  AND d.posteam = pbp.posteam
                  AND d.down IS NOT NULL
                  AND d.play_type NOT IN ('timeout', '', 'no_play', 'qb_kneel', 'qb_spike')
                GROUP BY d.drive
                HAVING COUNT(*) <= 3
                   AND (MAX(d.scoring_play) IS NULL OR MAX(d.scoring_play) = 0)
            ) t
        ) AS three_and_outs

    FROM nfl.play_by_play pbp
    WHERE pbp.season = :season
      AND pbp.play_type NOT IN ('timeout', '', 'no_play', 'qb_kneel', 'qb_spike')
      AND pbp.down IS NOT NULL
      AND pbp.posteam != ''
    GROUP BY pbp.season, pbp.week, pbp.game_id, pbp.posteam
) agg
WHERE gs.season = agg.season
  AND gs.week = agg.week
  AND gs.team_abbr = agg.team_abbr;
"""


async def aggregate_pbp_to_game_stats(
    db: AsyncSession,
    seasons: Optional[list[int]] = None,
) -> dict:
    """Aggregate PBP data into nfl.game_stats for the given seasons.

    Args:
        db: Async SQLAlchemy session.
        seasons: List of seasons to process. If None, processes all available.

    Returns:
        dict mapping season string to number of rows updated.
    """
    if seasons is None:
        result = await db.execute(
            text("SELECT DISTINCT season FROM nfl.play_by_play ORDER BY season")
        )
        seasons = [r[0] for r in result.fetchall()]

    results = {}
    for season in seasons:
        logger.info(f"Aggregating PBP stats into game_stats for {season}...")
        result = await db.execute(text(UPDATE_SQL), {"season": season})
        updated = result.rowcount
        await db.commit()
        results[str(season)] = updated
        logger.info(f"Updated {updated} game_stats rows for {season}")

    return results
