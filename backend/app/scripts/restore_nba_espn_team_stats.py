"""Restore authoritative ESPN team stats for games whose ORB/DRB were clobbered
by the pgs-sum backfill.

The pgs-sum backfill (backfill_nba_games_boxscores.py) wrote ORB/DRB from
player_game_stats for ~23k games. For games where ESPN ALSO had team stats,
the pgs values often DISAGREE with ESPN (pgs under/over-counts), leaving
ORB+DRB != TRB. This script re-fetches ESPN for those games and overwrites ALL
ESPN-derived team columns (including ORB/DRB) with the authoritative values.

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/restore_nba_espn_team_stats.py \
      [--season 35] [--limit N] [--throttle 0.15]
"""
import asyncio
import argparse
import logging
import sys
import os

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

import httpx
from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL
from app.ingestion.nba_team_stats_espn import process_game, _existing_box_cols, build_db_team_map

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("restore-espn")


def _mismatched_games(db_conn, season=None, limit=None):
    """Games where pgs ORB+DRB disagrees with stored TRB by >2 (home side),
    OR where ORB+DRB != TRB in nba.games itself. Season-scoped optionally."""
    cond = []
    params = {}
    if season:
        cond.append("g.season_id = :s")
        params["s"] = season
    where = " AND ".join(cond)
    q = f"""
        WITH p AS (
            SELECT game_id, team_id, SUM(rebounds_offensive)+SUM(rebounds_defensive) pgs_orbtot
            FROM nba.player_game_stats GROUP BY game_id, team_id
        )
        SELECT DISTINCT g.id, g.nba_game_id
        FROM nba.games g
        JOIN p ON p.game_id=g.id AND p.team_id=g.home_team_id
        WHERE g.status='FINAL' AND g.game_type IN ('REG','POST','PLAYIN')
          AND g.nba_game_id IS NOT NULL
          AND {where}{' AND ' if where else ''}(
                (ABS(p.pgs_orbtot - g.home_rebounds) > 2)
             OR (ABS(p.pgs_orbtot - g.away_rebounds) > 2)
          )
        ORDER BY g.id
    """
    if limit:
        q += " LIMIT :l"
        params["l"] = limit
    return db_conn.execute(text(q), params).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--throttle", type=float, default=0.15)
    a = ap.parse_args()

    engine = create_engine(PSYCOPG2_DATABASE_URL.replace("+asyncpg", "+psycopg2"))

    async def run():
        with engine.connect() as conn:
            valid_cols = _existing_box_cols(conn)
            db_team_map = build_db_team_map(conn)
            games = _mismatched_games(conn, a.season, a.limit)
            logger.info(f"{len(games)} games to restore from ESPN")
            total_sides = 0
            errors = 0
            async with httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0"}, timeout=30, follow_redirects=True
            ) as client:
                for i, (db_gid, espn_id) in enumerate(games, 1):
                    try:
                        sides = await process_game(
                            client, conn, str(espn_id), db_gid,
                            valid_cols=valid_cols, db_team_map=db_team_map,
                        )
                        total_sides += sides
                        if sides == 0:
                            errors += 1
                    except Exception as ex:
                        errors += 1
                        logger.warning(f"  game {db_gid} ({espn_id}): {ex}")
                    if i % 10 == 0 or i == len(games):
                        conn.commit()
                        logger.info(f"  {i}/{len(games)}, {total_sides} sides updated, {errors} skip/err")
                    await asyncio.sleep(a.throttle)
                conn.commit()
            logger.info(f"DONE: {total_sides} sides updated, {errors} skip/err over {len(games)} games")

    asyncio.run(run())


if __name__ == "__main__":
    main()
