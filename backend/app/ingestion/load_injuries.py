#!/usr/bin/env python3
"""Load nflverse weekly injury reports into nfl.injuries.

Idempotent per season (delete-then-insert). Denormalises team_abbr + position +
report_status so downstream consumers don't have to re-join players.

Usage:
    python -m app.ingestion.load_injuries            # 2022..current
    python -m app.ingestion.load_injuries 2026       # single season
"""
from __future__ import annotations

import asyncio
import csv
import io
import sys

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL

NFLVERSE_BASE = "https://github.com/nflverse/nflverse-data/releases/download/injuries"
DEFAULT_YEARS = list(range(2022, 2027))

# Statuses that matter for availability, with the weight applied to a player's value.
STATUS_WEIGHT = {
    "out": 1.0,
    "doubtful": 0.6,
    "questionable": 0.25,
}


def _clean(v) -> str:
    return ("" if v is None else str(v)).strip()


async def load(years: list[int] | None = None) -> int:
    years = years or DEFAULT_YEARS
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    total = 0
    try:
        async with Session() as db:
            # additive columns (safe on existing table)
            for col in (
                "team_abbr varchar(8)",
                "position varchar(8)",
                "report_status varchar(50)",
            ):
                await db.execute(
                    text(f"ALTER TABLE nfl.injuries ADD COLUMN IF NOT EXISTS {col}")
                )
            await db.commit()

            pmap = {
                r[1]: r[0]
                for r in (
                    await db.execute(
                        text(
                            "SELECT id, nflverse_id FROM nfl.players "
                            "WHERE nflverse_id IS NOT NULL"
                        )
                    )
                ).all()
            }
            smap = {
                r[1]: r[0]
                for r in (await db.execute(text("SELECT id, year FROM nfl.seasons"))).all()
            }

            async with httpx.AsyncClient(follow_redirects=True, timeout=90) as cl:
                for y in years:
                    sid = smap.get(y)
                    if sid is None:
                        print(f"{y}: SKIP (no season row)")
                        continue
                    url = f"{NFLVERSE_BASE}/injuries_{y}.csv"
                    resp = await cl.get(url)
                    if resp.status_code != 200:
                        print(f"{y}: SKIP (http {resp.status_code})")
                        continue
                    rows = list(csv.DictReader(io.StringIO(resp.text)))
                    reg = [
                        x
                        for x in rows
                        if _clean(x.get("season_type") or "REG").upper() == "REG"
                    ]
                    await db.execute(
                        text("DELETE FROM nfl.injuries WHERE season_id = :s"), {"s": sid}
                    )
                    batch = []
                    for x in reg:
                        pid = pmap.get(_clean(x.get("gsis_id")))
                        if not pid:
                            continue
                        try:
                            wk = int(_clean(x.get("week")) or 0)
                        except ValueError:
                            continue
                        if wk < 1 or wk > 22:
                            continue
                        report_status = _clean(x.get("report_status"))
                        batch.append(
                            {
                                "player_id": pid,
                                "week": wk,
                                "season_id": sid,
                                "injury_type": (
                                    _clean(x.get("report_primary_injury"))
                                    or _clean(x.get("practice_primary_injury"))
                                    or "Unknown"
                                )[:100],
                                "practice_status": _clean(x.get("practice_status"))[:50],
                                "game_status": (
                                    report_status or _clean(x.get("practice_status"))
                                )[:50],
                                "team_abbr": _clean(x.get("team"))[:8],
                                "position": _clean(x.get("position"))[:8],
                                "report_status": report_status[:50],
                            }
                        )
                    if batch:
                        await db.execute(
                            text(
                                """
                                INSERT INTO nfl.injuries
                                  (player_id, week, season_id, injury_type, practice_status,
                                   game_status, team_abbr, position, report_status)
                                VALUES (:player_id, :week, :season_id, :injury_type,
                                        :practice_status, :game_status, :team_abbr,
                                        :position, :report_status)
                                """
                            ),
                            batch,
                        )
                    await db.commit()
                    print(f"{y}: {len(reg)} reg rows -> {len(batch)} inserted")
                    total += len(batch)
    finally:
        await engine.dispose()
    print(f"Done: {total} injury rows")
    return total


def main(argv: list[str]) -> None:
    years = [int(a) for a in argv[1:]] or None
    asyncio.run(load(years))


if __name__ == "__main__":
    main(sys.argv)
