"""Build `nfl.roster_changes` (the offseason acquisitions/losses table) from the
shared v2 roster-turnover model.  Writes the per-team net value of players GAINED
minus LOST between the prior season's roster and each season's roster.

Consumed by `compute_power_ratings.py`, which adds `net_pts` to the season's prior
anchor (strongest at week 1, fading through shrinkage).

  python -m app.scripts.compute_roster_changes 2024 2025 2026
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL
from app.ingestion.roster_change_model import team_deltas

DDL = """
CREATE TABLE IF NOT EXISTS nfl.roster_changes (
    season        integer      NOT NULL,
    team_id       integer      NOT NULL,
    team_abbr     text,
    net_pts       double precision,
    net_raw       double precision,
    model_version text,
    updated_at    timestamptz  DEFAULT now(),
    PRIMARY KEY (season, team_id)
);
"""

CLIP = 1.2  # keep the adjustment gentle (v2 raw outliers reach +/-2.5)
MODEL = "off-nfl-v2"
# nfl.rosters uses some franchise abbrs that differ from nfl.teams
ABBR_ALIAS = {"LA": "LAR", "STL": "LAR", "SD": "LAC", "OAK": "LV", "WSH": "WAS"}


async def _write_year(db, year: int) -> None:
    deltas = await team_deltas(db, year - 1, year)
    tid = {
        r[0]: r[1]
        for r in (await db.execute(text("SELECT abbreviation, id FROM nfl.teams"))).all()
    }
    await db.execute(text("DELETE FROM nfl.roster_changes WHERE season = :y"), {"y": year})
    n = 0
    for abbr, raw in deltas.items():
        abbr = ABBR_ALIAS.get(abbr, abbr)
        if abbr not in tid:
            continue
        net = max(-CLIP, min(CLIP, raw))
        await db.execute(
            text(
                "INSERT INTO nfl.roster_changes "
                "(season, team_id, team_abbr, net_pts, net_raw, model_version) "
                "VALUES (:y, :t, :a, :n, :r, :m)"
            ),
            {"y": year, "t": tid[abbr], "a": abbr, "n": net, "r": raw, "m": MODEL},
        )
        n += 1
    print(f"nfl {year}: wrote {n} teams (range {min(deltas.values()):.2f}..{max(deltas.values()):.2f})")


async def main(years: list[int]) -> None:
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        await db.execute(text(DDL))
        for y in years:
            await _write_year(db, y)
        await db.commit()
    await engine.dispose()


if __name__ == "__main__":
    years = [int(a) for a in sys.argv[1:]] or [2024, 2025, 2026]
    asyncio.run(main(years))
