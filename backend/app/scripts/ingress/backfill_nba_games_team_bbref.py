"""Fill nba.games team box-score stats for games ESPN's core API lacks.

ESPN does not have team-statistics payloads for a slice of older NBA games
(~12% of season 26, growing for older seasons). Basketball-Reference HAS
complete box scores for every game. We reuse the existing BBRef parser
(backfill_nba_pgs_bbref) to fetch the box score page, sum each team's
per-player counting stats (including REAL offensive/defensive rebounds), and
write the team totals into nba.games.

This gives the cumulative/rolling builders a real offensive-rebound number for
these games instead of the reb*0.245 proxy, so the Dean-Oliver possession
fallback is exact rather than approximate.

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/ingress/backfill_nba_games_team_bbref.py [--season 26] [--limit N]
"""
import logging
import os
import re
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL

from app.scripts.ingress.backfill_nba_pgs_bbref import (
    _fetch,
    _parse_player_stats,
    _br_abbr_candidates,
    _norm_abbr,
    _to_int,
    _team_id_map,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("nba-team-bbref")

# nba.games column suffix (no home_/away_ prefix) -> stat keys summed from players
COL_SUMS = {
    "offensive_rebounds": ["ORB"],
    "defensive_rebounds": ["DRB"],
    "rebounds": ["TRB"],
    "field_goals_made": ["FG"],
    "field_goals_attempted": ["FGA"],
    "three_points_made": ["3P"],
    "three_points_attempted": ["3PA"],
    "free_throws_made": ["FT"],
    "free_throws_attempted": ["FTA"],
    "assists": ["AST"],
    "steals": ["STL"],
    "blocks": ["BLK"],
    "turnovers": ["TOV"],
    "fouls": ["PF"],
}
COL_LIST = list(COL_SUMS.keys())


def _sum_team_stats(player_rows):
    """Sum per-player stat dicts into a {col: total} aggregate (per COL_SUMS)."""
    totals = {c: 0 for c in COL_LIST}
    for _name, s in player_rows:
        for col, keys in COL_SUMS.items():
            for k in keys:
                v = _to_int(s.get(k))
                if v is not None:
                    totals[col] += v
    return totals


def _local_date_str(game_date):
    """BR URLs use US Eastern date; our stored date may be UTC +1 day ahead."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        if hasattr(game_date, "tzinfo"):
            local = game_date.astimezone(ZoneInfo("America/New_York"))
        else:
            local = datetime.fromisoformat(str(game_date)).replace(
                tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        return local.strftime("%Y%m%d"), local.year
    except Exception:
        return str(game_date)[:10].replace("-", ""), int(str(game_date)[:4])


def _fetch_team(html, br_abbr, team_id_map):
    """Sum the player table for one BR abbreviation -> (our_abbr, {col: total})."""
    m = re.search(r'<table[^>]*id="box-%s-game-basic"(.*?)</table>' % re.escape(br_abbr),
                  html, re.S)
    if not m:
        return None
    our = _norm_abbr(br_abbr)
    if our not in team_id_map:
        return None
    rows = _parse_player_stats(m.group(1))
    return our, _sum_team_stats(rows)


def _process_game(db_conn, db_game_id, game_date, home_abbr, away_abbr, team_id_map):
    ds, year = _local_date_str(game_date)
    html = None
    # Try home team abbr first, then away team abbr as a fallback (BR sometimes
    # keys a boxscore on a different team variant for an odd fixture).
    tried = []
    for home_ab in (home_abbr, away_abbr):
        for br_home in _br_abbr_candidates(home_ab, year):
            url = f"https://www.basketball-reference.com/boxscores/{ds}0{br_home}.html"
            tried.append(url)
            try:
                html = _fetch(url)
                break
            except Exception as e:
                logger.debug(f"  game {db_game_id} fetch {url}: {e}")
        if html is not None:
            break
    if html is None:
        logger.warning(f"  game {db_game_id}: no BBRef boxscore via {len(tried)} URL(s)")
        return 0

    updated = 0
    # Both team tables on the page
    for m in re.finditer(r'<table[^>]*id="box-(\w+)-game-basic"(.*?)</table>', html, re.S):
        br_abbr = m.group(1)
        our = _norm_abbr(br_abbr)
        if our not in team_id_map:
            continue
        totals = _sum_team_stats(_parse_player_stats(m.group(2)))
        # determine side (home vs away) by abbreviation
        side = "home" if our == home_abbr else "away" if our == away_abbr else None
        if side is None:
            continue
        sql_cols = []
        params = {"gid": db_game_id}
        for col in COL_LIST:
            sql_cols.append(f"{side}_{col} = :{col}")
            params[col] = totals[col]
        db_conn.execute(
            text(f"UPDATE nba.games SET {', '.join(sql_cols)} WHERE id = :gid"),
            params,
        )
        updated += 1
    return updated


def _gap_games(db_conn, seasons, limit):
    q = """
        SELECT g.id, g.nba_game_id, g.date, th.abbreviation, ta.abbreviation
        FROM nba.games g
        JOIN nba.teams th ON th.id = g.home_team_id
        JOIN nba.teams ta ON ta.id = g.away_team_id
        WHERE g.nba_game_id IS NOT NULL
          AND g.game_type IN ('REG','POST','PLAYIN')
          AND g.status = 'FINAL'
          AND g.home_estimated_possessions IS NULL
          AND (g.home_offensive_rebounds IS NULL OR g.away_offensive_rebounds IS NULL)
    """
    params = {}
    if seasons:
        q += f" AND g.season_id IN ({', '.join(str(s) for s in seasons)})"
    q += " ORDER BY g.date DESC"
    if limit:
        q += " LIMIT :l"
        params["l"] = limit
    return db_conn.execute(text(q), params).fetchall()


def _parse_args(argv):
    seasons = []
    limit = 0
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--season" and i + 1 < len(argv):
            seasons.append(int(argv[i + 1])); i += 1
        elif a.startswith("--season="):
            seasons.append(int(a.split("=", 1)[1]))
        elif a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1]); i += 1
        elif a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        i += 1
    return seasons, limit


def main(argv):
    seasons, limit = _parse_args(argv)

    engine = create_engine(PSYCOPG2_DATABASE_URL.replace("+asyncpg", "+psycopg2"))
    with engine.connect() as conn:
        team_id_map = _team_id_map(conn)
        games = _gap_games(conn, seasons or None, limit)
        logger.info(f"{len(games)} games need team box-score from BBRef (seasons={seasons or 'all'})")
        total = 0
        errors = 0
        for idx, (db_gid, _espn, gdate, hab, aab) in enumerate(games, 1):
            try:
                n = _process_game(conn, db_gid, gdate, hab, aab, team_id_map)
            except Exception as e:
                n = 0
                logger.warning(f"  game {db_gid} failed: {e}")
            total += n
            if n == 0:
                errors += 1
            if idx % 10 == 0 or idx == len(games):
                conn.commit()
                logger.info(f"  {idx}/{len(games)} games, {total} teams updated, {errors} empty")
            time.sleep(8)  # BBRef polite rate
        conn.commit()
        logger.info(f"DONE: {total} teams updated, {errors} games with no data")
        return {"games": len(games), "teams": total, "errors": errors}


if __name__ == "__main__":
    main(sys.argv[1:])
