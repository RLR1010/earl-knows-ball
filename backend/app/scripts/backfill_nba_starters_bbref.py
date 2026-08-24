"""Backfill missing NBA starters in nba.active_players from basketball-reference.

Cross-check approach: bball-ref boxscore tables list players STARTERS-FIRST (in
starting order), then reserves. For any team-game in nba.active_players that has
fewer than 5 is_starter rows, we fetch the bball-ref boxscore and take the first
5 players per team as the starting five.

STRICT GUARDRAIL (per Rich): a bball-ref starter is only marked is_starter=TRUE
if that player already has a boxscore row (nba.player_game_stats) for the exact
same game AND is present in nba.active_players for that team-game — i.e. they
genuinely played/started in THAT game per our own boxscore. This prevents
name-matching artifacts from flagging players who were never on the floor. We
never auto-create players and never un-flag an existing starter.

Usage:
  python app/scripts/backfill_nba_starters_bbref.py --limit 30            # dry-run
  python app/scripts/backfill_nba_starters_bbref.py --limit 30 --commit   # apply
"""
import argparse
import re
import time
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.nba_starter_backfill_bbref")

from sqlalchemy import create_engine, text

sys.path.insert(0, "app/scripts/ingress")
from backfill_nba_pgs_bbref import (
    _fetch,
    _parse_player_stats,
    _br_abbr_candidates,
    _norm_abbr,
    BR,
)

SYNC_DATABASE_URL = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"


def _et_date(game_date):
    """bball-ref games are indexed by the US Eastern (America/New_York) date."""
    from zoneinfo import ZoneInfo
    from datetime import datetime
    try:
        if hasattr(game_date, "tzinfo"):
            local = game_date.astimezone(ZoneInfo("America/New_York"))
        else:
            local = datetime.fromisoformat(str(game_date)).replace(
                tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        return local.strftime("%Y%m%d"), local.year
    except Exception:
        if hasattr(game_date, "strftime"):
            ds = game_date.strftime("%Y%m%d")
            return ds, int(ds[:4])
        ds = str(game_date)[:10].replace("-", "")
        return ds, int(ds[:4])


def short_team_games(db_conn, limit):
    """Team-games in active_players with <5 starters, most-empty first."""
    q = text("""
        WITH tg AS (
            SELECT a.game_id, a.team_id,
                   COUNT(*) FILTER (WHERE a.is_starter) cnt
            FROM nba.active_players a
            GROUP BY a.game_id, a.team_id
            HAVING COUNT(*) FILTER (WHERE a.is_starter) < 5
        )
        SELECT tg.game_id, tg.team_id, tg.cnt,
               g.date, g.home_team_id, g.away_team_id,
               h.abbreviation AS home_abbr, a.abbreviation AS away_abbr
        FROM tg
        JOIN nba.games g ON g.id = tg.game_id
        JOIN nba.teams h ON h.id = g.home_team_id
        JOIN nba.teams a ON a.id = g.away_team_id
        ORDER BY tg.cnt ASC, g.date ASC
        LIMIT :limit
    """)
    return db_conn.execute(q, {"limit": limit}).fetchall()


def _boxscore_players(db_conn, game_id, team_id):
    """player_ids in our pgs boxscore for this exact (game, team).
    Authoritative 'truly played this game' set used to gate matches."""
    return {r[0] for r in db_conn.execute(text(
        "SELECT DISTINCT player_id FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t"
    ), {"g": game_id, "t": team_id}).fetchall()}


def _resolve_br_to_pgs(db_conn, name, team_id, game_id):
    """Match a bball-ref starter name to a player_id who already has a pgs boxscore
    row for this exact game. NO auto-create. Returns player_id or None."""
    key = (name or "").strip().lower()
    if not key:
        return None
    row = db_conn.execute(text("""
        SELECT DISTINCT pg.player_id
        FROM nba.player_game_stats pg
        JOIN nba.players p ON p.id = pg.player_id
        WHERE pg.game_id = :g AND pg.team_id = :t
          AND lower(regexp_replace(p.name, '[^a-zA-Z ]', '', 'g')) = :key
        LIMIT 1
    """), {"g": game_id, "t": team_id, "key": key}).first()
    return row[0] if row else None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50, help="max short team-games to process")
    ap.add_argument("--commit", action="store_true", help="apply writes (default = dry-run)")
    args = ap.parse_args(argv)

    engine = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)
    if not args.commit:
        logger.info("DRY-RUN: no writes will be applied. Pass --commit to apply.")
    mode = "COMMIT" if args.commit else "DRY-RUN"

    changed = 0          # rows actually flipped FALSE->TRUE
    found = 0            # starters matched (already-true + newly-set)
    skipped_noact = 0    # bball starter not present in active_players for that game
    skipped_nopgs = 0    # bball starter matched but no pgs boxscore row (REJECTED)
    fetch_fail = 0
    games_done = 0

    conn = engine.connect()
    try:
        targets = short_team_games(conn, args.limit)
        logger.info(f"[{mode}] {len(targets)} short team-games targeted")
        games_needed = {gid: (date, hid, aid, hab, aab)
                        for gid, tid, cnt, date, hid, aid, hab, aab in targets}
        for gid, (date, hid, aid, hab, aab) in games_needed.items():
            games_done += 1
            ds, year = _et_date(date)
            html = None
            for br_home in _br_abbr_candidates(hab, year):
                try:
                    html = _fetch(f"{BR}{ds}0{br_home}.html")
                    break
                except Exception as e:
                    logger.warning(f"  game {gid} fetch: {e}")
            if html is None:
                fetch_fail += 1
                logger.warning(f"  game {gid}: could not fetch bball-ref page")
                continue
            for m in re.finditer(r'<table[^>]*id="box-(\w+)-game-basic"(.*?)</table>', html, re.S):
                br_abbr = m.group(1)
                our_abbr = _norm_abbr(br_abbr)
                team_id = hid if our_abbr == _norm_abbr(hab) else (aid if our_abbr == _norm_abbr(aab) else None)
                if team_id is None:
                    logger.warning(f"  game {gid}: unhandled BR abbr '{br_abbr}' (home={hab} away={aab})")
                    continue
                rows = _parse_player_stats(m.group(2))
                if len(rows) < 5:
                    logger.warning(f"  game {gid} team {team_id}: only {len(rows)} rows parsed")
                    continue
                starters = rows[:5]
                active_map = {r[0]: r[1] for r in conn.execute(text(
                    "SELECT player_id, is_starter FROM nba.active_players WHERE game_id=:g AND team_id=:t"
                ), {"g": gid, "t": team_id}).fetchall()}
                if not active_map:
                    logger.info(f"  game {gid} team {team_id}: no active_players rows at all (ACTIVE-ROSTER GAP)")
                boxset = _boxscore_players(conn, gid, team_id)
                for pname, _stats in starters:
                    pid = _resolve_br_to_pgs(conn, pname, team_id, gid)
                    if pid is None:
                        skipped_nopgs += 1
                        logger.info(f"  game {gid} team {team_id} '{pname}': no pgs boxscore match (REJECT)")
                        continue
                    if pid not in active_map:
                        skipped_noact += 1
                        logger.info(f"  game {gid} team {team_id} '{pname}'->{pid}: not in active_players (REJECT)")
                        continue
                    if pid not in boxset:
                        skipped_nopgs += 1
                        logger.info(f"  game {gid} team {team_id} '{pname}'->{pid}: pgs matched but not in boxset (REJECT)")
                        continue
                    found += 1
                    if not active_map[pid] and args.commit:
                        conn.execute(text(
                            "UPDATE nba.active_players SET is_starter=TRUE WHERE game_id=:g AND team_id=:t AND player_id=:p"
                        ), {"g": gid, "t": team_id, "p": pid})
                        changed += 1
                    elif not active_map[pid]:
                        changed += 1  # would flip in commit mode
            time.sleep(1.0)  # polite throttle to bball-ref
    finally:
        if args.commit:
            conn.commit()
            logger.info("COMMITTED all changes.")
        else:
            conn.rollback()
            logger.info("DRY-RUN: rolled back (no writes applied).")
        conn.close()

    logger.info("=" * 60)
    logger.info(f"[{mode}] SUMMARY")
    logger.info(f"  games fetched       : {games_done}")
    logger.info(f"  fetch failures      : {fetch_fail}")
    logger.info(f"  starters found      : {found}")
    logger.info(f"  rows flipped (or would flip): {changed}")
    logger.info(f"  rejected/no-boxscore: {skipped_nopgs}")
    logger.info(f"  rejected/no-active  : {skipped_noact}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main(sys.argv[1:])
