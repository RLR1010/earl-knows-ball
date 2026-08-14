"""Backfill nba.player_game_stats for OLD games using the NBA Stats API.

The current ingestor (nba_player_game_stats.py) uses ESPN endpoints that 404 or
return placeholder zeros for pre-2016 games. NBA Stats API
(boxscoretraditionalv2) returns complete, accurate per-player boxscores for all
historical games.

This loader:
  1. Fetches `leaguegamelog` once per season -> (date, home/away team) -> GAME_ID map.
  2. For each still-incomplete game, looks up its GAME_ID and fetches
     `boxscoretraditionalv2`, parsing per-player stats.
  3. Matches PLAYER_NAME to `nba.players` (auto-creating a row when missing, so
     historical players land in the players table too), then inserts into
     `nba.player_game_stats` (same ON CONFLICT semantics as the normal ingest).

Minutes are converted from "MM:SS" to integer minutes to match the schema.

Usage:
  cd <repo>/backend && PYTHONPATH=$PWD <venv>/bin/python \
      app/scripts/ingress/backfill_nba_pgs_statsapi.py [year ...] [--limit N]
  No args: all still-incomplete FINAL REG/POST games for years 2006..2018.
"""
import logging
import sys
import time
import urllib.parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.nba_pgs_statsapi")

REPO = "/home/rich/.openclaw/workspace/earl-knows-football"
sys.path.insert(0, f"{REPO}/backend")

from sqlalchemy import create_engine, text

try:
    from app.db_urls import PSYCOPG2_DATABASE_URL as DSN
except Exception:
    DSN = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

import logging
import sys
import time
import urllib.parse

import requests

NP = "https://stats.nba.com"
HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Referer": "https://www.nba.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}


class _Retrier:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_json(self, url, timeout=90, tries=4):
        last = None
        for attempt in range(1, tries + 1):
            try:
                r = requests.get(url, headers=HDRS, timeout=timeout)
                if r.status_code == 429:
                    wait = min(20 * attempt, 60)
                    logger.info(f"  429 rate-limited; sleeping {wait}s (attempt {attempt})")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e
                logger.warning(f"  request attempt {attempt} failed: {e}")
                if attempt < tries:
                    time.sleep(3 * attempt)
        raise last


def _stats_get(client, path, params):
    url = f"{NP}{path}?" + urllib.parse.urlencode(params)
    return client.get_json(url)


def _leaguegamelog(client, season):
    d = _stats_get(client, "/stats/leaguegamelog", {
        "LeagueID": "00", "Season": season,
        "SeasonType": "Regular Season", "Counter": 10000,
    })
    rs = d["resultSets"][0]
    hd = rs["headers"]; rows = rs["rowSet"]
    i = {h: idx for idx, h in enumerate(hd)}
    # game -> set of teams; date per game
    gm = {}
    for r in rows:
        gid = r[i["GAME_ID"]]
        gm.setdefault(gid, {"date": r[i["GAME_DATE"]], "teams": set()})
        gm[gid]["teams"].add(r[i["TEAM_ABBREVIATION"]])
    return gm  # gid -> {date, teams}


def _incomplete_games(db_conn, year, limit):
    q = text("""
        SELECT g.id, g.nba_game_id, g.date, h.abbreviation, a.abbreviation,
               g.home_team_id, g.away_team_id
        FROM nba.games g
        JOIN nba.seasons s ON s.id = g.season_id
        JOIN nba.teams h ON h.id = g.home_team_id
        JOIN nba.teams a ON a.id = g.away_team_id
        WHERE s.year = :year AND g.game_type IN ('REG','POST')
          AND g.status::text = 'FINAL'
          AND (
            NOT EXISTS (SELECT 1 FROM nba.player_game_stats ph
                        WHERE ph.game_id = g.id AND ph.team_id = g.home_team_id)
            OR NOT EXISTS (SELECT 1 FROM nba.player_game_stats pa
                           WHERE pa.game_id = g.id AND pa.team_id = g.away_team_id)
          )
        ORDER BY g.date
    """)
    rows = db_conn.execute(q, {"year": year}).fetchall()
    return rows[:limit] if limit else rows


def _min_to_int(m):
    s = (m or "").strip()
    if not s or s in ("--", "-"):
        return None
    try:
        if ":" in s:
            mm, ss = s.split(":")
            return int(mm) + (1 if int(ss) >= 30 else 0)
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _to_int(v):
    s = ("" if v is None else str(v)).strip()
    if s in ("", "--", "-", "None", "null"):
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _to_float(v):
    s = ("" if v is None else str(v)).strip()
    if s in ("", "--", "-", "None", "null"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _build_players_by_name(db_conn):
    rows = db_conn.execute(text(
        "SELECT id, name, espn_id FROM nba.players WHERE name IS NOT NULL"
    )).fetchall()
    by_name = {}
    by_espn = {}
    for pid, name, espn in rows:
        key = (name or "").strip().lower()
        by_name.setdefault(key, pid)
        if espn:
            by_espn[int(espn)] = pid
    return by_name, by_espn


def _resolve_player(db_conn, name, team_id, by_name):
    """Find or create nba.players row for `name` (NBA Stats spelling)."""
    key = (name or "").strip().lower()
    if not key:
        return None
    pid = by_name.get(key)
    if pid:
        return pid
    ins = db_conn.execute(text("""
        INSERT INTO nba.players (name, position, team_id, active)
        VALUES (:n, 'F', :t, 0) RETURNING id
    """), {"n": name.strip(), "t": team_id})
    row = ins.fetchone()
    if row:
        pid = row[0]
        by_name[key] = pid
        logger.info(f"  auto-created player id={pid} '{name.strip()}'")
    return pid


def _team_id_map(db_conn):
    rows = db_conn.execute(text("SELECT id, abbreviation, name FROM nba.teams")).fetchall()
    m = {}
    for tid, abbr, name in rows:
        if abbr:
            m[abbr.upper()] = tid
        if name:
            m[name.strip()] = tid
    return m


def _fetch_and_insert(client, db_conn, db_game_id, nba_gid, team_id_map, by_name, by_espn):
    d = _stats_get(client, "/stats/boxscoretraditionalv2", {
        "GameID": nba_gid, "StartPeriod": 1, "EndPeriod": 10,
    })
    rs = d["resultSets"][0]
    hd = rs["headers"]; rows = rs["rowSet"]
    i = {h: idx for idx, h in enumerate(hd)}
    inserted = 0
    for r in rows:
        team_abbr = (r[i["TEAM_ABBREVIATION"]] or "").upper()
        team_id = team_id_map.get(team_abbr)
        if not team_id:
            continue
        pname = r[i["PLAYER_NAME"]]
        pid = _resolve_player(db_conn, pname, team_id, by_name)
        if not pid:
            continue
        st = r[i["START_POSITION"]] or ""
        fgm = _to_int(r[i["FGM"]]); fga = _to_int(r[i["FGA"]])
        tpm = _to_int(r[i["FG3M"]]); tpa = _to_int(r[i["FG3A"]])
        ftm = _to_int(r[i["FTM"]]); fta = _to_int(r[i["FTA"]])
        def _pct3(made, att):
            if made is None or not att:
                return _to_float(r[i["FG3_PCT"]]) if made else None
            return None
        # fields for pct stored as decimals already from API
        fgp = _to_float(r[i["FG_PCT"]])
        tpp = _to_float(r[i["FG3_PCT"]])
        ftp = _to_float(r[i["FT_PCT"]])
        db_conn.execute(text("""
            INSERT INTO nba.player_game_stats
                (game_id, player_id, team_id, nba_game_id, nba_player_id,
                 minutes, field_goals_made, field_goals_attempted, field_goal_pct,
                 three_pointers_made, three_pointers_attempted, three_pointer_pct,
                 free_throws_made, free_throws_attempted, free_throw_pct,
                 rebounds_offensive, rebounds_defensive, rebounds_total,
                 assists, steals, blocks, turnovers, fouls_personal,
                 points, plus_minus)
            VALUES
                (:game_id, :player_id, :team_id, :nba_game_id, :nba_player_id,
                 :min, :fgm, :fga, :fgp, :tpm, :tpa, :tpp, :ftm, :fta, :ftp,
                 :oreb, :dreb, :treb, :ast, :stl, :blk, :tov, :pf,
                 :pts, :pm)
            ON CONFLICT (game_id, player_id) DO NOTHING
        """), {
            "game_id": db_game_id,
            "player_id": pid,
            "team_id": team_id,
            "nba_game_id": nba_gid,
            "nba_player_id": _to_int(r[i["PLAYER_ID"]]),
            "min": _min_to_int(r[i["MIN"]]),
            "fgm": fgm, "fga": fga, "fgp": fgp,
            "tpm": tpm, "tpa": tpa, "tpp": tpp,
            "ftm": ftm, "fta": fta, "ftp": ftp,
            "oreb": _to_int(r[i["OREB"]]), "dreb": _to_int(r[i["DREB"]]),
            "treb": _to_int(r[i["REB"]]),
            "ast": _to_int(r[i["AST"]]), "stl": _to_int(r[i["STL"]]),
            "blk": _to_int(r[i["BLK"]]), "tov": _to_int(r[i["TO"]]),
            "pf": _to_int(r[i["PF"]]), "pts": _to_int(r[i["PTS"]]),
            "pm": _to_float(r[i["PLUS_MINUS"]]),
        })
        inserted += 1
    return inserted


def backfill_year(year, limit, engine):
    started = time.time()
    season = f"{year}-{str(year + 1)[2:]}"
    import time as _t
    with engine.connect() as db_conn:
        team_id_map = _team_id_map(db_conn)
        by_name, by_espn = _build_players_by_name(db_conn)
        games = _incomplete_games(db_conn, year, limit)
        logger.info(f"[{year}] ({season}) {len(games)} incomplete games")
        if not games:
            return {"year": year, "games": 0, "rows": 0, "still": 0}
        with _Retrier() as client:
            # build game id map
            gm = _leaguegamelog(client, season)
            logger.info(f"  season game-map size: {len(gm)}")
            by_date = {}
            for gid, info in gm.items():
                by_date.setdefault(info["date"], {})[gid] = info["teams"]
            total = 0; errors = 0
            for idx, (db_gid, _espn, game_date, hab, aab, hid, aid) in enumerate(games, 1):
                ds = game_date.strftime("%Y-%m-%d") if hasattr(game_date, "strftime") else str(game_date)[:10]
                cand = by_date.get(ds) or {}
                nba_gid = None
                target = {hab.upper(), aab.upper()}
                for gid, teams in cand.items():
                    if teams == target:
                        nba_gid = gid; break
                if not nba_gid:
                    for gid, teams in cand.items():
                        if teams >= target:
                            nba_gid = gid; break
                if not nba_gid:
                    errors += 1
                    logger.warning(f"  game {db_gid} {ds} {aab}@{hab}: no NBA game id in map")
                    continue
                try:
                    n = _fetch_and_insert(client, db_conn, db_gid, nba_gid,
                                          team_id_map, by_name, by_espn)
                except Exception as e:
                    n = 0
                    logger.warning(f"  game {db_gid} ({nba_gid}) boxscore failed: {e}")
                if n == 0:
                    errors += 1
                total += n
                if idx % 20 == 0 or idx == len(games):
                    db_conn.commit()
                    logger.info(f"  [{year}] {idx}/{len(games)} games, {total} rows, {errors} missing/no-id")
                _t.sleep(0.4)
            db_conn.commit()
            still = len(_incomplete_games(db_conn, year, 0))
            logger.info(f"[{year}] DONE: {total} rows, {errors} issues, {still} still incomplete, {time.time()-started:.0f}s")
            return {"year": year, "games": len(games), "rows": total, "errors": errors, "still": still}


def main(argv):
    limit = 0; years = []
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
        years = list(range(2006, 2019))
    years = sorted({y for y in years if 1980 <= y <= 2026})
    logger.info(f"NBA-Stats backfill years={years} limit={limit or 'none'}")
    engine = create_engine(DSN)
    summary = []
    for year in years:
        try:
            summary.append(backfill_year(year, limit, engine))
        except Exception as e:
            logger.exception(f"[{year}] FAILED: {e}")
            summary.append({"year": year, "error": str(e)})
    engine.dispose()
    logger.info("=== NBA-STATS BACKFILL RESULT ===")
    for s in summary:
        logger.info(s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
