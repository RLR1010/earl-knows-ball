#!/usr/bin/env python3
"""Load nflverse season ROSTERS and DRAFT PICKS for offseason roster-turnover modelling.

Sources (nflverse-data releases, free):
  rosters:      https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{year}.csv
  draft picks:  https://github.com/nflverse/nflverse-data/releases/download/draft_picks/draft_picks.csv

Tables:
  nfl.rosters       (season, team_abbr, position, depth_chart_position, status,
                     gsis_id, player_id, full_name, years_exp, entry_year, draft_number)
  nfl.draft_picks   (season, round, pick, team_abbr, gsis_id, player_id, player_name,
                     position, college)

Idempotent: each season is DELETEd before insert.

Usage:
    python -m app.ingestion.load_rosters 2022 2026
"""
from __future__ import annotations

import asyncio
import csv
import io
import sys
from urllib.request import Request, urlopen

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL

ROSTER_URL = "https://github.com/nflverse/nflverse-data/releases/download/rosters/roster_{y}.csv"
DRAFT_URL = "https://github.com/nflverse/nflverse-data/releases/download/draft_picks/draft_picks.csv"

DDL_ROSTERS = """
CREATE TABLE IF NOT EXISTS nfl.rosters (
    season                integer NOT NULL,
    team_abbr             varchar(8),
    position              varchar(8),
    depth_chart_position  varchar(8),
    status                varchar(32),
    gsis_id               varchar(16) NOT NULL,
    player_id             integer,
    full_name             text,
    years_exp             integer,
    entry_year            integer,
    draft_number          integer,
    updated_at            timestamptz DEFAULT now(),
    PRIMARY KEY (season, gsis_id)
);
"""

DDL_DRAFT = """
CREATE TABLE IF NOT EXISTS nfl.draft_picks (
    season       integer NOT NULL,
    round        integer,
    pick         integer NOT NULL,
    team_abbr    varchar(8),
    gsis_id      varchar(16),
    player_id    integer,
    player_name  text,
    position     varchar(8),
    college      text,
    updated_at   timestamptz DEFAULT now(),
    PRIMARY KEY (season, pick)
);
"""


def _get(url: str) -> str:
    req = Request(url, headers={"User-Agent": "earl-knows-ball/1.0"})
    with urlopen(req, timeout=90) as r:  # noqa: S310 (trusted host)
        return r.read().decode("utf-8", "replace")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def _gsis_map(db) -> dict[str, int]:
    rows = (await db.execute(text(
        "SELECT nflverse_id, id FROM nfl.players WHERE nflverse_id IS NOT NULL"
    ))).all()
    return {r[0]: r[1] for r in rows}


async def load_draft_picks(db, seasons: list[int]) -> int:
    await db.execute(text(DDL_DRAFT))
    gsis = await _gsis_map(db)
    txt = _get(DRAFT_URL)
    rows = list(csv.DictReader(io.StringIO(txt)))
    n = 0
    for y in seasons:
        await db.execute(text("DELETE FROM nfl.draft_picks WHERE season = :y"), {"y": y})
        for r in rows:
            if _int(r.get("season")) != y:
                continue
            g = (r.get("gsis_id") or "").strip() or None
            await db.execute(
                text("""
                    INSERT INTO nfl.draft_picks
                        (season, round, pick, team_abbr, gsis_id, player_id,
                         player_name, position, college)
                    VALUES (:season,:round,:pick,:team,:gsis,:pid,:name,:pos,:college)
                    ON CONFLICT (season, pick) DO UPDATE SET
                        round=EXCLUDED.round, team_abbr=EXCLUDED.team_abbr,
                        gsis_id=EXCLUDED.gsis_id, player_id=EXCLUDED.player_id,
                        player_name=EXCLUDED.player_name, position=EXCLUDED.position,
                        college=EXCLUDED.college, updated_at=now()
                """),
                {
                    "season": y,
                    "round": _int(r.get("round")),
                    "pick": _int(r.get("pick")),
                    "team": (r.get("team") or "").strip() or None,
                    "gsis": g,
                    "pid": gsis.get(g) if g else None,
                    "name": (r.get("pfr_player_name") or "").strip() or None,
                    "pos": (r.get("position") or "").strip() or None,
                    "college": (r.get("college") or "").strip() or None,
                },
            )
            n += 1
        await db.commit()
    return n


async def load_rosters(db, seasons: list[int]) -> int:
    await db.execute(text(DDL_ROSTERS))
    gsis = await _gsis_map(db)
    n = 0
    for y in seasons:
        try:
            txt = _get(ROSTER_URL.format(y=y))
        except Exception as exc:  # noqa: BLE001
            print(f"  {y}: skip ({exc})")
            continue
        rows = list(csv.DictReader(io.StringIO(txt)))
        # keep the latest weekly snapshot per player
        best: dict[str, dict] = {}
        for r in rows:
            g = (r.get("gsis_id") or "").strip()
            if not g:
                continue
            cur = best.get(g)
            if cur is None or _int(r.get("week")) or 0 >= (_int(cur.get("week")) or 0):
                best[g] = r
        await db.execute(text("DELETE FROM nfl.rosters WHERE season = :y"), {"y": y})
        for g, r in best.items():
            await db.execute(
                text("""
                    INSERT INTO nfl.rosters
                        (season, team_abbr, position, depth_chart_position, status,
                         gsis_id, player_id, full_name, years_exp, entry_year, draft_number)
                    VALUES (:season,:team,:pos,:dcp,:status,:gsis,:pid,:name,
                            :yexp,:eyear,:dnum)
                    ON CONFLICT (season, gsis_id) DO UPDATE SET
                        team_abbr=EXCLUDED.team_abbr, position=EXCLUDED.position,
                        depth_chart_position=EXCLUDED.depth_chart_position,
                        status=EXCLUDED.status, player_id=EXCLUDED.player_id,
                        full_name=EXCLUDED.full_name, years_exp=EXCLUDED.years_exp,
                        entry_year=EXCLUDED.entry_year, draft_number=EXCLUDED.draft_number,
                        updated_at=now()
                """),
                {
                    "season": y,
                    "team": (r.get("team") or "").strip() or None,
                    "pos": (r.get("position") or "").strip() or None,
                    "dcp": (r.get("depth_chart_position") or "").strip() or None,
                    "status": (r.get("status") or "").strip() or None,
                    "gsis": g,
                    "pid": gsis.get(g),
                    "name": (r.get("full_name") or "").strip() or None,
                    "yexp": _int(r.get("years_exp")),
                    "eyear": _int(r.get("entry_year")),
                    "dnum": _int(r.get("draft_number")),
                },
            )
            n += 1
        await db.commit()
        cnt = await db.scalar(text("SELECT count(*) FROM nfl.rosters WHERE season=:y"), {"y": y})
        print(f"  rosters {y}: {cnt}")
    return n


async def main_async(seasons: list[int]) -> None:
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            print("draft picks...")
            n = await load_draft_picks(db, seasons)
            print(f"  draft picks rows: {n}")
            print("rosters...")
            await load_rosters(db, seasons)
    finally:
        await engine.dispose()


def main(argv: list[str]) -> None:
    a, b = (int(argv[0]), int(argv[1])) if len(argv) >= 2 else (2022, 2026)
    asyncio.run(main_async(list(range(a, b + 1))))


if __name__ == "__main__":
    main(sys.argv[1:])
