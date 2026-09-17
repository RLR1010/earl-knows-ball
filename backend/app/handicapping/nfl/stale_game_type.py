"""Purge stale derived-stat rows whose game_type disagrees with nfl.games.

Why this exists
---------------
An older build of the NFL derived stat tables (qb_cumulative_stats,
qb_rolling_stats, skill_rolling_stats, kicker_rolling_stats,
qb_badweather_stats, team_badweather_stats) wrote postseason games with
``game_type='REG'``.  Those tables upsert on a primary key that *includes*
game_type, so the bogus 'REG' twin of a real 'POST' game was never replaced and
never deleted -> two rows per playoff game.

That matters because history lookups filter on game_type.  A lookup that asks
for ``game_type='REG'`` (which the loader used to do, via a constant ``gt``)
would happily pick the bogus playoff row, silently mixing postseason data into
features it was never meant to see.

The current builders derive game_type from ``nfl.games`` (correct), so this is a
cleanup/monitoring safety net that belongs at the end of the stats refresh: it
should normally remove 0 rows.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Every nfl.* table that carries a game_type column alongside a game_id.
TABLES: tuple[str, ...] = (
    "qb_cumulative_stats",
    "qb_rolling_stats",
    "skill_rolling_stats",
    "kicker_rolling_stats",
    "qb_badweather_stats",
    "team_badweather_stats",
    "cumulative_game_stats",
    "team_rolling_stats",
    "defensive_rolling_stats",
    "team_pace_stats",
)


async def purge_stale_game_type_rows(conn, tables: tuple[str, ...] = TABLES, apply: bool = True) -> dict[str, int]:
    """Delete rows whose game_type disagrees with nfl.games.game_type.

    ``conn`` is an asyncpg connection.  Returns ``{table: rows}`` (rows removed,
    or rows that *would* be removed when ``apply=False``).
    """
    out: dict[str, int] = {}
    for t in tables:
        has_col = await conn.fetchval(
            "select count(*) from information_schema.columns "
            "where table_schema='nfl' and table_name=$1 and column_name='game_type'",
            t,
        )
        if not has_col:
            continue
        if apply:
            res = await conn.execute(
                f"delete from nfl.{t} x using nfl.games g "
                "where g.id = x.game_id and x.game_type <> g.game_type"
            )
            n = int(res.split()[-1]) if res else 0
        else:
            n = await conn.fetchval(
                f"select count(*) from nfl.{t} x join nfl.games g on g.id = x.game_id "
                "where x.game_type <> g.game_type"
            )
        out[t] = int(n or 0)
    return out


async def _main() -> int:
    import argparse
    import asyncio

    import asyncpg

    from app.db_urls import PSYCOPG2_DATABASE_URL

    ap = argparse.ArgumentParser(description="Purge derived rows whose game_type disagrees with nfl.games")
    ap.add_argument("--apply", action="store_true", help="actually delete (default: report only)")
    ap.add_argument("--tables", nargs="*", default=list(TABLES))
    args = ap.parse_args()

    conn = await asyncpg.connect(PSYCOPG2_DATABASE_URL)
    try:
        res = await purge_stale_game_type_rows(conn, tuple(args.tables), apply=args.apply)
    finally:
        await conn.close()
    mode = "removed" if args.apply else "would remove"
    for t, n in res.items():
        logger.info(f"{t:26s} {mode}: {n}")
    return 0


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    raise SystemExit(asyncio.run(_main()))
