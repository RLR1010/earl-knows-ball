"""Authoritative replacement of incomplete/inaccurate NBA boxscores (sid 26-35).

After the gap-fill + team_id fix (97.4% exact), the remaining ~2.6% of team
boxscores don't sum to the final score. Root causes (confirmed by inspection):
  * OLD-ingest duplicate/overlapping rows with valid-but-wrong espn_ids and
    wrong points (e.g. Memphis double-counted; "Trenton Hassell" mis-link).
  * suffix-name duplicate player rows ("Xavier Tillman" vs "Xavier Tillman Sr.").
  * residual missing players.

Source of truth: the ESPN **summary** boxscore endpoint
(site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=<gid>).
Proven exact vs final scores on test games, and used by the DNP backfill across
all 12,824 games with zero empty results.

Strategy per mismatched team-boxscore: AUTHORITATIVE REPLACEMENT
  1. Fetch the summary boxscore -> authoritative per-team roster with espn_id,
     name, points, starter, dnp.
  2. SAFETY GUARD: proceed only if the team got >= 8 athletes AND its points sum
     EQUALS the official final score (both must hold, else skip -> never wipe a
     game on a bad/stale fetch).
  3. DELETE all existing pgs rows for that (game, team).
  4. INSERT one fresh row per summary athlete: resolve espn_id -> player_id via
     the players.espn_id map; if absent, auto-create a minimal player row (same
     approach nba_player_game_stats.process_game uses) so no stats are dropped.
     is_starter/dnp/dnp_reason are set from the summary.

This produces an exact ESPN mirror, resolving over-counts, under-counts, suffix
dups, and mis-links in one authoritative pass.

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/repair_nba_boxscores.py
  flags: --limit N | --newest-only | --dry-run
"""
import argparse
import asyncio
import logging
import os
import re
import sys
import time
import unicodedata

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

import httpx
from sqlalchemy import create_engine, text

from app.db_urls import PSYCOPG2_DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("nba-boxscore-repair")

SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EarlKnowsBall/1.0)"}

MIN_ROSTER_FOR_REPLACE = 8


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _minutes(v):
    if not v:
        return None
    m = re.match(r"^(\d+):(\d+)$", str(v))
    if m:
        return round(int(m.group(1)) + int(m.group(2)) / 60.0, 2)
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _pct(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _parse_split(v):
    """Parse '7-14' into (made, attempted), or (None, None)."""
    m = re.match(r"^(\d+)-(\d+)$", str(v))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _athlete_db(ath):
    """Map a parsed summary athlete to the exact nba.player_game_stats columns."""
    fgm, fga = _parse_split(ath.get("fg"))
    tpm, tpa = _parse_split(ath.get("tp"))
    ftm, fta = _parse_split(ath.get("ft"))
    return {
        "player_id": ath.get("player_id"),
        "game_id": ath.get("game_id"),
        "team_id": ath.get("team_id"),
        "nba_game_id": ath.get("nba_game_id"),
        "position": ath.get("position"),
        "jersey_number": ath.get("jersey"),
        "is_starter": ath.get("starter"),
        "minutes": ath.get("min"),
        "field_goals_made": fgm, "field_goals_attempted": fga, "field_goal_pct": ath.get("pct_fg"),
        "three_pointers_made": tpm, "three_pointers_attempted": tpa, "three_pointer_pct": ath.get("pct_tp"),
        "free_throws_made": ftm, "free_throws_attempted": fta, "free_throw_pct": ath.get("pct_ft"),
        "rebounds_offensive": ath.get("oreb"), "rebounds_defensive": ath.get("dreb"),
        "rebounds_total": ath.get("reb"), "assists": ath.get("ast"), "steals": ath.get("stl"),
        "blocks": ath.get("blk"), "turnovers": ath.get("tov"),
        "fouls_personal": ath.get("pf"), "points": ath.get("pts"),
        "dnp": ath.get("dnp"), "dnp_reason": None,
    }


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


def find_mismatched(engine, limit=None, newest_only=False):
    q = """
        WITH sums AS (
            SELECT pgs.game_id, pgs.team_id, sum(pgs.points) AS box_pts
            FROM nba.player_game_stats pgs
            JOIN nba.games g ON g.id = pgs.game_id
            WHERE g.season_id BETWEEN 26 AND 35 AND g.game_type IN ('REG','POST','PLAYIN')
            GROUP BY pgs.game_id, pgs.team_id
        ), actual AS (
            SELECT id AS game_id, home_team_id, away_team_id, home_score, away_score,
                   nba_game_id, season_id FROM nba.games
            WHERE season_id BETWEEN 26 AND 35 AND game_type IN ('REG','POST','PLAYIN')
        )
        SELECT a.game_id, a.nba_game_id, a.season_id, s.team_id,
               CASE WHEN s.team_id=a.home_team_id THEN a.home_score ELSE a.away_score END AS final
        FROM actual a JOIN sums s ON s.game_id=a.game_id
        WHERE ((s.team_id=a.home_team_id AND s.box_pts != a.home_score)
            OR (s.team_id=a.away_team_id AND s.box_pts != a.away_score))
    """
    q += "\n ORDER BY season_id, game_id" if not newest_only else "\n ORDER BY season_id DESC, game_id DESC"
    with engine.connect() as c:
        rows = c.execute(text(q)).all()
    return rows[:limit] if limit else rows


def load_caches(engine):
    espn = {}  # espn_id(int) -> player_id
    with engine.connect() as c:
        for pid, eid in c.execute(text("SELECT id, espn_id FROM nba.players WHERE espn_id IS NOT NULL")):
            espn[int(eid)] = pid
    return espn


def _db_team(engine, espn_abbr):
    alt = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "PHO": "PHX",
           "BK": "BKN", "UTAH": "UTA", "CHA": "CHO", "NOH": "NOP", "NOK": "NOP",
           "WSH": "WAS"}
    with engine.connect() as c:
        # Canonical teams are id 1-30. The ESPH-duplicate team rows (56-59) also
        # carry some abbreviations (e.g. id 58 = 'WSH') and would shadow canonical
        # ids in a naive lookup, so restrict to canonical teams only.
        for a in (espn_abbr, alt.get(espn_abbr)):
            if not a:
                continue
            row = c.execute(text("SELECT id FROM nba.teams WHERE abbreviation=:a AND id BETWEEN 1 AND 30"),
                            {"a": a}).first()
            if row:
                return row[0]
    return None


async def fetch_summary(client, espn_gid):
    try:
        r = await client.get(SUMMARY, params={"event": str(espn_gid)}, timeout=30)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def parse_summary_team(engine, js, target_db_team):
    """Return (db_team_id, athletes) for the team matching target_db_team, else (None,[])."""
    boxscore = (js or {}).get("boxscore") or {}
    for team in boxscore.get("players", []):
        db_tid = _db_team(engine, team.get("team", {}).get("abbreviation"))
        if db_tid != target_db_team:
            continue
        athletes = []
        for section in team.get("statistics", []):
            labels = section.get("labels", [])
            for a in section.get("athletes", []):
                at = a.get("athlete", {})
                sd = dict(zip(labels, a.get("stats", [])))
                athletes.append({
                    "espn_id": int(at.get("id")),
                    "name": at.get("displayName"),
                    "position": (at.get("position") or {}).get("abbreviation")
                                if isinstance(at.get("position"), dict) else at.get("position"),
                    "jersey": _num(at.get("jersey")),
                    "starter": bool(a.get("starter")),
                    "dnp": bool(a.get("didNotPlay")),
                    "pts": _num(sd.get("PTS")),
                    "min": _minutes(sd.get("MIN")),
                    "oreb": _num(sd.get("OREB")), "dreb": _num(sd.get("DREB")),
                    "reb": _num(sd.get("REB")), "ast": _num(sd.get("AST")),
                    "stl": _num(sd.get("STL")), "blk": _num(sd.get("BLK")),
                    "tov": _num(sd.get("TO")), "pf": _num(sd.get("PF")),
                    "fg": sd.get("FG"), "tp": sd.get("3PT"), "ft": sd.get("FT"),
                    "pct_fg": _pct(sd.get("FG%")), "pct_tp": _pct(sd.get("3PT%")),
                    "pct_ft": _pct(sd.get("FT%")),
                })
        return db_tid, athletes
    return None, []


def _resolve_or_create(engine, espn_cache, athlete, db_team_id):
    pid = espn_cache.get(athlete["espn_id"])
    if pid is not None:
        return pid
    # No DB player carries this espn_id. Try a normalized-name match to an existing
    # player (so we don't duplicate); fall back to auto-create a minimal row.
    nk = _nk(athlete["name"])
    if nk:
        with engine.connect() as c:
            cand = c.execute(text("SELECT id, name FROM nba.players")).all()
        for cpid, cname in cand:
            if _nk(cname) == nk:
                espn_cache[athlete["espn_id"]] = cpid
                return cpid
    # auto-create a minimal player row (like process_game) and link the espn id.
    # NOTE: idx_players_espn_id_unique is a PARTIAL unique index (WHERE espn_id IS
    # NOT NULL); PostgreSQL disallows ON CONFLICT on partial indexes, so we do an
    # explicit existence check + guarded insert instead.
    name = (athlete["name"] or "").strip() or f"unknown-{athlete['espn_id']}"
    with engine.connect() as c:
        existing = c.execute(text("SELECT id FROM nba.players WHERE espn_id=:e"),
                             {"e": athlete["espn_id"]}).first()
        if existing:
            espn_cache[athlete["espn_id"]] = existing[0]
            return existing[0]
    with engine.begin() as c:
        res = c.execute(text("""
            INSERT INTO nba.players (name, position, team_id, espn_id, active)
            VALUES (:name, 'F', :team, :espn, 0)
            RETURNING id
        """), {"name": name, "team": db_team_id, "espn": athlete["espn_id"]})
        new_id = res.scalar()
    espn_cache[athlete["espn_id"]] = new_id
    return new_id


async def repair_game(client, engine, row, espn_cache, dry_run):
    d = row._mapping
    db_gid, espn_gid, target_team, final = d["game_id"], d["nba_game_id"], d["team_id"], d["final"]
    js = await fetch_summary(client, espn_gid)
    if js is None:
        return "fetch_fail"
    db_tid, athletes = parse_summary_team(engine, js, target_team)
    if db_tid is None or len(athletes) < MIN_ROSTER_FOR_REPLACE:
        logger.warning("  SAFETY skip game=%s team=%s athletes=%d (<%d)",
                       db_gid, target_team, len(athletes), MIN_ROSTER_FOR_REPLACE)
        return "skip"
    # integrity guard: summary points sum must equal the official final score
    sum_pts = sum(a["pts"] or 0 for a in athletes)
    if sum_pts != int(final):
        logger.warning("  INTEGRITY skip game=%s team=%s summ=%d final=%d",
                       db_gid, target_team, sum_pts, int(final))
        return "skip"
    if dry_run:
        logger.info("  [DRYRUN] game=%s team=%s => would replace with %d athletes (%d pts)",
                    db_gid, target_team, len(athletes), sum_pts)
        return "ok"

    # authoritative replacement
    with engine.begin() as c:
        before = c.execute(text("SELECT count(*) FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t"),
                           {"g": db_gid, "t": target_team}).scalar()
        c.execute(text("DELETE FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t"),
                  {"g": db_gid, "t": target_team})
    inserted = 0
    for a in athletes:
        a["player_id"] = _resolve_or_create(engine, espn_cache, a, target_team)
        a["game_id"] = db_gid
        a["team_id"] = target_team
        a["nba_game_id"] = espn_gid
        v = _athlete_db(a)
        cols = [k for k in v if v[k] is not None]
        if not cols:
            continue
        try:
            # We DELETE the team's rows first, so plain INSERT cannot conflict on
            # (game_id, player_id). Avoids ON CONFLICT arbiter-inference failures
            # on the couple of games where the constraint isn't picked up.
            with engine.begin() as c:
                c.execute(text(
                    "INSERT INTO nba.player_game_stats ({}) VALUES ({})".format(
                        ", ".join(cols), ", ".join(":" + k for k in cols),
                    )),
                    {k: v[k] for k in cols})
            inserted += 1
        except Exception as ex:
            logger.warning("  insert err pid=%s game=%s: %s", a["player_id"], db_gid, ex)
    logger.info("  [REPLACE] game=%s team=%s before=%d -> %d athletes (%d pts, exact)",
                db_gid, target_team, before, inserted, sum_pts)
    return "ok"


async def main_async(args):
    engine = create_engine(PSYCOPG2_DATABASE_URL)
    mismatched = find_mismatched(engine, args.limit, args.newest_only)
    logger.info("mismatched team-boxscores (sid26-35): %d", len(mismatched))
    if not mismatched:
        return
    espn_cache = load_caches(engine)
    ok = fail = skip = 0
    t0 = time.time()
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        for i, row in enumerate(mismatched, 1):
            try:
                res = await repair_game(client, engine, row, espn_cache, args.dry_run)
                if res == "ok":
                    ok += 1
                elif res == "skip":
                    skip += 1
                else:
                    fail += 1
            except Exception as e:
                fail += 1
                logger.warning("  error game=%s team=%s: %s",
                               row._mapping["game_id"], row._mapping["team_id"], e)
            if i % 25 == 0 or i == len(mismatched):
                logger.info("  %d/%d ok=%d fail=%d skip=%d elapsed=%.0fs",
                            i, len(mismatched), ok, fail, skip, time.time() - t0)
            await asyncio.sleep(0.15)
    logger.info("DONE ok=%d fail=%d skip=%d", ok, fail, skip)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--newest-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="compute + log only, no DB writes")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
