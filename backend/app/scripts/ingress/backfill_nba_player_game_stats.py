"""Targeted backfill of nba.player_game_stats from ESPN.

Fills ONLY the games that are missing per-game player data:
  - Seasons 16-21 (NBA years 2006-2011): all REG + POST games (wholesale gap).
  - Seasons 22-28 (NBA years 2012-2018): only games where a team-side has zero
    player_game_stats rows (scattered ingest gap).

Reuses the exact ESPN fetch/parse/match logic from nba_player_game_stats.process_game
so rows are schema-consistent and accurate. Idempotent (ON CONFLICT DO NOTHING),
resumable (skips games already complete), and rate-limit aware.

Usage:
  cd <repo>/backend && PYTHONPATH=$PWD <venv>/bin/python \
      app/scripts/ingress/backfill_nba_player_game_stats.py [season_year ...]
  Without args: backfills years 2006-2018 (seasons 16-28).
"""
import asyncio
import httpx
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.nba_pgs_backfill")

REPO = "/home/rich/.openclaw/workspace/earl-knows-football"
sys.path.insert(0, f"{REPO}/backend")

from sqlalchemy import create_engine, text
from app.ingestion.nba_player_game_stats import process_game
from app.db_urls import PSYCOPG2_DATABASE_URL

# NBA season_id = year - 1990. year -> season_id mapping for scope.
DEFAULT_YEARS = list(range(2006, 2019))  # 2006..2018 => seasons 16..28


def _build_espn_cache(db_conn) -> dict:
    rows = db_conn.execute(
        text("SELECT id, espn_id FROM nba.players WHERE espn_id IS NOT NULL")
    ).fetchall()
    return {int(eid): pid for pid, eid in rows}


def _incomplete_games(db_conn, year: int) -> list:
    """Return (db_game_id, nba_game_id, home_abbr, away_abbr) for games that
    need data: any FINAL REG/POST game where a participating team-side has zero
    player_game_stats rows."""
    return db_conn.execute(text("""
        SELECT g.id, g.nba_game_id, h.abbreviation, a.abbreviation
        FROM nba.games g
        JOIN nba.seasons s ON s.id = g.season_id
        JOIN nba.teams h ON h.id = g.home_team_id
        JOIN nba.teams a ON a.id = g.away_team_id
        WHERE s.year = :year AND g.game_type IN ('REG','POST')
          AND g.status::text = 'FINAL'
          AND g.nba_game_id IS NOT NULL
          AND (
            NOT EXISTS (SELECT 1 FROM nba.player_game_stats ph
                        WHERE ph.game_id = g.id AND ph.team_id = g.home_team_id)
            OR NOT EXISTS (SELECT 1 FROM nba.player_game_stats pa
                           WHERE pa.game_id = g.id AND pa.team_id = g.away_team_id)
          )
        ORDER BY g.date
    """), {"year": year}).fetchall()


async def backfill_year(year: int, limit: int = 0) -> dict:
    engine = create_engine(PSYCOPG2_DATABASE_URL)
    started = time.time()
    out = {"year": year, "games": 0, "rows": 0, "empty": 0}

    try:
        with engine.connect() as db_conn:
            espn_cache = _build_espn_cache(db_conn)
            games = _incomplete_games(db_conn, year)
            if limit:
                games = games[:limit]
            logger.info(f"[{year}] {len(games)} games need data ({len(espn_cache)} players cached)")

            async with httpx.AsyncClient(timeout=30) as client:
                total = 0
                errors = 0
                done = 0
                commit_counter = 0
                cooldown_counter = 0

                for idx, (db_gid, nba_gid, home_abbr, away_abbr) in enumerate(games, 1):
                    await asyncio.sleep(0.3)
                    cooldown_counter += 1
                    if cooldown_counter >= 100:
                        logger.info(f"    [{year}] 30s cooldown after {idx} games")
                        await asyncio.sleep(30)
                        cooldown_counter = 0

                    rows = await process_game(
                        client, db_conn, nba_gid, db_gid, home_abbr, away_abbr, espn_cache
                    )
                    if rows:
                        total += rows
                    else:
                        errors += 1

                    commit_counter += 1
                    if commit_counter >= 20:
                        db_conn.commit()
                        commit_counter = 0

                    if idx % 50 == 0 or idx == len(games):
                        done = idx
                        logger.info(f"  [{year}] {idx}/{len(games)} games, {total} rows, {errors} empty")

                if commit_counter > 0:
                    db_conn.commit()

            # Post-run completeness check for this year
            incomplete = _incomplete_games(db_conn, year)
            logger.info(
                f"[{year}] DONE: processed {done}/{len(games)} games, {total} rows, "
                f"{errors} empty, {len(incomplete)} still missing, {time.time()-started:.0f}s"
            )
            out.update({"games": len(games), "rows": total, "empty": errors,
                        "still_missing": len(incomplete)})
    finally:
        engine.dispose()
    return out


async def main(argv) -> int:
    # support: --limit N  (test cap for each year), plus explicit NBA YEAR args
    limit = 0
    years = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--limit":
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                limit = int(argv[i + 1]); i += 1
        elif a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        elif a.lstrip("-").isdigit():
            years.append(int(a))
        i += 1
    if not years:
        years = DEFAULT_YEARS
    years = sorted({y for y in years if 1980 <= y <= 2026})
    logger.info(f"Backfilling NBA player_game_stats for years: {years} (limit per year: {limit or 'none'})")

    summary = []
    for year in years:
        try:
            summary.append(await backfill_year(year, limit=limit))
        except Exception as e:
            logger.exception(f"[{year}] FAILED: {e}")
            summary.append({"year": year, "error": str(e)})

    logger.info("=== BACKFILL SUMMARY ===")
    for s in summary:
        logger.info(s)
    return 0


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
