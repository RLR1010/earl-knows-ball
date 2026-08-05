"""Backfill NULL team_id in {schema}.player_daily_props.

Resolves each prop's team from the game's two teams (home/away) by matching the
player name against the roster with accent/case-insensitive normalization —
the same logic as the ingest fix in odds_props.py.

Usage:
    cd backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/backfill_props_team_id.py
"""
import asyncio
import unicodedata

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL


def _normalize_name(name):
    if not name:
        return name
    return (
        "".join(c for c in unicodedata.normalize("NFD", str(name)) if unicodedata.category(c) != "Mn")
        .strip()
        .lower()
    )


async def backfill(schema: str, db) -> tuple:
    # Ensure the table exists (skip schemas without a props table).
    try:
        await db.execute(text(f"SELECT 1 FROM {schema}.player_daily_props LIMIT 0"))
    except Exception:
        return schema, 0, 0, 0

    before = await db.scalar(
        text(f"SELECT count(*) FROM {schema}.player_daily_props WHERE team_id IS NULL")
    )
    if not before:
        return schema, 0, 0, 0

    games = await db.execute(
        text(
            f"""
            SELECT DISTINCT p.game_id
            FROM {schema}.player_daily_props p
            WHERE p.team_id IS NULL
            """
        )
    )
    games = games.scalars().all()

    resolved = 0
    unresolved = 0
    for gid in games:
        gg = await db.execute(
            text(
                f"""
                SELECT home_team_id, away_team_id
                FROM {schema}.games
                WHERE CAST(id AS text) = :gid
                """
            ),
            {"gid": str(gid)},
        )
        gg = gg.mappings().first()
        if not gg:
            unresolved += 1
            continue
        tids = [t for t in (gg["home_team_id"], gg["away_team_id"]) if t is not None]
        if not tids:
            unresolved += 1
            continue

        roster = await db.execute(
            text(f"SELECT name, team_id FROM {schema}.players WHERE team_id = ANY(:tids)"),
            {"tids": tids},
        )
        team_map = {}
        for row in roster.mappings():
            if row["name"]:
                team_map.setdefault(_normalize_name(row["name"]), row["team_id"])

        null_rows = (
            await db.execute(
                text(
                    f"""
                    SELECT id, player_name
                    FROM {schema}.player_daily_props
                    WHERE game_id = :gid AND team_id IS NULL
                    """
                ),
                {"gid": str(gid)},
            )
        ).mappings().all()

        for row in null_rows:
            tid = team_map.get(_normalize_name(row["player_name"]))
            if tid is None:
                unresolved += 1
                continue
            await db.execute(
                text(f"UPDATE {schema}.player_daily_props SET team_id = :tid WHERE id = :pid"),
                {"tid": tid, "pid": row["id"]},
            )
            resolved += 1

    await db.commit()
    after = await db.scalar(
        text(f"SELECT count(*) FROM {schema}.player_daily_props WHERE team_id IS NULL")
    )
    return schema, before, resolved, after


async def main():
    engine = create_async_engine(ASYNC_DATABASE_URL)
    async with async_sessionmaker(engine)() as db:
        for schema in ("mlb", "nfl", "nba"):
            s, before, resolved, after = await backfill(schema, db)
            print(f"[{s}] before NULL={before} resolved={resolved} -> after NULL={after}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
