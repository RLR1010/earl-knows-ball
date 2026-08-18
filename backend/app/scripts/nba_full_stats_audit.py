"""Full NBA team-season stats audit vs basketball-reference.

For EVERY team × trainable season (labels 2016-2025, i.e. real 2016-17..2025-26),
compares our per-team season TOTALS (from nba.games) against basketball-reference's
/totals-team table on /leagues/NBA_{season_end}.html.

Correct column mapping discovered during audit:
- TOV = total_turnovers (player + team turnover), NOT the player-only `turnovers` col.
- PF (fouls) is largely MISSING in our data (sparse per-season coverage) -> flagged.

Usage:
  python app/scripts/nba_season_stats_audit.py            # run full audit
  python app/scripts/nba_season_stats_audit.py --fetch    # scrape bball-ref + cache
  python app/scripts/nba_season_stats_audit.py --cache /path.json  # use cached bball-ref
"""
import argparse
import json
import os
import sys
import time

import psycopg2
import requests
from bs4 import BeautifulSoup, Comment

from app.db_urls import PSYCOPG2_DATABASE_URL

# our label year -> bball-ref season-end URL year (label = season start year)
SEASONS = {yr: yr + 1 for yr in range(2016, 2026)}  # 2016->2017 ... 2025->2026

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
}

# mapping of stat name -> (our_sum_expr over games cols, bball stat key)
# Our totals computed from games columns (REG, FINAL). tov uses total_turnovers.
OUR_STATS = {
    "fg":   "sum(fg)",
    "fga":  "sum(fga)",
    "fg3":  "sum(fg3)",
    "fg3a": "sum(fg3a)",
    "ft":   "sum(ft)",
    "fta":  "sum(fta)",
    "orb":  "sum(orb)",
    "drb":  "sum(drb)",
    "trb":  "sum(trb)",
    "ast":  "sum(ast)",
    "stl":  "sum(stl)",
    "blk":  "sum(blk)",
    "tov":  "sum(tov)",
    "pf":   "sum(pf)",
    "pts":  "sum(pts)",
}

BB_STATS = ["fg", "fga", "fg3", "fg3a", "ft", "fta", "orb", "drb", "trb",
            "ast", "stl", "blk", "tov", "pf", "pts"]


def fetch_totals(season_end, cache=None):
    """Return {team_abbr: {stat: text}} from bball-ref /leagues/NBA_{season_end}.html."""
    if cache and str(season_end) in cache:
        return cache[str(season_end)]
    url = f"https://www.basketball-reference.com/leagues/NBA_{season_end}.html"
    r = requests.get(url, headers=HEADERS, timeout=50)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    t = soup.find("table", id="totals-team")
    if not t:
        return None
    res = {}
    for tr in t.find("tbody").find_all("tr"):
        cells = {}
        abbr = None
        for td in tr.find_all(["th", "td"]):
            ds = td.get("data-stat", "")
            if ds:
                cells[ds] = td.get_text(strip=True)
            a = td.find("a", href=True)
            if a and "/teams/" in a.get("href", ""):
                abbr = a["href"].split("/")[2]
        if cells and abbr:
            res[abbr] = cells
    return res


def our_totals(conn, lo_year, hi_year):
    """Return {(year, abbr): {stat: int}} computed from nba.games (REG FINAL)."""
    q = """
    WITH t AS (
      SELECT s.year yr, g.home_team_id tid,
        g.home_field_goals_made fg, g.home_field_goals_attempted fga,
        g.home_three_points_made fg3, g.home_three_points_attempted fg3a,
        g.home_free_throws_made ft, g.home_free_throws_attempted fta,
        g.home_offensive_rebounds orb, g.home_defensive_rebounds drb, g.home_rebounds trb,
        g.home_assists ast, g.home_steals stl, g.home_blocks blk,
        g.home_total_turnovers tov, g.home_fouls pf, g.home_score pts
      FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
      WHERE g.game_type='REG' AND g.status='FINAL'
      UNION ALL
      SELECT s.year, g.away_team_id,
        g.away_field_goals_made, g.away_field_goals_attempted,
        g.away_three_points_made, g.away_three_points_attempted,
        g.away_free_throws_made, g.away_free_throws_attempted,
        g.away_offensive_rebounds, g.away_defensive_rebounds, g.away_rebounds,
        g.away_assists, g.away_steals, g.away_blocks,
        g.away_total_turnovers, g.away_fouls, g.away_score
      FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
      WHERE g.game_type='REG' AND g.status='FINAL'
    )
    SELECT t.yr, te.abbreviation, count(*),
           sum(fg), sum(fga), sum(fg3), sum(fg3a), sum(ft), sum(fta),
           sum(orb), sum(drb), sum(trb), sum(ast), sum(stl), sum(blk), sum(tov),
           sum(pf), sum(pts)
    FROM t JOIN nba.teams te ON te.id=t.tid
    WHERE t.yr BETWEEN %s AND %s
    GROUP BY t.yr, te.abbreviation, te.id
    """
    cur = conn.cursor()
    cur.execute(q, (lo_year, hi_year))
    out = {}
    for r in cur.fetchall():
        yr, abbr, g = r[0], r[1], r[2]
        vals = r[3:]
        out[(yr, abbr)] = dict(zip([c for c in OUR_STATS], vals))
        out[(yr, abbr)]["g"] = g
    cur.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true",
                    help="Scrape all bball-ref seasons and cache to /tmp/nba_bball_totals.json")
    ap.add_argument("--cache", default="/tmp/nba_bball_totals.json")
    ap.add_argument("--lo", type=int, default=2016)
    ap.add_argument("--hi", type=int, default=2025)
    args = ap.parse_args()

    cache = {}
    if os.path.exists(args.cache):
        with open(args.cache) as f:
            cache = json.load(f)

    if args.fetch or not cache:
        print("Scraping basketball-reference seasons...", file=sys.stderr)
        for lbl, end in sorted(SEASONS.items()):
            if end in cache:
                continue
            try:
                d = fetch_totals(end, cache)
                if d:
                    cache[str(end)] = d
                    print(f"  NBA_{end}: {len(d)} teams", file=sys.stderr)
                else:
                    print(f"  NBA_{end}: FAILED", file=sys.stderr)
            except Exception as e:
                print(f"  NBA_{end}: error {e}", file=sys.stderr)
            time.sleep(2)
        with open(args.cache, "w") as f:
            json.dump(cache, f)

    conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
    ours = our_totals(conn, args.lo, args.hi)
    conn.close()

    # Compare
    print("year team  game counts (our | bball)  + discrepancies per stat (ours-bball)")
    n_teams = n_missing = 0
    discrep_rows = []
    for (yr, abbr), od in sorted(ours.items()):
        end = str(yr + 1)
        bb_teams = cache.get(end, {})
        bb = bb_teams.get(abbr)
        n_teams += 1
        if not bb:
            n_missing += 1
            continue
        diffs = []
        for s in BB_STATS:
            ov = od.get(s)
            bv_txt = bb.get(s)
            if ov is None or bv_txt is None:
                diffs.append(f"{s}:ours={ov} bb={bv_txt}")
                continue
            try:
                bv = int(bv_txt)
            except ValueError:
                continue
            if s == "pf":
                # our pf is counts of games WITH foul data, not total fouls
                continue
            d = int(ov) - bv
            if d != 0:
                diffs.append(f"{s}({d:+d})")
        g_ours, g_bb = od.get("g"), int(bb.get("g", 0))
        gap = g_ours - g_bb
        if gap != 0 or diffs:
            discrep_rows.append((yr, abbr, g_ours, g_bb, gap, diffs))
            print(f"{yr} {abbr:4}  G:{g_ours}|{g_bb} ({gap:+d})  " + " ".join(diffs))

    print(f"\nTotal team-seasons compared: {n_teams}, missing bball-ref: {n_missing}")
    print(f"Team-seasons with game-count or stat discrepancies: {len(discrep_rows)}")


if __name__ == "__main__":
    main()
