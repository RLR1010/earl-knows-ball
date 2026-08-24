"""Backfill nba.active_players from verified FINAL boxscores.

For every FINAL NBA game (seasons 26-35, REG/POST/PLAYIN) this inserts one row
per player who actually PLAYED (minutes > 0) into nba.active_players, using the
RELIABLE minutes signal from nba.player_game_stats.

NOTES on the "who played" signal:
  * pgs.dnp / pgs.dnp_reason are currently UNRELIABLE (every player rows out as
    dnp=False / "COACH'S DECISION" even when they played 0 minutes -- see the
    2026-08-22 nba_pgs_dnp.sql wiring). Do NOT use them for the active test.
  * pgs.minutes is the source of truth: parse it as numeric minutes. Active iff
    parsed minutes > 0. Inactive patterns (trimmed): None, '-', '0', '0:00'.

Idempotent: re-running deletes each game's existing rows then re-inserts
(src='postgame'), so it is safe to run after the table is already populated.

Usage:
  cd backend && PYTHONPATH=$PWD ./venv/bin/python app/scripts/backfill_nba_active_players.py [--min-sid 26] [--max-sid 35]
"""
import sys
import os
import logging
import argparse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from sqlalchemy import create_engine, text
from app.database import async_session
from app.db_urls import SYNC_DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.nba_active_players_backfill")


def minutes_active(minutes):
    """Return True if the pgs minutes string indicates the player actually played."""
    if not minutes:
        return False
    m = str(minutes).strip()
    if not m:
        return False
    if ":" in m:
        mm, ss = m.split(":")
        return int(mm or 0) > 0 or int(ss or 0) > 0
    try:
        return int(float(m)) > 0
    except (TypeError, ValueError):
        return False


def backfill(min_sid=26, max_sid=35, dry_run=False):
    # Fetch all DISTINCT-on (game,player) boxscore rows and filter active (minutes>0) in Python.
    sql = """
        SELECT pgs.game_id, pgs.team_id, pgs.player_id,
               COALESCE(pgs.is_starter, FALSE) AS is_starter,
               COALESCE(TRIM(pgs.minutes), '') AS minutes
        FROM nba.player_game_stats pgs
        JOIN nba.games g ON g.id = pgs.game_id
        WHERE g.season_id BETWEEN :min_sid AND :max_sid
          AND g.status = 'FINAL'
    """
    eng = create_engine(SYNC_DATABASE_URL)
    with eng.connect() as c:
        rows = c.execute(text(sql), {"min_sid": min_sid, "max_sid": max_sid}).all()
    # group by game, dedupe (game,player), filter active
    per_game = {}
    stats = {"total_rows": 0, "active": 0, "inactive": 0, "games": 0}
    seen = set()
    for game_id, team_id, player_id, is_starter, minutes in rows:
        stats["total_rows"] += 1
        if not minutes_active(minutes):
            stats["inactive"] += 1
            continue
        key = (game_id, player_id)
        if key in seen:
            continue
        seen.add(key)
        stats["active"] += 1
        per_game.setdefault(game_id, []).append((team_id, player_id, is_starter))
    stats["games"] = len(per_game)
    logger.info("total pgs rows=%d active_players=%d inactive=%d games=%d",
                stats["total_rows"], stats["active"], stats["inactive"], stats["games"])
    if dry_run:
        logger.info("DRY RUN -- no writes")
        return stats
    with eng.begin() as c:
        inserted = 0
        for game_id, players in per_game.items():
            c.execute(text("DELETE FROM nba.active_players WHERE game_id = :g"), {"g": game_id})
            for team_id, player_id, is_starter in players:
                c.execute(text(
                    """INSERT INTO nba.active_players
                         (game_id, team_id, player_id, is_starter, src)
                       VALUES (:g, :t, :p, :s, 'postgame')
                       ON CONFLICT (game_id, player_id) DO NOTHING"""),
                    {"g": game_id, "t": team_id, "p": player_id, "s": is_starter})
                inserted += 1
        logger.info("inserted %d rows across %d games", inserted, stats["games"])
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-sid", type=int, default=26)
    ap.add_argument("--max-sid", type=int, default=35)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    r = backfill(a.min_sid, a.max_sid, dry_run=a.dry_run)
    print("DONE", r)
