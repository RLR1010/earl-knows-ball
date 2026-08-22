"""Backfill historical NBA active/inactive (DNP) + starter info for sid 26-35.

Source: ESPN summary endpoint
  https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={espn_game_id}
The boxscore payload lists the full per-team game roster; each athlete carries:
  * starter     (bool)  -> populates nba.player_game_stats.is_starter
  * didNotPlay  (bool)  -> populates nba.player_game_stats.dnp
  * reason      (str)   -> populates nba.player_game_stats.dnp_reason
  * athlete.id  (str)   -> matched to nba.players.espn_id
We already store the ESPN game id in nba.games.nba_game_id, so no id translation.

Design:
  * Sync psycopg2 engine + blocking requests; sequential + throttle to be gentle.
  * Checkpointed via nba.pgs_dnp_backfill_state (one row per completed game id) so a
    killed run resumes cleanly without reprocessing finished games.
  * Only mutates EXISTING nba.player_game_stats rows (matches by espn_id, then by
    name fallback). Players ESPN lists on the roster but with no pgs row are
    reported only (--report-missing) — we do NOT fabricate score rows.
  * --insert-rostered optionally inserts a bare roster row (starter/dnp only, NULL
    stats) for missing players so active/inactive coverage is complete even where
    pgs is sparse. Default OFF.

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python \
      app/scripts/backfill_nba_game_active_inactive.py            # full 10 seasons
  # options: --season 26 --min-gid 400000000 --throttle 0.15 --limit 50 --insert-rostered
"""
import argparse
import json
import logging
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

import requests
from sqlalchemy import create_engine, text

from app.db_urls import SYNC_DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("nba-active-inactive-backfill")

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
HEADERS = {"User-Agent": "Mozilla/5.0 (Earl-Knows-Ball research/1.0)"}

# season_id 26-35 == 2016/17 .. 2025/26 (sid = calendar year of season start - 1990)
MIN_SEASON, MAX_SEASON = 26, 35


def ensure_checkpoint_table(engine):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS nba.pgs_dnp_backfill_state (
                game_id        INTEGER PRIMARY KEY,
                espn_game_id   VARCHAR(20),
                season_id      INTEGER,
                processed_utc  TIMESTAMPTZ DEFAULT now(),
                inserted_rows  INTEGER NOT NULL DEFAULT 0,
                updated_rows   INTEGER NOT NULL DEFAULT 0,
                missing_rows   INTEGER NOT NULL DEFAULT 0
            )
        """))


def done_game_ids(engine):
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT game_id FROM nba.pgs_dnp_backfill_state")).scalars().all())


def pick_games(engine, season, min_gid, limit):
    q = text("""
        SELECT id AS db_game_id, nba_game_id AS espn_game_id, season_id, date::date AS game_date
        FROM nba.games
        WHERE nba_game_id IS NOT NULL
          AND season_id BETWEEN :min_s AND :max_s
          AND game_type IN ('REG','POST','PLAYIN')
        ORDER BY season_id DESC, date ASC
    """).bindparams(min_s=season if season else MIN_SEASON,
                    max_s=season if season else MAX_SEASON)
    with engine.connect() as conn:
        rows = conn.execute(q).all()
    if min_gid:
        rows = [r for r in rows if int(r.espn_game_id) >= min_gid]
    if limit:
        rows = rows[:limit]
    return rows


def load_player_map(engine):
    """espn_id (str) -> (player_id, name) and a lowercased-name fallback map."""
    espn = {}
    names = {}
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, espn_id, name FROM nba.players WHERE espn_id IS NOT NULL"
        )).all()
    for pid, eid, name in rows:
        espn.setdefault(str(eid), (pid, name))
        names.setdefault(_nk(name), (pid, name))
    return espn, names


def _nk(name):
    if not name:
        return None
    return "".join(ch for ch in name.lower() if ch.isalnum())


def fetch_roster(espn_game_id, throttle):
    time.sleep(throttle)
    r = requests.get(SUMMARY_URL, params={"event": espn_game_id}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    js = r.json()
    out = []  # list of {espn_id, name, starter, dnp, reason}
    boxscore = js.get("boxscore") or {}
    for team in boxscore.get("players", []):
        for section in team.get("statistics", []):
            for a in section.get("athletes", []):
                ath = a.get("athlete") or {}
                eid = str(ath.get("id")) if ath.get("id") is not None else None
                out.append({
                    "espn_id": eid,
                    "name": ath.get("displayName"),
                    "starter": bool(a.get("starter")),
                    "dnp": bool(a.get("didNotPlay")),
                    "reason": a.get("reason"),
                })
    return out


def process_game(engine, db_game_id, espn_game_id, season_id, player_espn, player_names,
                 insert_rostered):
    roster = fetch_roster(espn_game_id, 0.0)  # throttle handled by caller batch
    updated = inserted = missing = 0
    missing_list = []
    with engine.begin() as conn:
        # existing pgs rows for this game: nba_player_id in 1=8/9/10 digits
        existing = conn.execute(text("""
            SELECT pgs.id, pgs.player_id, p.espn_id AS espn_id, p.name AS name
            FROM nba.player_game_stats pgs
            LEFT JOIN nba.players p ON p.id = pgs.player_id
            WHERE pgs.game_id = :g
        """), {"g": db_game_id}).all()
        # index by espn_id then player_id
        row_by_espn = {}
        row_by_pid = {}
        for rid, pid, peid, pname in existing:
            if peid is not None:
                row_by_espn.setdefault(str(peid), rid)
            if pid is not None:
                row_by_pid.setdefault(str(pid), rid)

        for item in roster:
            eid = item["espn_id"]
            starter, dnp, reason = item["starter"], item["dnp"], item["reason"]
            target = None
            if eid and eid in row_by_espn:
                target = row_by_espn[eid]
            elif eid and eid in player_espn:
                pid, _ = player_espn[eid]
                if str(pid) in row_by_pid:
                    target = row_by_pid[str(pid)]
            elif item["name"]:
                pid, _ = player_names.get(_nk(item["name"]), (None, None))
                if pid is not None and str(pid) in row_by_pid:
                    target = row_by_pid[str(pid)]

            if target is not None:
                conn.execute(text(
                    "UPDATE nba.player_game_stats SET is_starter=:s, dnp=:d, dnp_reason=:r WHERE id=:id"
                ), {"s": starter, "d": dnp, "r": reason, "id": target})
                updated += 1
            else:
                missing += 1
                if len(missing_list) < 30:
                    missing_list.append(f"{item['name'] or '?'}(espn {eid})")
                if insert_rostered:
                    pid = None
                    if eid and eid in player_espn:
                        pid = player_espn[eid][0]
                    conn.execute(text("""
                        INSERT INTO nba.player_game_stats
                            (player_id, game_id, nba_game_id, is_starter, dnp, dnp_reason,
                             minutes, points)
                        VALUES (:pid, :g, :eg, :s, :d, :r, '0:00', 0)
                        ON CONFLICT DO NOTHING
                    """), {"pid": pid, "g": db_game_id, "eg": espn_game_id,
                           "s": starter, "d": dnp, "r": reason})
                    inserted += 1

        conn.execute(text("""
            INSERT INTO nba.pgs_dnp_backfill_state (game_id, espn_game_id, season_id, inserted_rows, updated_rows, missing_rows)
            VALUES (:g, :eg, :s, :ins, :upd, :miss)
            ON CONFLICT (game_id) DO UPDATE SET
                espn_game_id=EXCLUDED.espn_game_id, season_id=EXCLUDED.season_id,
                inserted_rows=EXCLUDED.inserted_rows, updated_rows=EXCLUDED.updated_rows,
                missing_rows=EXCLUDED.missing_rows, processed_utc=now()
        """), {"g": db_game_id, "eg": espn_game_id, "s": season_id,
               "ins": inserted, "upd": updated, "miss": missing})
    return updated, inserted, missing, missing_list


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None, help="single season_id to process")
    ap.add_argument("--min-gid", type=int, default=None, help="only espn_game_id >= this")
    ap.add_argument("--limit", type=int, default=None, help="max games to process this run")
    ap.add_argument("--throttle", type=float, default=0.15, help="seconds between espn calls")
    ap.add_argument("--insert-rostered", action="store_true",
                    help="insert bare roster rows (starter/dnp, NULL stats) for players with no pgs row")
    args = ap.parse_args()

    engine = create_engine(SYNC_DATABASE_URL)
    ensure_checkpoint_table(engine)
    done = done_game_ids(engine)
    games = pick_games(engine, args.season, args.min_gid, args.limit)
    player_espn, player_names = load_player_map(engine)

    todo = [g for g in games if g.db_game_id not in done]
    logger.info("games in scope=%d, already done=%d, to process=%d",
                len(games), len(games) - len(todo), len(todo))

    t_start = time.time()
    ok = err = 0
    for i, g in enumerate(todo, 1):
        d = g._mapping
        db_gid, eid, sid = d["db_game_id"], d["espn_game_id"], d["season_id"]
        try:
            updated, inserted, missing, mlist = process_game(
                engine, db_gid, str(eid), sid, player_espn, player_names, args.insert_rostered)
            ok += 1
            if i % 50 == 0 or i == len(todo):
                el = time.time() - t_start
                rate = i / el if el else 0
                logger.info("sid%s proc %d/%d (%.1f g/s) upd=%d ins=%d miss=%d err=%d elapsed=%.0fs",
                            sid, i, len(todo), rate, updated, inserted, missing, err, el)
        except Exception as e:
            err += 1
            logger.warning("FAILED gid=%s espn=%s sid=%s: %s", db_gid, eid, sid, e)
            if err >= 20:
                logger.error("too many consecutive errors, aborting")
                break
            continue
        time.sleep(args.throttle)

    logger.info("DONE ok=%d err=%d total_games=%d elapsed=%.0fs",
                ok, err, len(todo), time.time() - t_start)
    if args.limit and args.min_gid is None:
        logger.info("dry/limited run finished — rerun without --limit to continue (checkpoints skip done games)")


if __name__ == "__main__":
    main()
