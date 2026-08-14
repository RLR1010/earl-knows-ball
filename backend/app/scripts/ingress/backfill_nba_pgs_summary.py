"""Backfill nba.player_game_stats for OLD games using ESPN's /summary endpoint.

The current ingestor (nba_player_game_stats.py) uses the
`/competitions/{id}/competitors/{id}/statistics` URL shape, which returns HTTP 404
for many pre-2016 games. Those old games ARE served by ESPN's
`/summary?event={gid}` endpoint, which returns boxscore.players[0..1] with
per-athlete stats arrays aligned to statistics[0].keys.

This loader reuses the same `nba.player_game_stats` insert/ON CONFLICT semantics
and auto-creates missing `nba.players` rows (name/position/team/espn_id) so no
player's stats are dropped.

Usage:
  cd <repo>/backend && PYTHONPATH=$PWD <venv>/bin/python \
      app/scripts/ingress/backfill_nba_pgs_summary.py [year ...] [--limit N]
  Without args: targets all still-incomplete FINAL REG/POST games for years
  2006..2018 (the old games the current ingestor 404s on).
"""
import asyncio
import httpx
import json
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.nba_pgs_summary")

REPO = "/home/rich/.openclaw/workspace/earl-knows-football"
sys.path.insert(0, f"{REPO}/backend")

from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={gid}"

# statistics[0].keys -> (db_column stored via nba_player_stats dict names for sv())
# We'll parse from the aligned stats array directly.


def _to_int(v):
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = str(v).strip()
    if s in ("", "--", "-", "None", "null"):
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _to_float(v):
    if isinstance(v, int) or isinstance(v, float):
        return float(v)
    s = str(v).strip()
    if s in ("", "--", "-", "None", "null"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_fg(v):
    """return (made, attempted) from '5-12' style."""
    made = attempted = None
    s = str(v).strip()
    if s and "-" in s:
        try:
            m, a = s.split("-")
            made, attempted = _to_int(m), _to_int(a)
        except Exception:
            pass
    return made, attempted


def _build_espn_cache(db_conn) -> dict:
    rows = db_conn.execute(
        text("SELECT id, espn_id FROM nba.players WHERE espn_id IS NOT NULL")
    ).fetchall()
    return {int(eid): pid for pid, eid in rows}


def _incomplete_games(db_conn, year: int, limit: int):
    q = text("""
        SELECT g.id, g.nba_game_id, h.abbreviation, a.abbreviation
        FROM nba.games g
        JOIN nba.seasons s ON s.id = g.season_id
        JOIN nba.teams h ON h.id = g.home_team_id
        JOIN nba.teams a ON a.id = g.away_team_id
        WHERE s.year = :year AND g.game_type IN ('REG','POST')
          AND g.status::text = 'FINAL' AND g.nba_game_id IS NOT NULL
          AND (
            NOT EXISTS (SELECT 1 FROM nba.player_game_stats ph
                        WHERE ph.game_id = g.id AND ph.team_id = g.home_team_id)
            OR NOT EXISTS (SELECT 1 FROM nba.player_game_stats pa
                           WHERE pa.game_id = g.id AND pa.team_id = g.away_team_id)
          )
        ORDER BY g.date
    """)
    rows = db_conn.execute(q, {"year": year}).fetchall()
    if limit:
        rows = rows[:limit]
    return rows


def _team_id_from_abbr(db_conn, abbr: str):
    return db_conn.execute(
        text("SELECT id FROM nba.teams WHERE abbreviation = :a OR name = :n"),
        {"a": abbr, "n": abbr},
    ).scalar()


def _resolve_or_create_player(db_conn, espn_id, display_name, position, team_id, espn_cache):
    pid = espn_cache.get(int(espn_id)) if espn_id else None
    if pid:
        return pid
    # name match attempt (fallback)
    if display_name:
        row = db_conn.execute(
            text("SELECT id FROM nba.players WHERE name = :n LIMIT 1"),
            {"n": display_name},
        ).fetchone()
        if row:
            pid = row[0]
            if espn_id:
                db_conn.execute(text("UPDATE nba.players SET espn_id=:e WHERE id=:i"),
                                {"e": int(espn_id), "i": pid})
                espn_cache[int(espn_id)] = pid
            return pid
    # auto-create
    pos = (position or "F")[:4] or "F"
    ins = db_conn.execute(text("""
        INSERT INTO nba.players (name, position, team_id, espn_id, active)
        VALUES (:n, :p, :t, :e, 0) RETURNING id
    """), {"n": display_name, "p": pos, "t": team_id, "e": int(espn_id) if espn_id else None})
    row = ins.fetchone()
    if row:
        pid = row[0]
        if espn_id:
            espn_cache[int(espn_id)] = pid
        logger.info(f"  auto-created player id={pid} '{display_name}' ({pos})")
        return pid
    return None


async def _process_summary_game(db_conn, db_game_id, espn_gid, home_abbr, away_abbr, espn_cache, client) -> int:
    url = SUMMARY_URL.format(gid=espn_gid)
    r = await client.get(url)
    if r.status_code != 200:
        logger.warning(f"  game {db_game_id} ({espn_gid}): summary HTTP {r.status_code}")
        return 0
    d = r.json()
    boxscore = d.get("boxscore") or {}
    all_players = boxscore.get("players") or []
    inserted = 0
    for team_block in all_players:
        team_meta = team_block.get("team") or {}
        team_abbr = (team_meta.get("abbreviation") or "").upper()
        db_team_id = _team_id_from_abbr(db_conn, team_abbr)
        if not db_team_id:
            # maybe use home/away by matching; fall back to pass-through
            db_team_id = _team_id_from_abbr(db_conn, home_abbr) or _team_id_from_abbr(db_conn, away_abbr)
        stats_blocks = team_block.get("statistics") or []
        if not stats_blocks:
            continue
        sb = stats_blocks[0]
        keys = sb.get("keys") or []
        athletes = sb.get("athletes") or []
        for ath in athletes:
            athlete = ath.get("athlete") or {}
            a_id = athlete.get("id")
            a_name = athlete.get("displayName") or athlete.get("shortName") or ""
            pos = (athlete.get("position") or {}).get("abbreviation") or "F"
            if not a_id or not a_name:
                continue
            pid = _resolve_or_create_player(db_conn, a_id, a_name, pos, db_team_id, espn_cache)
            if not pid:
                continue
            raw = ath.get("stats") or []
            s = dict(zip(keys, raw))  # key -> string value
            fgm, fga = _parse_fg(s.get("fieldGoalsMade-fieldGoalsAttempted"))
            tpm, tpa = _parse_fg(s.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
            ftm, fta = _parse_fg(s.get("freeThrowsMade-freeThrowsAttempted"))
            try:
                db_conn.execute(text("""
                    INSERT INTO nba.player_game_stats
                        (game_id, player_id, team_id, nba_game_id, nba_player_id,
                         minutes, field_goals_made, field_goals_attempted,
                         three_pointers_made, three_pointers_attempted,
                         free_throws_made, free_throws_attempted,
                         rebounds_offensive, rebounds_defensive, rebounds_total,
                         assists, steals, blocks, turnovers, fouls_personal,
                         points, plus_minus)
                    VALUES
                        (:game_id, :player_id, :team_id, :nba_game_id, :nba_player_id,
                         :min, :fgm, :fga, :tpm, :tpa, :ftm, :fta,
                         :oreb, :dreb, :treb, :ast, :stl, :blk, :tov, :pf,
                         :pts, :pm)
                    ON CONFLICT (game_id, player_id) DO NOTHING
                """), {
                    "game_id": db_game_id,
                    "player_id": pid,
                    "team_id": db_team_id,
                    "nba_game_id": str(espn_gid),
                    "nba_player_id": _to_int(a_id),
                    "min": s.get("minutes"),
                    "fgm": fgm, "fga": fga,
                    "tpm": tpm, "tpa": tpa,
                    "ftm": ftm, "fta": fta,
                    "oreb": _to_int(s.get("offensiveRebounds")),
                    "dreb": _to_int(s.get("defensiveRebounds")),
                    "treb": _to_int(s.get("rebounds")),
                    "ast": _to_int(s.get("assists")),
                    "stl": _to_int(s.get("steals")),
                    "blk": _to_int(s.get("blocks")),
                    "tov": _to_int(s.get("turnovers")),
                    "pf": _to_int(s.get("fouls")),
                    "pts": _to_int(s.get("points")),
                    "pm": _to_float(s.get("plusMinus")),
                })
                inserted += 1
            except Exception as e:
                logger.warning(f"  insert err game {db_game_id} player {a_id}: {e}")
    return inserted


async def backfill_year(year: int, limit: int, engine):
    started = time.time()
    total = 0
    errors = 0
    with engine.connect() as db_conn:
        espn_cache = _build_espn_cache(db_conn)
        games = _incomplete_games(db_conn, year, limit)
        logger.info(f"[{year}] {len(games)} incomplete games to backfill via /summary")
        if not games:
            return {"year": year, "games": 0, "rows": 0, "still": 0}
        async with httpx.AsyncClient(timeout=30) as client:
            for idx, (db_gid, espn_gid, hab, aab) in enumerate(games, 1):
                try:
                    n = await _process_summary_game(
                        db_conn, db_gid, espn_gid, hab, aab, espn_cache, client)
                except Exception as e:
                    n = 0
                    logger.warning(f"  game {db_gid} ({espn_gid}) failed: {e}")
                total += n
                if n == 0:
                    errors += 1
                if idx % 25 == 0 or idx == len(games):
                    db_conn.commit()
                    logger.info(f"  [{year}] {idx}/{len(games)} games, {total} rows, {errors} empty")
                await asyncio.sleep(0.25)
        db_conn.commit()
        still = len(_incomplete_games(db_conn, year, 0))
        logger.info(f"[{year}] DONE: {total} rows, {errors} empty, {still} still incomplete, {time.time()-started:.0f}s")
        return {"year": year, "games": len(games), "rows": total, "empty": errors, "still": still}


async def main(argv):
    limit = 0
    years = []
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
    logger.info(f"Summary backfill years={years} limit={limit or 'none'}")
    engine = create_engine(PSYCOPG2_DATABASE_URL)
    summary = []
    for year in years:
        try:
            summary.append(await backfill_year(year, limit, engine))
        except Exception as e:
            logger.exception(f"[{year}] FAILED: {e}")
            summary.append({"year": year, "error": str(e)})
    engine.dispose()
    logger.info("=== SUMMARY BACKFILL RESULT ===")
    for s in summary:
        logger.info(s)
    return 0


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
