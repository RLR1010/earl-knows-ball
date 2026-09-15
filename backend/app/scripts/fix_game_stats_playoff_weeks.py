"""One-time (idempotent) cleanup of nfl.game_stats playoff rows.

Problem
-------
`nfl.game_stats` ended up with TWO copies of every playoff game:

  * generation A: weeks 19..22 (Wild Card = 19 ... Super Bowl = 22) -- matches
    `nfl.games`, which is our canonical convention; and
  * generation B: weeks 18..21 (nflverse's native numbering for pre-2021 seasons,
    where the regular season was 17 weeks).

The rows were written under both conventions (the upsert key is
`(season, week, team_abbr, opponent_abbr)`, so a changed week inserted a parallel
row instead of updating the existing one). Result: 102 redundant rows across
2016-2020, and any join keyed on `(season, week, team_abbr)` fanned out during the
playoffs (this is what produced the `qb_rolling_stats` CardinalityViolation and
also silently corrupted playoff cumulative joins).

What this does
--------------
Keeps exactly one row per real game (per team), at the canonical `nfl.games` week.
It only removes *redundant duplicate copies* -- every real game is preserved
(verified: each POST game still has exactly its two team rows afterwards).

Safe to run multiple times (idempotent). Prints a before/after summary.

Usage (on a box that can reach the DB):
    cd backend && PYTHONPATH=. ../venv/bin/python app/scripts/fix_game_stats_playoff_weeks.py
"""
from __future__ import annotations

import asyncio
import collections
import logging

import asyncpg

from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("fix_game_stats_playoff_weeks")


# Returns every POST game_stats row together with the canonical week of its game.
CANON_SQL = """
SELECT gs.id, gs.season, gs.week, gs.team_abbr, gs.opponent_abbr, gs.loaded_at,
       g.week AS canon_week
FROM nfl.game_stats gs
JOIN nfl.seasons s ON s.year = gs.season
JOIN nfl.games g ON g.season_id = s.id AND g.game_type = 'POST'
JOIN nfl.teams ht ON ht.id = g.home_team_id
JOIN nfl.teams at ON at.id = g.away_team_id
WHERE gs.season_type = 'POST'
  AND ((ht.abbreviation = gs.team_abbr AND at.abbreviation = gs.opponent_abbr)
    OR (at.abbreviation = gs.team_abbr AND ht.abbreviation = gs.opponent_abbr))
"""


async def _summary(conn: asyncpg.Connection) -> tuple[int, int, int]:
    total = await conn.fetchval("SELECT count(*) FROM nfl.game_stats")
    post = await conn.fetchval(
        "SELECT count(*) FROM nfl.game_stats WHERE season_type = 'POST'"
    )
    dups = await conn.fetchval(
        "SELECT count(*) FROM (SELECT 1 FROM nfl.game_stats "
        "GROUP BY season, week, team_abbr HAVING count(*) > 1) x"
    )
    return total, post, dups


async def main() -> None:
    conn = await asyncpg.connect(
        settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    try:
        before = await _summary(conn)
        logger.info("BEFORE total=%s post=%s dup_groups=%s", *before)

        rows = await conn.fetch(CANON_SQL)
        by_key: dict[tuple, list] = collections.defaultdict(list)
        for r in rows:
            by_key[(r["season"], r["team_abbr"], r["opponent_abbr"])].append(r)

        to_delete: list[int] = []
        to_update: list[tuple[int, int]] = []
        for group in by_key.values():
            canon = group[0]["canon_week"]
            at_canon = [x for x in group if x["week"] == canon]
            pool = at_canon if at_canon else group
            # Prefer the freshest copy; tie-break on id.
            keep = sorted(pool, key=lambda x: (x["loaded_at"], x["id"]), reverse=True)[0]
            if keep["week"] != canon:
                to_update.append((keep["id"], canon))
            for x in group:
                if x["id"] != keep["id"]:
                    to_delete.append(x["id"])

        logger.info(
            "plan: real_games=%s delete_duplicates=%s reweek=%s",
            len(by_key), len(to_delete), len(to_update),
        )

        if to_delete:
            res = await conn.execute(
                "DELETE FROM nfl.game_stats WHERE id = ANY($1::int[])", to_delete
            )
            logger.info("DELETE -> %s", res)
        for rid, wk in to_update:
            await conn.execute(
                "UPDATE nfl.game_stats SET week = $2 WHERE id = $1", rid, wk
            )
        if to_update:
            logger.info("re-weeked %s rows", len(to_update))

        after = await _summary(conn)
        logger.info("AFTER total=%s post=%s dup_groups=%s", *after)
        per_season = await conn.fetch(
            "SELECT season, count(*) n FROM nfl.game_stats "
            "WHERE season_type = 'POST' GROUP BY season ORDER BY season"
        )
        logger.info("POST rows per season: %s", [(r["season"], r["n"]) for r in per_season])
        if after[2] != 0:
            raise SystemExit("ERROR: duplicate groups remain after migration")
        logger.info("OK - no duplicate playoff rows remain")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
