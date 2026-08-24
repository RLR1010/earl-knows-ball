"""Populate nba.players.br_id from basketball-reference via verified stats-bridge.

Method (PROVEN on sample): for each training-season NBA game, fetch the bball-ref
boxscore (URL = YYYYMMDD0<HOME_ABBR>.html, ET date). Each bball-ref player row
carries a unique slug (e.g. jamesle01). We match each bball-ref player to exactly
one of our nba.player_game_stats rows for that game by an identical per-game
stat-line fingerprint (PTS,FGA,3P,3PA,FT,FTA,TRB,AST,STL,BLK,TOV,PF -- minutes
excluded because bball-ref stores M:SS while pgs stores whole minutes). A match is
accepted ONLY when it is unique (one pgs player has that exact stat line).

We then associate player_id <-> br_id across all games. br_id is committed to
nba.players only when a player has a single CONSISTENT slug (no conflicts).

ACCURACY-FIRST:
- dry-run by default (--commit to write).
- no auto-create of players; only updates nba.players.br_id on existing rows.
- conflicts/ambiguities are logged, never guessed.
"""
import argparse
import re
import sys
import logging
import time
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.nba_br_id_backfill")

sys.path.insert(0, "app/scripts/ingress")
# robust fetch (backoff + disk cache) to survive bball-ref rate limits
from bbref_fetch import fetch_client, BBRateLimited
from backfill_nba_pgs_bbref import _fetch, _parse_player_stats, _br_abbr_candidates

from sqlalchemy import create_engine, text

SYNC_DATABASE_URL = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"
BR = "https://www.basketball-reference.com/boxscores/"

# stat fingerprint keys shared by both sources (minutes EXCLUDED: M:SS vs int)
KEYS = ["PTS", "FGA", "3P", "3PA", "FT", "FTA", "TRB", "AST", "STL", "BLK", "TOV", "PF"]
PGS_IDX = {3: "points", 4: "field_goals_attempted", 5: "three_pointers_made", 6: "three_pointers_attempted",
           7: "free_throws_made", 8: "free_throws_attempted", 9: "rebounds_total", 10: "assists",
           11: "steals", 12: "blocks", 13: "turnovers", 14: "fouls_personal"}


def _norm(v):
    if v in (None, "", "--", "-"):
        return None
    return str(v).strip()


def br_fp(st):
    # bball-ref stats dict: keys like PTS, FGA...
    return tuple(_norm(st.get(k)) for k in KEYS)


def pgs_fp(o):
    # o = SELECT row: pid(0),espn(1),name(2),points(3),fga(4),3pm(5),3pa(6),ftm(7),
    #     fta(8),reb(9),ast(10),stl(11),blk(12),tov(13),pf(14)
    return tuple(_norm(o[i]) for i in range(3, 15))


def game_targets(conn, seasons):
    """All FINAL games in the given seasons (full coverage for br_id)."""
    q = text("""
        SELECT g.id, (g.date AT TIME ZONE 'America/New_York')::date AS et,
               g.home_team_id, g.away_team_id, h.abbreviation, a.abbreviation
        FROM nba.games g
        JOIN nba.teams h ON h.id = g.home_team_id
        JOIN nba.teams a ON a.id = g.away_team_id
        WHERE g.status = 'FINAL' AND g.season_id BETWEEN :lo AND :hi
        ORDER BY g.date
    """)
    return conn.execute(q, {"lo": seasons[0], "hi": seasons[1]}).fetchall()


def collect(engine, seasons, max_games, dry_run):
    # player_id -> set of slugs
    slug_map = defaultdict(set)
    games_done = 0
    fetch_fail = 0
    ambiguity = 0
    no_match = 0
    total_matches = 0
    with engine.connect() as conn:
        targets = game_targets(conn, seasons)
        logger.info(f"{len(targets)} games in seasons {seasons} matched for sweep")
        for gid, et, hid, aid, hab, aab in targets:
            if max_games and games_done >= max_games:
                break
            ds = et.strftime("%Y%m%d"); year = et.year
            html = None
            for br_home in _br_abbr_candidates(hab, year):
                try:
                    html = fetch_client.fetch(f"{BR}{ds}0{br_home}.html")
                    break
                except BBRateLimited:
                    logger.error(f"game {gid}: bball-ref rate limit exhausted; aborting run")
                    raise
                except Exception:
                    continue
            if html is None:
                fetch_fail += 1
                continue
            games_done += 1
            tmap = {}
            for cand in _br_abbr_candidates(hab, year):
                tmap.setdefault(cand.upper(), hid)
            for cand in _br_abbr_candidates(aab, year):
                tmap.setdefault(cand.upper(), aid)

            for m in re.finditer(r'<table[^>]*id="box-(\w+)-game-basic"(.*?)</table>', html, re.S):
                tid = tmap.get(m.group(1).upper())
                if tid is None:
                    continue
                seg = m.group(2)
                players = _parse_player_stats(seg)
                ours = conn.execute(text("""
                    SELECT pg.player_id, p.espn_id, p.name, pg.points, pg.field_goals_attempted,
                           pg.three_pointers_made, pg.three_pointers_attempted, pg.free_throws_made,
                           pg.free_throws_attempted, pg.rebounds_total, pg.assists, pg.steals,
                           pg.blocks, pg.turnovers, pg.fouls_personal
                    FROM nba.player_game_stats pg JOIN nba.players p ON p.id = pg.player_id
                    WHERE pg.game_id = :g AND pg.team_id = :t
                """), {"g": gid, "t": tid}).fetchall()
                ours_by_fp = defaultdict(list)
                for o in ours:
                    ours_by_fp[pgs_fp(o)].append(o)
                # also extract slug per name from the table hrefs
                slug_by_name = {}
                for sm in re.finditer(r'href="/players/[a-z]/([a-z0-9]{6,12})\.html"[^>]*>([^<]+)</a>', seg):
                    slug_by_name[sm.group(2).strip().lower()] = sm.group(1)
                for pname, st in players:
                    fp = br_fp(st)
                    if fp.count(None) == len(fp):
                        continue
                    cand = ours_by_fp.get(fp, [])
                    if len(cand) == 1:
                        pid = cand[0][0]
                        slug = slug_by_name.get((pname or "").strip().lower())
                        if slug:
                            slug_map[pid].add(slug)
                            total_matches += 1
                    elif len(cand) > 1:
                        ambiguity += 1
                    else:
                        no_match += 1
            time.sleep(1.0)
    return slug_map, {"games_done": games_done, "fetch_fail": fetch_fail,
                      "ambiguity": ambiguity, "no_match": no_match, "matches": total_matches}


def resolve(engine, slug_map):
    """Resolve each player to a single consistent br_id (or leave NULL if conflicted)."""
    to_write = []   # (player_id, br_id)
    conflicted = []
    missing = []
    with engine.connect() as conn:
        for pid, slugs in sorted(slug_map.items()):
            slugs = {s for s in slugs if s}
            if len(slugs) == 1:
                to_write.append((pid, slugs.pop()))
            elif len(slugs) > 1:
                # keep only if all variants share a common stable root is risky;
                # report as conflict
                conflicted.append((pid, slugs))
            else:
                missing.append(pid)
    return to_write, conflicted, missing


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="26-35", help="season_id range, e.g. 26-35")
    ap.add_argument("--max-games", type=int, default=0, help="cap games (0=all)")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args(argv)
    lo, hi = (int(x) for x in args.seasons.split("-"))
    engine = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)

    logger.info(f"Collecting br_id cross-references (seasons {lo}-{hi}, dry_run={not args.commit})")
    slug_map, stats = collect(engine, (lo, hi), args.max_games, not args.commit)
    logger.info(f"COLLECT: {stats}")
    to_write, conflicted, missing = resolve(engine, slug_map)
    logger.info(f"RESOLVE: {len(to_write)} unique-to-write, {len(conflicted)} conflicted, "
                f"{len(missing)} slug-less")

    # report conflicts (never guess)
    if conflicted:
        with engine.connect() as conn:
            for pid, slugs in conflicted[:20]:
                name = conn.execute(text("SELECT name FROM nba.players WHERE id=:p"), {"p": pid}).scalar()
                logger.warning(f"CONFLICT pid={pid} {name}: slugs={slugs}")

    if not args.commit:
        logger.info("DRY-RUN complete. Pass --commit to write br_id values.")
        return

    written = 0
    with engine.begin() as conn:
        for pid, br_id in to_write:
            conn.execute(text(
                "UPDATE nba.players SET br_id=:b WHERE id=:p AND (br_id IS NULL OR br_id=:b)"
            ), {"b": br_id, "p": pid})
            written += 1
    logger.info(f"COMMITTED br_id on {written} players. Conflicts left NULL: {len(conflicted)}")


if __name__ == "__main__":
    main(sys.argv[1:])
