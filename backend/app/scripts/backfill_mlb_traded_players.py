"""Backfill FULL-season (combined) stats for traded players.

statsapi returns, for a traded player, a combined season split (team is None) PLUS one split per
team stint. The season loader used to store the LAST stint, so traded players' `batting_stats` /
`pitching_stats` rows under-counted their season. This re-fetches the combined split for the
affected players only (identified from our own game-level stats) and updates the season row.

Usage:
    PYTHONPATH=$PWD <venv>/bin/python app/scripts/backfill_mlb_traded_players.py [--dry-run]
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
log = logging.getLogger("backfill.traded")

BAT_SQL = text("""
WITH tms AS (
  SELECT b.player_id, g.season_id,
         CASE WHEN b.team_side='home' THEN g.home_team_id ELSE g.away_team_id END AS team_api,
         count(*) AS n
  FROM mlb.batting_game_stats b JOIN mlb.games g ON g.id=b.game_id
  GROUP BY 1,2,3
), agg AS (
  SELECT player_id, season_id FROM tms GROUP BY 1,2 HAVING count(DISTINCT team_api)>1
), pt AS (
  SELECT DISTINCT ON (player_id, season_id) player_id, season_id, team_api
  FROM tms ORDER BY player_id, season_id, n DESC, team_api
)
SELECT a.player_id, p.mlb_id, a.season_id, s.year, COALESCE(pt.team_api, bs.team_id) AS team_id
FROM agg a
JOIN mlb.players p ON p.id=a.player_id
JOIN mlb.seasons s ON s.id=a.season_id
JOIN pt ON pt.player_id=a.player_id AND pt.season_id=a.season_id
LEFT JOIN mlb.batting_stats bs ON bs.player_id=a.player_id AND bs.season_id=a.season_id
""")

PIT_SQL = text("""
WITH tms AS (
  SELECT p.pitcher_mlb_id AS mlb_id, g.season_id, p.team_abbr AS team_abbr, count(*) AS n
  FROM mlb.pitcher_game_stats p JOIN mlb.games g ON g.id=p.game_id
  GROUP BY 1,2,3
), agg AS (
  SELECT mlb_id, season_id FROM tms GROUP BY 1,2 HAVING count(DISTINCT team_abbr)>1
), pt AS (
  SELECT DISTINCT ON (mlb_id, season_id) mlb_id, season_id, team_abbr
  FROM tms ORDER BY mlb_id, season_id, n DESC, team_abbr
)
SELECT p2.id AS player_id, a.mlb_id, a.season_id, s.year, COALESCE(t.id, ps.team_id) AS team_id
FROM agg a
JOIN mlb.players p2 ON p2.mlb_id=a.mlb_id
JOIN mlb.seasons s ON s.id=a.season_id
JOIN pt ON pt.mlb_id=a.mlb_id AND pt.season_id=a.season_id
LEFT JOIN mlb.teams t ON t.abbreviation=pt.team_abbr
LEFT JOIN mlb.pitching_stats ps ON ps.player_id=p2.id AND ps.season_id=a.season_id
""")


async def fetch_splits(client, mlb_id, year, group):
    r = await client.get(f"{API}/people/{mlb_id}/stats",
                         params={"stats": "season", "group": group, "season": year, "gameType": "R"},
                         timeout=60)
    r.raise_for_status()
    for se in r.json().get("stats", []):
        if se.get("group", {}).get("displayName", "").lower() == group:
            return se.get("splits", [])
    return []


async def run_group(group: str, sql, upsert, dry: bool):
    async with async_session() as db:
        rows = (await db.execute(sql)).mappings().all()
    log.info("%s: %d affected player-seasons", group, len(rows))

    fetched = {}
    async with httpx.AsyncClient() as client:
        sem = asyncio.Semaphore(6)

        async def worker(r):
            async with sem:
                try:
                    sp = await fetch_splits(client, r["mlb_id"], r["year"], group)
                except Exception as e:
                    log.warning("fetch failed %s %s: %s", r["mlb_id"], r["year"], str(e)[:100])
                    sp = []
                fetched[(r["mlb_id"], r["year"])] = sp
        await asyncio.gather(*(worker(r) for r in rows))

    if dry:
        for r in rows[:10]:
            sp = fetched.get((r["mlb_id"], r["year"]), [])
            tot = next((s for s in sp if not s.get("team")), None)
            log.info("  %s %s -> total=%s", r["mlb_id"], r["year"], (tot or {}).get("stat", {}).get("avg"))
        return len(rows)

    updated = 0
    skipped = 0
    async with async_session() as db:
        for r in rows:
            if not r["team_id"]:
                skipped += 1
                continue
            sp = fetched.get((r["mlb_id"], r["year"]), [])
            tot = next((s for s in sp if not s.get("team")), None)
            if tot is None:
                continue
            await upsert(db, tot, r["year"], r["season_id"], r["team_id"])
            updated += 1
        await db.commit()
    log.info("%s: updated %d rows (skipped %d with no resolvable team)", group, updated, skipped)
    return updated


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    nb = await run_group("hitting", BAT_SQL, _upsert_batting_row, args.dry_run)
    np_ = await run_group("pitching", PIT_SQL, _upsert_pitching_row, args.dry_run)
    log.info("done: batting=%s pitching=%s", nb, np_)


if __name__ == "__main__":
    asyncio.run(main())
