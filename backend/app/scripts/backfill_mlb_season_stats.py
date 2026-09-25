"""Backfill ALL MLB season batting/pitching rows from statsapi's combined season totals.

statsapi `stats?stats=season&group=<g>&season=<Y>&playerPool=ALL` returns ONE row per player with
the COMBINED season totals (for traded players too), in a single call per season. We use it to make
every `mlb.batting_stats` / `mlb.pitching_stats` row a full-season total (fixes traded-player gap,
including pre-2012 seasons where we have no game-level data).

Idempotent: updates existing (player, season) rows in place; never creates rows for players we don't have.

Usage:
    PYTHONPATH=$PWD <venv>/bin/python app/scripts/backfill_mlb_season_stats.py [--start 2006] [--end 2026] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

import httpx
from sqlalchemy import text

from app.database import async_session
from app.ingestion.mlb_stats import _upsert_batting_row, _upsert_pitching_row

API = "https://statsapi.mlb.com/api/v1"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("backfill.season_stats")


async def fetch_totals(client, year, group):
    r = await client.get(f"{API}/stats",
                         params={"stats": "season", "group": group, "season": year,
                                 "gameType": "R", "playerPool": "ALL", "limit": 2000},
                         timeout=90)
    r.raise_for_status()
    out = {}
    for se in r.json().get("stats", []):
        for s in se.get("splits", []):
            pid = s.get("player", {}).get("id")
            if pid and s.get("stat", {}).get("gamesPlayed"):
                out[pid] = (s["stat"], (s.get("team") or {}).get("id"))
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2006)
    ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    async with async_session() as db:
        seasons = (await db.execute(text(
            "SELECT id, year FROM mlb.seasons WHERE year BETWEEN :a AND :b ORDER BY year"),
            {"a": args.start, "b": args.end})).mappings().all()
        teams = (await db.execute(text("SELECT id, api_team_id FROM mlb.teams"))).mappings().all()
    team_by_api = {t["api_team_id"]: t["id"] for t in teams if t["api_team_id"]}

    async with httpx.AsyncClient() as client:
        for s in seasons:
            year, sid = s["year"], s["id"]
            for group, table, upsert in (("hitting", "batting_stats", _upsert_batting_row),
                                         ("pitching", "pitching_stats", _upsert_pitching_row)):
                totals = await fetch_totals(client, year, group)
                async with async_session() as db:
                    existing = (await db.execute(text(f"""
                        SELECT b.player_id, p.mlb_id, b.team_id
                        FROM mlb.{table} b JOIN mlb.players p ON p.id=b.player_id
                        WHERE b.season_id=:sid"""), {"sid": sid})).mappings().all()
                    n = 0
                    for row in existing:
                        hit = totals.get(row["mlb_id"])
                        if not hit:
                            continue
                        stat, team_api = hit
                        team_db = team_by_api.get(team_api) or row["team_id"]
                        if not team_db:
                            continue
                        if args.dry_run:
                            n += 1
                            continue
                        await upsert(db, {"stat": stat, "player": {"id": row["mlb_id"]}}, year, sid, team_db)
                        n += 1
                    if not args.dry_run:
                        await db.commit()
                log.info("%s %s: %d rows %s", year, group, n, "(dry)" if args.dry_run else "updated")


if __name__ == "__main__":
    asyncio.run(main())
