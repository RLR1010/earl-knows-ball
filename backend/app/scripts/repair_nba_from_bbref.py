"""Repair 4 NBA games where ESPN's summary/CORE returns all-zero boxscore stats,
using Basketball-Reference's authoritative boxscore as the source.

Games (ESPN data gap, confirmed):
  sid26 15577 NYK@NOP 2016-12-30  (final NYK 92)   - short 2 (missing Hernangomez)
  sid26 15651 NYK@NOP 2017-01-09  (final NYK 96)   - short 6
  sid26 16245 NYK@CHI 2017-04-05  (final NYK 100)  - short 8
  sid27 17623 CHA@CHI 2018-04-04  (final CHA 114)  - short 11

Source: basketball-reference.com/boxscores/<YYYMMDD><TMM>.html
(Note: URLs use the LOCAL/ET game date, one day before the UTC DB timestamp for
 8pm-ET games.)

For each targeted team, parse the team's basic boxscore (per-player points &
minutes) with pandas.read_html and authoritatively replace the game+team's pgs
rows so the boxscore sums EXACTLY to the official final score.

Players are matched to our nba.players by normalized (accent-stripped, suffix-
stripped) name; unmatched names are auto-created as minimal rows.

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/repair_nba_from_bbref.py
  flags: --dry-run  (log only, no writes)
"""
import argparse
import logging
import os
import sys
import time
import unicodedata
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

import requests
from sqlalchemy import create_engine, text

from app.db_urls import SYNC_DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("nba-bbref-repair")

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# (nba.games.id, bbref boxscore url [ET date], db_team_id, team_abbr_for_parse, official_final)
TARGETS = [
    (15577, "https://www.basketball-reference.com/boxscores/201612300NOP.html", 16, "NYK", 92),
    (15651, "https://www.basketball-reference.com/boxscores/201701090NYK.html", 16, "NYK", 96),
    (16245, "https://www.basketball-reference.com/boxscores/201704040NYK.html", 16, "NYK", 100),
    (17623, "https://www.basketball-reference.com/boxscores/201804030CHI.html", 30, "CHA", 114),
]

FULL = {"NYK": "New York Knicks", "CHA": "Charlotte Hornets",
        "NOP": "New Orleans Pelicans", "CHI": "Chicago Bulls"}


def _nk(name):
    if not name:
        return None
    s = unicodedata.normalize("NFD", name)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    out = "".join(ch for ch in s.lower() if ch.isalnum())
    for suffix in ("sr", "jr", "iii", "ii"):
        if out.endswith(suffix):
            out = out[: -len(suffix)]
    return out


def parse_bbref_team_points(url, team_abbr):
    """Pure-regex parser for a team's BB-Ref basic boxscore.

    Returns (players, min_players, team_total). players = [(name, pts, mp)].
    Parses every player row in the page and groups them into per-team blocks
    split on the 'Starters' header (BB-Ref lists away team block first, then
    home). Returns (None,...) on failure. Requires no lxml/bs4.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        if resp.status_code != 200:
            logger.error("  fetch %s -> %s", url, resp.status_code)
            return None, 0, None
    except Exception as ex:
        logger.error("  fetch error %s: %s", url, ex)
        return None, 0, None
    txt = resp.text
    # Find all boxscore tables (each appears twice: basic + advanced). BB-Ref
    # marks basic tables with <caption ...>NAME Basic</caption>.
    basic_blocks = []
    for m in re.finditer(r'<caption>[^<]*(?:Basic)[^<]*</caption>.*?</table>', txt, re.S):
        basic_blocks.append(m.group(0))
    # If caption structure differs, fall back to any block containing Team Totals
    if not basic_blocks:
        for m in re.finditer(r'<table[^>]*>.*?Team Totals.*?</table>', txt, re.S):
            basic_blocks.append(m.group(0))
    if not basic_blocks:
        logger.warning("  no tables found for %s", team_abbr)
        return None, 0, None
    # For each basic block, extract team name from title attribute / caption
    best = None
    for block in basic_blocks:
        capm = re.search(r'<caption[^>]*>([^<]+)</caption>', block)
        cap = capm.group(1) if capm else ""
        players, total = _parse_block(block)
        kw = FULL.get(team_abbr.upper(), team_abbr).split()[-1]
        # match this block to the target team by caption OR by team total == final
        if kw.lower() in cap.lower() or team_abbr.lower() in cap.lower() or team_abbr.upper() in cap.upper():
            return players, len(players), total
        best = best or (players, total)
    # fallback: return the first block (away team is first on BB-Ref)
    if best is None and basic_blocks:
        best = _parse_block(basic_blocks[0])
    if best is None:
        return None, 0, None
    return best[0], len(best[0]), best[1]


def _parse_block(block):
    players = []
    total = None
    for row in re.finditer(r'<tr[^>]*>(?:(?!</tr>).)*?</tr>', block, re.S):
        rtext = row.group(0)
        nm = re.search(r'data-stat="player"[^>]*>\s*(?:<a[^>]*>)?([^<]+?)(?:</a>)?\s*</th>', rtext)
        if not nm:
            continue
        name = nm.group(1).strip()
        if name in ("Starters", "Reserves", "Team Totals"):
            tm = re.search(r'data-stat="pts"[^>]*>(-?\d+)<', rtext)
            if name == "Team Totals" and tm:
                total = int(tm.group(1))
            continue
        pts = re.search(r'data-stat="pts"[^>]*>(-?\d+)<', rtext)
        mp = re.search(r'data-stat="mp"[^>]*>([^<]+)<', rtext)
        players.append((name, int(pts.group(1)) if pts else 0, mp.group(1).strip() if mp else ""))
    return players, total


def load_players(engine):
    name_pid = {}
    with engine.connect() as c:
        for pid, name in c.execute(text("SELECT id, name FROM nba.players")):
            nk = _nk(name)
            if nk:
                name_pid.setdefault(nk, pid)
    return name_pid


def resolve(engine, name_pid, name, team_id):
    nk = _nk(name)
    if nk and nk in name_pid:
        return name_pid[nk]
    with engine.begin() as c:
        res = c.execute(text(
            "INSERT INTO nba.players (name, position, team_id, active) "
            "VALUES (:n,'F',:t,0) RETURNING id"),
            {"n": name, "t": team_id})
        pid = res.scalar()
    name_pid[nk] = pid
    return pid


def repair_game(engine, name_pid, game_id, url, team_id, abbr, final, dry_run):
    players, cnt, total = parse_bbref_team_points(url, abbr)
    if players is None or cnt < 5:
        logger.warning("  SKIP game=%s team=%s (bbref parse incomplete: %d players)",
                       game_id, team_id, cnt)
        return "skip"
    if total is not None and total != int(final):
        logger.warning("  INTEGRITY fail game=%s team=%s bbref_total=%s final=%d",
                       game_id, team_id, total, final)
        return "skip"
    box_sum = sum(p[1] for p in players)
    if box_sum != int(final):
        logger.warning("  INTEGRITY fail game=%s team=%s player_sum=%d final=%d",
                       game_id, team_id, box_sum, final)
        return "skip"
    if dry_run:
        logger.info("  [DRYRUN] game=%s team=%s would replace with %d players (%d pts = %s)",
                    game_id, team_id, len(players), box_sum, final)
        return "ok"
    with engine.begin() as c:
        before = c.execute(text("SELECT count(*) FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t"),
                           {"g": game_id, "t": team_id}).scalar()
        c.execute(text("DELETE FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t"),
                  {"g": game_id, "t": team_id})
    ins = 0
    for name, pts, mp in players:
        pid = resolve(engine, name_pid, name, team_id)
        try:
            with engine.begin() as c:
                c.execute(text("""
                    INSERT INTO nba.player_game_stats (player_id, game_id, team_id, points)
                    VALUES (:p,:g,:t,:pts)
                """), {"p": pid, "g": game_id, "t": team_id, "pts": pts})
            ins += 1
        except Exception as ex:
            logger.warning("  insert err %s game=%s: %s", name, game_id, ex)
    logger.info("  [REPLACE] game=%s team=%s before=%d -> %d players (%d pts, exact)",
                game_id, team_id, before, ins, box_sum)
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    engine = create_engine(SYNC_DATABASE_URL)
    name_pid = load_players(engine)
    for game_id, url, team_id, abbr, final in TARGETS:
        try:
            repair_game(engine, name_pid, game_id, url, team_id, abbr, final, args.dry_run)
        except Exception as ex:
            logger.warning("  error game=%s team=%s: %s", game_id, team_id, ex)
        time.sleep(1.5)
    logger.info("DONE")


if __name__ == "__main__":
    main()
