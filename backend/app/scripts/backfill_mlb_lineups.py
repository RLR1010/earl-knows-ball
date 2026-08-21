"""
Backfill MLB starting lineups (batting order) for games 2016 - today from the
authoritative MLB StatsAPI boxscore endpoint (teams.{away,home}.battingOrder).

Resolves each starter to our players.id via players.mlb_id (so mlb.lineups.player_id
is the real DB player id, enabling lineup-OPS features).

Resumable: skips any game that already has lineup rows. Safe to re-run.

Usage (as systemd user, non-login shell):
    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    ./venv/bin/python app/scripts/backfill_mlb_lineups.py --limit 10   # test
    ./venv/bin/python app/scripts/backfill_mlb_lineups.py               # full 2016-2026
    ./venv/bin/python app/scripts/backfill_mlb_lineups.py --year 2016   # one season
"""
import asyncio
import sys
import os
import time
import logging
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from app.database import async_session
from app.ingestion.mlb_lineups import fetch_lineups, save_lineups

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("earl.lineup_backfill")

SELECT_SQL = """
    SELECT g.id AS game_id, g.mlb_game_id, g.date::date AS date,
           ht.abbreviation AS ha, at.abbreviation AS aa
    FROM mlb.games g
    JOIN mlb.teams ht ON ht.id = g.home_team_id
    JOIN mlb.teams at ON at.id = g.away_team_id
    WHERE g.mlb_game_id IS NOT NULL
      AND EXTRACT(YEAR FROM g.date)::int >= {min_year}
      AND NOT EXISTS (SELECT 1 FROM mlb.lineups l WHERE l.game_id = g.id)
    ORDER BY g.date
"""


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-year", type=int, default=2016)
    args = ap.parse_args()

    async with async_session() as db:
        games = (await db.execute(text(SELECT_SQL.format(min_year=args.min_year)))).all()
        games = [g._asdict() for g in games]
        logger.info(f"Discovered {len(games)} games missing lineups (year>={args.min_year})")

        if args.limit and args.limit > 0:
            games = games[: args.limit]

        ok = 0
        skip = 0
        errors = 0
        start = time.time()

        for i, game in enumerate(games):
            pk = game["mlb_game_id"]
            try:
                data = await fetch_lineups(pk)
                if "error" in data:
                    logger.warning(f"  [{(i+1)}/{len(games)}] {game['date']} {game['ha']}@{game['aa']} fetch ERR {data['error']}")
                    errors += 1
                    continue
                away_lu = data.get("away_lineup", [])
                home_lu = data.get("home_lineup", [])
                if not away_lu and not home_lu:
                    logger.warning(f"  [{(i+1)}/{len(games)}] {game['date']} {game['ha']}@{game['aa']} empty lineups")
                    skip += 1
                    continue
                await save_lineups(db, game["game_id"], away_lu, home_lu)
                await db.commit()
                ok += 1
            except Exception as e:
                errors += 1
                logger.warning(f"  [{(i+1)}/{len(games)}] {game['date']} {game['ha']}@{game['aa']} ERR {e}")
                await db.rollback()

            if (i + 1) % 25 == 0:
                elapsed = time.time() - start
                logger.info(f"  [{i+1}/{len(games)}] ok={ok} errors={errors} skip={skip} "
                            f"({(i+1)/elapsed:.2f} games/s, ~{(len(games)-(i+1))/((i+1)/elapsed):.0f}s remaining)")

        elapsed = time.time() - start
        logger.info(f"\nDONE: {len(games)} games, ok={ok}, errors={errors}, skip/empty={skip} "
                    f"in {elapsed:.0f}s ({(len(games))/elapsed:.2f} games/s)")


if __name__ == "__main__":
    asyncio.run(main())
