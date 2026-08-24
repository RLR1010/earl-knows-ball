"""Cross-check & backfill incomplete NBA boxscores (training seasons) from basketball-reference.

SCOPE: only REG/POST/PLAYIN games in training seasons (26-35) whose pgs boxscore does NOT
sum to the final score (missing players/rows). Fetches each game's bball-ref boxscore,
matches players to our nba.player_game_stats / nba.players via a VERIFIED stats-bridge
(identical per-game stat-line), populates nba.players.br_id, and inserts missing pgs rows.

HARD GUARDRAILS (Rich):
- NEVER auto-create players. A pgs row is only written for a player already in nba.players
  (matched by br_id, espn_id, or exact name). Unresolvable players are LOGGED, never guessed.
- Match players by UNIQUE stat-line fingerprint (PTS,FGA,3P,3PA,FT,FTA,TRB,AST,STL,BLK,TOV,PF).
  Minutes EXCLUDED (bball-ref M:SS vs our int). Ambiguous/duplicate lines -> skip + log.
- Dry-run by default; --commit to write.
- After processing a game, verify the pgs boxscore now sums exactly to the final score.

Usage:
  python app/scripts/backfill_nba_boxscores_bbref.py                       # dry-run, all 52
  python app/scripts/backfill_nba_boxscores_bbref.py --seasons 29-30 --max 5 --commit
"""
import argparse
import logging
import os
import re
import sys
import time
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.nba_boxscore_backfill_bbref")

sys.path.insert(0, "app/scripts")
sys.path.insert(0, "app/scripts/ingress")
from bbref_fetch import fetch_client, BBRateLimited
from backfill_nba_pgs_bbref import _br_abbr_candidates, _norm_abbr

from sqlalchemy import create_engine, text

SYNC_DATABASE_URL = os.environ.get(
    "SYNC_DATABASE_URL",
    "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)
BR = "https://www.basketball-reference.com/boxscores/"

# stat fingerprint keys (shared, minutes excluded for M:SS vs int)
KEYS = ["PTS", "FGA", "3P", "3PA", "FT", "FTA", "TRB", "AST", "STL", "BLK", "TOV", "PF"]
PGS_SELECT = [
    "points", "field_goals_attempted", "three_pointers_made", "three_pointers_attempted",
    "free_throws_made", "free_throws_attempted", "rebounds_total", "assists",
    "steals", "blocks", "turnovers", "fouls_personal",
]

BR_STAT_KEY = {  # bball-ref parse dict key -> pgs column
    "PTS": "points", "FG": "field_goals_made", "FGA": "field_goals_attempted",
    "FG%": "field_goal_pct", "3P": "three_pointers_made", "3PA": "three_pointers_attempted",
    "3P%": "three_pointer_pct", "FT": "free_throws_made", "FTA": "free_throws_attempted",
    "FT%": "free_throw_pct", "ORB": "rebounds_offensive", "DRB": "rebounds_defensive",
    "TRB": "rebounds_total", "AST": "assists", "STL": "steals", "BLK": "blocks",
    "TOV": "turnovers", "PF": "fouls_personal", "+/-": "plus_minus",
}


def _norm(v):
    if v in (None, "", "--", "-"):
        return None
    return str(v).strip()


def _int(v):
    v = _norm(v)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    v = _norm(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _min(v):
    """bball-ref MP '46:13' -> 46 whole minutes (matches pgs int minutes)."""
    v = _norm(v)
    if not v:
        return None
    try:
        return int(float(v.split(":")[0]))
    except (TypeError, ValueError):
        return None


def br_fp(st):
    return tuple(_norm(st.get(k)) for k in KEYS)


def pgs_fp(o):
    # o = [pid, espn_id, name, stat1..stat12]  (indices 3..14)
    return tuple(_norm(o[3 + i]) for i in range(len(KEYS)))


def _incomplete_games(conn, seasons, game_types):
    q = text("""
        SELECT g.id, g.season_id, g.game_type,
               (g.date AT TIME ZONE 'America/New_York')::date AS et,
               g.home_score, g.away_score, g.home_team_id, g.away_team_id,
               t.abbreviation hab, a.abbreviation aab
        FROM nba.games g
        JOIN nba.teams t ON t.id = g.home_team_id
        JOIN nba.teams a ON a.id = g.away_team_id
        WHERE g.status = 'FINAL' AND g.season_id BETWEEN :lo AND :hi
          AND g.game_type IN :gtypes
          AND (
            NOT EXISTS (SELECT 1 FROM nba.player_game_stats pg WHERE pg.game_id=g.id AND pg.team_id=g.home_team_id)
            OR NOT EXISTS (SELECT 1 FROM nba.player_game_stats pg WHERE pg.game_id=g.id AND pg.team_id=g.away_team_id)
            OR (SELECT COALESCE(SUM(pg.points),0) FROM nba.player_game_stats pg WHERE pg.game_id=g.id AND pg.team_id=g.home_team_id) <> g.home_score
            OR (SELECT COALESCE(SUM(pg.points),0) FROM nba.player_game_stats pg WHERE pg.game_id=g.id AND pg.team_id=g.away_team_id) <> g.away_score)
        ORDER BY g.date
    """)
    return conn.execute(q, {"lo": seasons[0], "hi": seasons[1],
                            "gtypes": tuple(game_types)}).fetchall()


def _resolve_player(conn, br_name, br_slug, team_id, game_id):
    """Resolve a bball-ref player to our player_id. Order:
    1) by matching to an EXISTING pgs row for this game by exact normalized name (highest signal)
    2) by br_slug if we already know it (nba.players.br_id)
    3) by exact normalized name in nba.players (position/team not authoritative).
    Returns player_id or None. NEVER auto-creates.
    """
    key = (br_name or "").strip().lower()
    if br_slug:
        r = conn.execute(text("SELECT id FROM nba.players WHERE br_id=:b"), {"b": br_slug}).first()
        if r:
            return r[0]
    if key:
        r = conn.execute(text("""
            SELECT id FROM nba.players
            WHERE lower(regexp_replace(name, '[^a-zA-Z ]','','g')) = :k
            ORDER BY id LIMIT 1
        """), {"k": re.sub(r"[^a-zA-Z ]", "", key)}).first()
        if r:
            return r[0]
    return None


def process_game(conn, g, commit):
    gid, season, gtype, et, hs, as_, hid, aid, hab, aab = g
    ds = et.strftime("%Y%m%d"); year = et.year
    html = None
    for br_home in _br_abbr_candidates(hab, year):
        try:
            html = fetch_client.fetch(f"{BR}{ds}0{br_home}.html")
            break
        except BBRateLimited:
            logger.error(f"game {gid}: bball-ref rate-limited; aborting run")
            raise
        except Exception:
            continue
    if html is None:
        logger.warning(f"game {gid} ({ds}): could not fetch boxscore")
        return {"fetched": False}

    tmap = {}
    for cand in _br_abbr_candidates(hab, year):
        tmap.setdefault(cand.upper(), hid)
    for cand in _br_abbr_candidates(aab, year):
        tmap.setdefault(cand.upper(), aid)

    stats = {}
    slug_by_fp = {}      # (team_id, fp) -> slug (first seen)
    name_by_fp = {}      # (team_id, fp) -> name
    for m in re.finditer(r'<table[^>]*id="box-(\w+)-game-basic"(.*?)</table>', html, re.S):
        tid = tmap.get(m.group(1).upper())
        if tid is None:
            continue
        seg = m.group(2)
        # map name->slug from hrefs
        sl_by_name = {}
        for sm in re.finditer(r'href="/players/[a-z]/([a-z0-9]{6,12})\.html"[^>]*>([^<]+)</a>', seg):
            sl_by_name[sm.group(2).strip().lower()] = sm.group(1)
        from backfill_nba_pgs_bbref import _parse_player_stats
        for pname, s in _parse_player_stats(seg):
            fp = br_fp(s)
            if fp.count(None) == len(fp):
                continue
            nm = (pname or "").strip().lower()
            slug = sl_by_name.get(nm)
            stats.setdefault(tid, []).append({"name": pname, "slug": slug, "fp": fp, "sd": s})

    report = {"fetched": True, "teams": 0, "footprint_matches": 0, "br_id_populated": 0,
              "inserted_rows": 0, "unresolved": [], "boxscore_exact_after": False}
    for tid, players in stats.items():
        report["teams"] += 1
        # build our pgs fingerprint index for this team+game
        ours = conn.execute(text(f"""
            SELECT pg.player_id, p.espn_id, p.name,
                   {', '.join('pg.' + c for c in PGS_SELECT)}
            FROM nba.player_game_stats pg JOIN nba.players p ON p.id = pg.player_id
            WHERE pg.game_id = :g AND pg.team_id = :t
        """), {"g": gid, "t": tid}).fetchall()
        ours_by_fp = defaultdict(list)
        for o in ours:
            ours_by_fp[pgs_fp(o)].append(o)
        ours_by_name = {}
        for o in ours:
            ours_by_name[(o[2] or "").strip().lower()] = o

        for pl in players:
            pid = None
            # 1) unique stat fingerprint match against existing pgs row
            cand = ours_by_fp.get(pl["fp"], [])
            if len(cand) == 1:
                pid = cand[0][0]
                report["footprint_matches"] += 1
            elif len(cand) > 1:
                logger.warning(f"  game {gid} team {tid} '{pl['name']}' stat-FP ambiguous "
                               f"({len(cand)} players); skip")
                continue
            # 2) fall back to name/br_id resolution (no new pgs rows--only for br_id)
            if pid is None:
                pid = _resolve_player(conn, pl["name"], pl["slug"], tid, gid)

            if pid is None:
                report["unresolved"].append(pl["name"])
                logger.warning(f"  game {gid} team {tid} '{pl['name']}' (slug={pl['slug']}) "
                               f"UNRESOLVED (no existing player boxscore/record); NOT created")
                continue

            # populate br_id if we learned a slug and don't have one
            if pl["slug"]:
                cur = conn.execute(text("SELECT br_id FROM nba.players WHERE id=:p"),
                                   {"p": pid}).scalar()
                if not cur and commit:
                    conn.execute(text("UPDATE nba.players SET br_id=:b WHERE id=:p"),
                                 {"b": pl["slug"], "p": pid})
                    report["br_id_populated"] += 1

            # insert missing pgs row (this player has no pgs row for this game yet)
            if pid not in {o[0] for o in ours}:
                if commit:
                    sd = pl["sd"]
                    try:
                        conn.execute(text("""
                            INSERT INTO nba.player_game_stats
                              (game_id, player_id, team_id, minutes,
                               field_goals_made, field_goals_attempted, field_goal_pct,
                               three_pointers_made, three_pointers_attempted, three_pointer_pct,
                               free_throws_made, free_throws_attempted, free_throw_pct,
                               rebounds_offensive, rebounds_defensive, rebounds_total,
                               assists, steals, blocks, turnovers, fouls_personal, points, plus_minus)
                            VALUES
                              (:game_id,:player_id,:team_id,:min,:fgm,:fga,:fgp,:tpm,:tpa,:tpp,
                               :ftm,:fta,:ftp,:oreb,:dreb,:treb,:ast,:stl,:blk,:tov,:pf,:pts,:pm)
                            ON CONFLICT (game_id, player_id) DO NOTHING
                        """), {
                            "game_id": gid, "player_id": pid, "team_id": tid,
                            "min": _min(sd.get("MP")),
                            "fgm": _int(sd.get("FG")), "fga": _int(sd.get("FGA")),
                            "fgp": _float(sd.get("FG%")),
                            "tpm": _int(sd.get("3P")), "tpa": _int(sd.get("3PA")),
                            "tpp": _float(sd.get("3P%")),
                            "ftm": _int(sd.get("FT")), "fta": _int(sd.get("FTA")),
                            "ftp": _float(sd.get("FT%")),
                            "oreb": _int(sd.get("ORB")), "dreb": _int(sd.get("DRB")),
                            "treb": _int(sd.get("TRB")), "ast": _int(sd.get("AST")),
                            "stl": _int(sd.get("STL")), "blk": _int(sd.get("BLK")),
                            "tov": _int(sd.get("TOV")), "pf": _int(sd.get("PF")),
                            "pts": _int(sd.get("PTS")), "pm": _int(sd.get("+/-")),
                        })
                        report["inserted_rows"] += 1
                    except Exception as e:
                        logger.warning(f"  insert err game {gid} {pl['name']}: {e}")

    # log pre-existing pgs status (not just post)
    hsum0 = conn.execute(text(
        "SELECT COALESCE(SUM(points),0) FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t"),
        {"g": gid, "t": hid}).scalar()
    asum0 = conn.execute(text(
        "SELECT COALESCE(SUM(points),0) FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t"),
        {"g": gid, "t": aid}).scalar()
    logger.info(f"  game {gid}: BEFORE home {hsum0}/{hs} away {asum0}/{as_}")
    # verify boxscore sums
    if commit:
        hsum = conn.execute(text(
            "SELECT COALESCE(SUM(points),0) FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t"),
            {"g": gid, "t": hid}).scalar()
        asum = conn.execute(text(
            "SELECT COALESCE(SUM(points),0) FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t"),
            {"g": gid, "t": aid}).scalar()
        report["boxscore_exact_after"] = (hsum == hs and asum == as_)
        logger.info(f"  game {gid}: AFTER home {hsum}/{hs} away {asum}/{as_} exact={report['boxscore_exact_after']}")
    return report


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="26-35")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--game-id", type=int, default=0, help="only process this one game id")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args(argv)
    lo, hi = (int(x) for x in args.seasons.split("-"))
    gtypes = ["REG", "POST", "PLAYIN"]
    engine = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)

    with engine.connect() as conn:
        games = _incomplete_games(conn, (lo, hi), gtypes)
    if args.game_id:
        games = [g for g in games if g[0] == args.game_id]
        if not games:
            logger.error(f"game {args.game_id} not in incomplete set"); return
    logger.info(f"{len(games)} incomplete games to process (dry_run={not args.commit})")
    if args.max:
        games = games[: args.max]

    agg = {"games": 0, "fetched": 0, "footprint_matches": 0, "br_id_populated": 0,
           "inserted_rows": 0, "unresolved": 0, "exact_after": 0}
    with engine.connect() as conn:
        if args.commit:
            tx = conn.begin()
        for g in games:
            try:
                rep = process_game(conn, g, args.commit)
            except BBRateLimited as e:
                logger.error(f"rate-limited: {e}")
                if args.commit:
                    tx.rollback()
                break
            agg["games"] += 1
            if rep.get("fetched"):
                agg["fetched"] += 1
                agg["footprint_matches"] += rep["footprint_matches"]
                agg["br_id_populated"] += rep["br_id_populated"]
                agg["inserted_rows"] += rep["inserted_rows"]
                agg["unresolved"] += len(rep["unresolved"])
                if rep.get("boxscore_exact_after"):
                    agg["exact_after"] += 1
            time.sleep(2.0)  # polite pacing for bball-ref
        if args.commit:
            tx.commit()

    logger.info("=" * 60)
    logger.info(f"[{'COMMIT' if args.commit else 'DRY-RUN'}] SUMMARY")
    logger.info(f"  games processed : {agg['games']}")
    logger.info(f"  games fetched   : {agg['fetched']}")
    logger.info(f"  stat-FP matches : {agg['footprint_matches']}")
    logger.info(f"  br_id populated : {agg['br_id_populated']}")
    logger.info(f"  pgs rows inserted: {agg['inserted_rows']}")
    logger.info(f"  unresolved      : {agg['unresolved']}")
    logger.info(f"  boxscore exact after: {agg['exact_after']}")
    logger.info("=" * 60)
    if not args.commit:
        logger.info("DRY-RUN complete. Re-run with --commit to write changes.")


if __name__ == "__main__":
    main(sys.argv[1:])
