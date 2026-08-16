"""Backfill nba.games team box-score stats from ESPN's core API team-statistics
endpoint for all games that have an nba_game_id, oldest-to-newest (or scope by
--season / --recent).

This populates the NEW columns added by
migrations/20260816_nba_game_boxscore_team_stats.sql (real offensive/defensive
rebounds, estimatedPossessions, pointsInPaint, fastBreakPoints, turnoverPoints,
team/total turnovers, lead/flow stats, fouling detail, double/triple double,
advanced ratios/ratings, VORP, etc.). These team-only stats are NOT derivable by
summing player_game_stats, so they must be fetched from ESPN directly.

Throttled to be gentle on ESPN's core API.

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/backfill_nba_games_espn_team_stats.py
  # options: --season 35 --recent 30 --throttle 0.2
"""
import argparse
import asyncio
import logging
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL

from app.ingestion.nba_team_stats_espn import run_for_games

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("nba-team-stats-backfill")


def pick_games(engine, season=None, recent=None):
    """Return {espn_game_id: db_game_id} for games needing backfill.

    Filters to games that have an nba_game_id AND whose team-stats have not yet
    been filled (home_estimated_possessions IS NULL). Optionally scoped by
    --season (integer season_id) or --recent (N most recent games).
    """
    base = """
        SELECT g.nba_game_id, g.id
        FROM nba.games g
        WHERE g.nba_game_id IS NOT NULL
          AND g.home_estimated_possessions IS NULL
    """
    # Newest-first so live/current seasons are covered before deep history.
    base += " ORDER BY g.date DESC"
    params = {}
    if season is not None:
        base += " AND g.season_id = :season"
        params["season"] = season
    if recent is not None:
        # most recent N by date
        sub = base.replace("SELECT g.nba_game_id, g.id", "SELECT g.nba_game_id, g.id")
        recent_sql = f"""
            SELECT sub.nba_game_id, sub.id FROM (
                {sub} ORDER BY g.date DESC LIMIT :limit
            ) sub
        """
        params["limit"] = recent
        base = recent_sql
    with engine.connect() as conn:
        rows = conn.execute(text(base), params).fetchall()
    out = {int(r[0]): int(r[1]) for r in rows}
    logger.info("Selected %d games for team-stats backfill (%s)", len(out),
                f"season={season}" if season else f"recent={recent}" if recent else "all")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None, help="Only backfill this season_id")
    ap.add_argument("--recent", type=int, default=None, help="Only backfill N most recent games")
    ap.add_argument("--throttle", type=float, default=0.15, help="Seconds between ESPN game fetches")
    ap.add_argument("--dry-run", action="store_true", help="Print selected games without fetching")
    args = ap.parse_args()

    engine = create_engine(PSYCOPG2_DATABASE_URL.replace("+asyncpg", "+psycopg2"))
    games = pick_games(engine, season=args.season, recent=args.recent)

    if not games:
        logger.info("Nothing to backfill.")
        return

    if args.dry_run:
        for espn_id, db_id in list(games.items())[:50]:
            logger.info("  espn=%s db=%s", espn_id, db_id)
        logger.info("Dry run: %d games would be processed.", len(games))
        return

    # Chunk large backfills so we can report progress & resume cleanly.
    asyncio.run(run_for_games(games, throttle=args.throttle))


if __name__ == "__main__":
    main()
