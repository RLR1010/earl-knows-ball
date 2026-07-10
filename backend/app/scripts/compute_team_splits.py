"""
Compute missing rate stats (AVG, OBP, SLG, OPS, ERA, WHIP) in mlb.team_splits
from game-level batting/pitching data.

Usage:
  python -m backend.app.scripts.compute_team_splits [--year 2026] [--all]
"""

import argparse
import asyncio
import logging

import asyncpg

logger = logging.getLogger(__name__)

DB_DSN = "postgresql://earl:earl2025@localhost:5432/earl_knows_football"

SPLITS = ["home", "away", "day", "night", "grass", "turf"]


async def ensure_split_rows(pool, season_id: int):
    """Create skeleton team_splits rows for teams that have game stats but no split rows yet."""
    async with pool.acquire() as conn:
        for split in SPLITS:
            extra = ""
            if split == "home":
                extra = "AND bgs.team_side = 'home'"
            elif split == "away":
                extra = "AND bgs.team_side = 'away'"
            elif split == "day":
                extra = "AND (g.day_night IS NULL OR g.day_night ILIKE 'day')"
            elif split == "night":
                extra = "AND g.day_night ILIKE 'night'"
            elif split == "grass":
                extra = "AND (g.surface IS NULL OR g.surface ILIKE 'grass')"
            elif split == "turf":
                extra = "AND g.surface ILIKE 'turf'"

            sql = f"""
INSERT INTO mlb.team_splits (team_id, season_id, split_type, games)
SELECT
    CASE WHEN bgs.team_side = 'home' THEN g.home_team_id ELSE g.away_team_id END,
    g.season_id,
    '{split}',
    COUNT(DISTINCT g.id)
FROM mlb.batting_game_stats bgs
JOIN mlb.games g ON g.id = bgs.game_id
WHERE g.season_id = $1 {extra}
GROUP BY CASE WHEN bgs.team_side = 'home' THEN g.home_team_id ELSE g.away_team_id END, g.season_id
ON CONFLICT (team_id, season_id, split_type) DO NOTHING;
"""
            await conn.execute(sql, season_id)

        logger.info("Skeleton rows ensured for season %s", season_id)


async def compute_batting(pool, season_id: int):
    """Compute AVG, OBP, SLG, OPS from batting_game_stats and update team_splits."""
    async with pool.acquire() as conn:
        for split in SPLITS:
            extra = ""
            if split == "home":
                extra = "AND bgs.team_side = 'home'"
            elif split == "away":
                extra = "AND bgs.team_side = 'away'"
            elif split == "day":
                extra = "AND (g.day_night IS NULL OR g.day_night ILIKE 'day')"
            elif split == "night":
                extra = "AND g.day_night ILIKE 'night'"
            elif split == "grass":
                extra = "AND (g.surface IS NULL OR g.surface ILIKE 'grass')"
            elif split == "turf":
                extra = "AND g.surface ILIKE 'turf'"

            sql = f"""
WITH team_game_batting AS (
    SELECT
        g.id AS game_id,
        CASE WHEN bgs.team_side = 'home' THEN g.home_team_id ELSE g.away_team_id END AS team_id,
        GREATEST(SUM(bgs.at_bats), 0)       AS ab,
        GREATEST(SUM(bgs.hits), 0)          AS hits,
        GREATEST(SUM(bgs.base_on_balls), 0) AS bb,
        GREATEST(SUM(bgs.hit_by_pitch), 0)  AS hbp,
        GREATEST(SUM(bgs.sacrifice_flies), 0) AS sf,
        GREATEST(
            SUM(bgs.hits + 2 * bgs.doubles + 3 * bgs.triples + 4 * bgs.home_runs), 0
        ) AS tb,
        GREATEST(SUM(bgs.home_runs), 0)     AS hr
    FROM mlb.batting_game_stats bgs
    JOIN mlb.games g ON g.id = bgs.game_id
    WHERE g.season_id = $1
    {extra}
    GROUP BY g.id, bgs.team_side, g.home_team_id, g.away_team_id
)
, team_totals AS (
    SELECT
        team_id,
        SUM(ab)  AS ab,
        SUM(hits) AS hits,
        SUM(bb)  AS bb,
        SUM(hbp) AS hbp,
        SUM(sf)  AS sf,
        SUM(tb)  AS tb,
        SUM(hr)  AS hr
    FROM team_game_batting
    GROUP BY team_id
)
UPDATE mlb.team_splits ts
SET
    avg       = ROUND(tt.hits::numeric / NULLIF(tt.ab, 0), 3),
    obp       = ROUND((tt.hits + tt.bb + tt.hbp)::numeric / NULLIF(tt.ab + tt.bb + tt.hbp + tt.sf, 0), 3),
    slg       = ROUND(tt.tb::numeric / NULLIF(tt.ab, 0), 3),
    ops       = ROUND(
        (tt.hits + tt.bb + tt.hbp)::numeric / NULLIF(tt.ab + tt.bb + tt.hbp + tt.sf, 0)
        + tt.tb::numeric / NULLIF(tt.ab, 0), 3
    ),
    home_runs = COALESCE(NULLIF(ts.home_runs, 0), tt.hr),
    games     = t.games
FROM team_totals tt
JOIN (
    SELECT
        CASE WHEN bgs.team_side = 'home' THEN g.home_team_id ELSE g.away_team_id END AS team_id,
        COUNT(DISTINCT g.id) AS games
    FROM mlb.batting_game_stats bgs
    JOIN mlb.games g ON g.id = bgs.game_id
    WHERE g.season_id = $1 {extra}
    GROUP BY CASE WHEN bgs.team_side = 'home' THEN g.home_team_id ELSE g.away_team_id END
) t ON t.team_id = tt.team_id
WHERE ts.team_id = tt.team_id
  AND ts.season_id = $2
  AND ts.split_type = $3
  AND (tt.ab > 0);
"""
            result = await conn.execute(sql, season_id, season_id, split)
            affected = result.split()[-1] if result else "0"
            logger.info("Batting %s: %s rows updated", split, affected)


async def compute_pitching(pool, season_id: int):
    """Compute ERA, WHIP from pitcher_game_stats and update team_splits."""
    async with pool.acquire() as conn:
        for split in SPLITS:
            extra = ""
            if split == "home":
                extra = "AND CASE WHEN pgs.team_abbr = ht.abbreviation THEN 'home' ELSE 'away' END = 'home'"
            elif split == "away":
                extra = "AND CASE WHEN pgs.team_abbr = ht.abbreviation THEN 'home' ELSE 'away' END = 'away'"
            elif split == "day":
                extra = "AND (g.day_night IS NULL OR g.day_night ILIKE 'day')"
            elif split == "night":
                extra = "AND g.day_night ILIKE 'night'"
            elif split == "grass":
                extra = "AND (g.surface IS NULL OR g.surface ILIKE 'grass')"
            elif split == "turf":
                extra = "AND g.surface ILIKE 'turf'"

            sql = f"""
WITH pitcher_games AS (
    SELECT
        g.id AS game_id,
        CASE WHEN pgs.team_abbr = ht.abbreviation THEN g.home_team_id ELSE g.away_team_id END AS team_id,
        pgs.ip,
        pgs.er,
        pgs.h,
        pgs.bb,
        pgs.hr
    FROM mlb.pitcher_game_stats pgs
    JOIN mlb.games g ON g.id = pgs.game_id
    JOIN mlb.teams ht ON ht.id = g.home_team_id
    JOIN mlb.teams at ON at.id = g.away_team_id
    WHERE g.season_id = $1
      AND pgs.ip IS NOT NULL AND pgs.ip > 0
    {extra}
)
, team_totals AS (
    SELECT
        team_id,
        GREATEST(SUM(pg.er), 0)   AS er,
        SUM(pg.ip)                AS ip,
        GREATEST(SUM(pg.bb), 0)   AS walks,
        GREATEST(SUM(pg.h), 0)    AS hits,
        GREATEST(SUM(pg.hr), 0)   AS hr
    FROM pitcher_games pg
    GROUP BY team_id
)
UPDATE mlb.team_splits ts
SET
    era  = ROUND(tt.er::numeric * 9.0 / NULLIF(tt.ip, 0), 2),
    whip = ROUND((tt.walks + tt.hits)::numeric / NULLIF(tt.ip, 0), 2),
    home_runs = COALESCE(NULLIF(ts.home_runs, 0), tt.hr)
FROM team_totals tt
WHERE ts.team_id = tt.team_id
  AND ts.season_id = $2
  AND ts.split_type = $3;
"""
            result = await conn.execute(sql, season_id, season_id, split)
            affected = result.split()[-1] if result else "0"
            logger.info("Pitching %s: %s rows updated", split, affected)


async def main(year: int | None = None):
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    try:
        async with pool.acquire() as conn:
            if year:
                rows = await conn.fetch(
                    "SELECT id, year FROM mlb.seasons WHERE year = $1", year
                )
            else:
                rows = await conn.fetch(
                    "SELECT id, year FROM mlb.seasons WHERE id >= 15 ORDER BY id"
                )

        for row in rows:
            sid, yr = row["id"], row["year"]
            logger.info("=== Season %s (id=%s) ===", yr, sid)
            await ensure_split_rows(pool, sid)
            await compute_batting(pool, sid)
            await compute_pitching(pool, sid)
            logger.info("  done")

        # Verify
        async with pool.acquire() as conn:
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM mlb.team_splits WHERE avg IS NOT NULL"
            )
            cnt2 = await conn.fetchval(
                "SELECT COUNT(*) FROM mlb.team_splits WHERE era IS NOT NULL"
            )
        logger.info("== Verified: %s rows with AVG, %s rows with ERA ==", cnt, cnt2)

    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=None, help="Target year")
    parser.add_argument("--all", action="store_true", help="All years with splits data")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.all:
        asyncio.run(main())
    elif args.year:
        asyncio.run(main(year=args.year))
    else:
        parser.print_help()
