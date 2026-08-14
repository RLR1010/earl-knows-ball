"""Backfill nba.player_game_stats for OLD games using Basketball-Reference.

ESPN's public API returns 404/placeholder-zero boxscores for pre-2016 games and
the NBA Stats API rate-limits this host, so Basketball-Reference (complete,
accurate, static HTML boxscores, not blocked) is the reliable source for the
remaining historical gap games.

Parsing: each game page has tables id='box-{ABBR}-game-basic'. Stat columns:
MP, FG, FGA, FG%, 3P, 3PA, 3P%, FT, FTA, FT%, ORB, DRB, TRB, AST, STL, BLK,
TOV, PF, PTS, GmSc, +/-. Player rows: name in the first <th>, then the stat
<td>s. DNP players are not listed (they played 0 minutes by definition).

Usage:
  cd <repo>/backend && PYTHONPATH=$PWD <venv>/bin/python \
      app/scripts/ingress/backfill_nba_pgs_bbref.py [year ...] [--limit N]
  No args: all still-incomplete FINAL REG/POST games for years 2006..2018.
"""
import logging
import re
import sys
import time
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.nba_pgs_bbref")

REPO = "/home/rich/.openclaw/workspace/earl-knows-football"
sys.path.insert(0, f"{REPO}/backend")

from sqlalchemy import create_engine, text

from app.db_urls import PSYCOPG2_DATABASE_URL as DSN

BR = "https://www.basketball-reference.com/boxscores/"

# Basketball-Reference NBA team abbreviation -> our nba.teams abbreviation.
# Our nba.teams uses current-ish names; BR uses historical team abbrevs per season.
BR_TEAM_MAP = {
    # historical relocations/renames
    "NOH": "NOP", "NOK": "NOP",  # New Orleans Hornets came back as Pelicans
    "NJN": "BKN", "NJ": "BKN",   # New Jersey Nets -> Brooklyn
    "CHA": "CHA", "CHH": "CHA", "CHO": "CHA",  # Charlotte (Bobcats/Hornets eras)
    "SEA": "OKC",                # Seattle SuperSonics -> OKC
    "PHO": "PHX",                # Phoenix (BR uses PHO)
    "BRK": "BKN",                # Brooklyn (BR uses BRK)
    "WSB": "WAS", "WAS": "WAS", "WSH": "WAS",
    "VAN": "MEM",                # Vancouver -> Memphis
    "CHH": "CHA",
}
# canonical BR abbrev -> our abbreviation (same for most)
CANON = {
    "ATL": "ATL","BOS":"BOS","BKN":"BKN","BRK":"BKN","CHA":"CHA","CHI":"CHI",
    "CLE":"CLE","DAL":"DAL","DEN":"DEN","DET":"DET","GSW":"GSW","GS":"GSW",
    "HOU":"HOU","IND":"IND","LAC":"LAC","LAL":"LAL","MEM":"MEM","MIA":"MIA",
    "MIL":"MIL","MIN":"MIN","NOP":"NOP","NYK":"NYK","OKC":"OKC","ORL":"ORL",
    "PHI":"PHI","PHX":"PHX","POR":"POR","SAC":"SAC","SAS":"SAS","TOR":"TOR",
    "UTA":"UTA","WAS":"WAS",
}


def _br_home_abbr(our_abbr, year):
    """Our DB team abbreviation -> Basketball-Reference home abbreviation.
    BR uses its own naming: Phoenix='PHO' (not PHX), Brooklyn='BRK' (not BKN),
    and the Hornets were 'NOH' until the mid-2013-14 Pelicans rename."""
    a = (our_abbr or "").upper()
    if a == "NOP":
        return "NOH" if year <= 2013 else "NOP"
    return {
        "PHX": "PHO",
        "BKN": "BRK",
        "NYK": "NYK", "OKC": "OKC", "WAS": "WAS", "GSW": "GSW",
    }.get(a, a)


def _br_abbr_candidates(our_abbr, year):
    """All plausible BR home abbreviations for a team (try in order on 404)."""
    a = (our_abbr or "").upper()
    primary = _br_home_abbr(a, year)
    pool = {primary}
    if a in ("PHX", "PHO"):
        pool.update({"PHO", "PHX"})
    if a in ("BKN", "BRK"):
        pool.update({"BRK", "BKN"})
    if a == "NOP":
        pool.update({"NOH", "NOP"})  # Hornets->Pelicans mid-2013-14
    if a in ("CHA", "CHH", "CHO"):
        pool.update({"CHA", "CHH", "CHO"})  # Bobcats(CHA)/Hornets(CHH,CHO) eras
    return list(pool)


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


def _min_to_int(m):
    s = ("" if m is None else str(m)).strip()
    if not s or s in ("--", "-"):
        return None
    try:
        if ":" in s:
            mm, ss = s.split(":")
            return int(mm) + (1 if int(ss) >= 30 else 0)
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "ignore")


def _get_url(year, gid):
    """gid is nba_game_id (ESPN). BR URL is by date+home-abbr. We look up via
    the game's date. Build URL from our DB: date YYYYMMDD + home BR abbrev.
    Return url if determinable; caller passes date+home away abbrevs."""
    # handled in parse path
    raise NotImplementedError


STAT_KEYS = ["MP", "FG", "FGA", "FG%", "3P", "3PA", "3P%", "FT", "FTA", "FT%",
             "ORB", "DRB", "TRB", "AST", "STL", "BLK", "TOV", "PF", "PTS", "GmSc", "+/-"]


# start_pos order we won't easily derive; start position captured where present
def _parse_player_stats(table_html):
    """Extract list of (name, start_position|None, stats dict) from a game-basic table."""
    out = []
    tbody_re = re.findall(r"<tbody>(.*?)</tbody>", table_html, re.S)
    for tbody in tbody_re:
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbody, re.S):
            name = None
            m = re.search(r"<th[^>]*scope=.row.[^>]*>(.*?)</th>", tr, re.S)
            if not m:
                m = re.search(r"<th[^>]*>(.*?)</th>", tr, re.S)
            if m:
                name = re.sub("<[^>]+>", "", m.group(1)).strip()
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if not name:
                continue
            vals = [re.sub("<[^>]+>", "", t).strip() for t in tds]
            if len(vals) < len(STAT_KEYS):
                continue
            s = dict(zip(STAT_KEYS, vals[: len(STAT_KEYS)]))
            out.append((name, s))
    return out


def _incomplete_games(db_conn, year, limit):
    q = text("""
        SELECT g.id, g.nba_game_id, g.date, h.abbreviation, a.abbreviation
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


def _team_id_map(db_conn):
    rows = db_conn.execute(text("SELECT id, abbreviation, name FROM nba.teams")).fetchall()
    m = {}
    for tid, abbr, name in rows:
        if abbr:
            m[abbr.upper()] = tid
        if name:
            m[name.strip().lower()] = tid
    return m


def _resolve_player(db_conn, name, team_id, by_name):
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


def _norm_abbr(br_abbr):
    br_abbr = (br_abbr or "").upper()
    return BR_TEAM_MAP.get(br_abbr, CANON.get(br_abbr, br_abbr))


def _process_game(db_conn, db_game_id, game_date, home_abbr, away_abbr, team_id_map, by_name):
    """Fetch BR boxscore for a game by date + home abbrev, parse both teams, insert."""
    # BR URLs use US Eastern game date. Our stored date is UTC and ~1 day ahead
    # of the US date for evening games, so convert UTC -> America/New_York.
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        if hasattr(game_date, "tzinfo"):
            local = game_date.astimezone(ZoneInfo("America/New_York"))
        else:
            local = datetime.fromisoformat(str(game_date)).replace(
                tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
        ds = local.strftime("%Y%m%d")
        year = local.year
    except Exception:
        if hasattr(game_date, "strftime"):
            ds = game_date.strftime("%Y%m%d")
            year = game_date.year
        else:
            ds = str(game_date)[:10].replace("-", "")
            year = int(ds[:4])
    html = None
    for br_home in _br_abbr_candidates(home_abbr, year):
        url = f"{BR}{ds}0{br_home}.html"
        try:
            html = _fetch(url)
            break
        except Exception as e:
            logger.warning(f"  game {db_game_id} fetch {url}: {e}")
    if html is None:
        return 0
    inserted = 0
    # Find every team boxscore table on the page (box-{ABBR}-game-basic) -
    # there are exactly two (home + away). Map each BR abbrev to our team.
    for m in re.finditer(r'<table[^>]*id="box-(\w+)-game-basic"(.*?)</table>', html, re.S):
        br_abbr = m.group(1)
        our_abbr = _norm_abbr(br_abbr)
        team_id = team_id_map.get(our_abbr)
        if not team_id:
            logger.warning(f"    skip table for BR abbr '{br_abbr}' -> our '{our_abbr}' (no team id)")
            continue
        rows = _parse_player_stats(m.group(2))
        for pname, s in rows:
            pid = _resolve_player(db_conn, pname, team_id, by_name)
            if not pid:
                continue
            fgm, fga = _to_int(s.get("FG")), _to_int(s.get("FGA"))
            tpm, tpa = _to_int(s.get("3P")), _to_int(s.get("3PA"))
            ftm, fta = _to_int(s.get("FT")), _to_int(s.get("FTA"))
            try:
                db_conn.execute(text("""
                    INSERT INTO nba.player_game_stats
                        (game_id, player_id, team_id,
                         minutes, field_goals_made, field_goals_attempted, field_goal_pct,
                         three_pointers_made, three_pointers_attempted, three_pointer_pct,
                         free_throws_made, free_throws_attempted, free_throw_pct,
                         rebounds_offensive, rebounds_defensive, rebounds_total,
                         assists, steals, blocks, turnovers, fouls_personal,
                         points, plus_minus)
                    VALUES
                        (:game_id, :player_id, :team_id,
                         :min, :fgm, :fga, :fgp, :tpm, :tpa, :tpp, :ftm, :fta, :ftp,
                         :oreb, :dreb, :treb, :ast, :stl, :blk, :tov, :pf, :pts, :pm)
                    ON CONFLICT (game_id, player_id) DO NOTHING
                """), {
                    "game_id": db_game_id, "player_id": pid, "team_id": team_id,
                    "min": _min_to_int(s.get("MP")),
                    "fgm": fgm, "fga": fga, "fgp": _to_float(s.get("FG%")),
                    "tpm": tpm, "tpa": tpa, "tpp": _to_float(s.get("3P%")),
                    "ftm": ftm, "fta": fta, "ftp": _to_float(s.get("FT%")),
                    "oreb": _to_int(s.get("ORB")), "dreb": _to_int(s.get("DRB")),
                    "treb": _to_int(s.get("TRB")),
                    "ast": _to_int(s.get("AST")), "stl": _to_int(s.get("STL")),
                    "blk": _to_int(s.get("BLK")), "tov": _to_int(s.get("TOV")),
                    "pf": _to_int(s.get("PF")), "pts": _to_int(s.get("PTS")),
                    "pm": _to_int(s.get("+/-")),
                })
                inserted += 1
            except Exception as e:
                logger.warning(f"  insert err game {db_game_id} {pname}: {e}")
    return inserted


def backfill_year(year, limit, engine):
    started = time.time()
    with engine.connect() as db_conn:
        team_id_map = _team_id_map(db_conn)
        by_name = {}
        for pid, name in db_conn.execute(text("SELECT id, name FROM nba.players WHERE name IS NOT NULL")):
            by_name[(name or "").strip().lower()] = pid
        games = _incomplete_games(db_conn, year, limit)
        logger.info(f"[{year}] {len(games)} incomplete games")
        if not games:
            return {"year": year, "games": 0, "rows": 0, "still": 0}
        total = 0; errors = 0
        for idx, (db_gid, _espn, gdate, hab, aab) in enumerate(games, 1):
            try:
                n = _process_game(db_conn, db_gid, gdate, hab, aab, team_id_map, by_name)
            except Exception as e:
                n = 0
                logger.warning(f"  game {db_gid} failed: {e}")
            if n == 0:
                errors += 1
            total += n
            # BR polite rate: ~6 req/min
            if idx % 10 == 0 or idx == len(games):
                db_conn.commit()
                logger.info(f"  [{year}] {idx}/{len(games)} games, {total} rows, {errors} empty")
            time.sleep(9)
        db_conn.commit()
        still = len(_incomplete_games(db_conn, year, 0))
        logger.info(f"[{year}] DONE: {total} rows, {errors} empty, {still} still incomplete, {time.time()-started:.0f}s")
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
    logger.info(f"Basketball-Reference backfill years={years} limit={limit or 'none'}")
    engine = create_engine(DSN)
    summary = []
    for year in years:
        try:
            summary.append(backfill_year(year, limit, engine))
        except Exception as e:
            logger.exception(f"[{year}] FAILED: {e}")
            summary.append({"year": year, "error": str(e)})
    engine.dispose()
    logger.info("=== BBREF BACKFILL RESULT ===")
    for s in summary:
        logger.info(s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
