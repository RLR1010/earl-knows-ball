#!/usr/bin/env python3
"""
Populate nfl.player_weekly_stats with FULL player stats (offense + defense +
special teams) from ESPN's core API (2016 → today).

Mirrors the proven NBA ingestion (`nba_player_game_stats.py`) which we fixed
for boxscore coverage accuracy. The previous NFL ingest (`ingestion/espn.py`)
only read ESPN's *site* summary boxscore, which:
  1) caps the per-category athlete arrays  -> rotation RBs/WRs dropped
     (e.g. NYG week 8 2024 missing Tyrone Tracy), causing offensive gaps that
     fail the sum-to-team check;
  2) skipped punting / kickReturns / puntReturns  -> no special-teams data;
  3) captured only a tiny defensive subset  -> no tackle family / TFL / QB hits.

This module uses the SAME core API (`sports.core.api.espn.com/v2/sports/football/
leagues/nfl`) the NBA mirror uses, and adds:
  - full-roster augmentation via `.../competitors/{comp_id}/roster` (every
    athlete incl. didNotPlay), so no player is silently dropped;
  - an extensive column set on nfl.player_weekly_stats (offense + full defensive
    tackle family + INT/FF/FR/TD + kick/punt returns + punting + kicking).

Requires the schema migration (app/ingestion/nfl_schema_migration.py) to have
added the defensive/ST columns first.

Usage:
    python app/ingestion/nfl_player_game_stats.py                 # all seasons 2016->today
    python app/ingestion/nfl_player_game_stats.py --season 2024   # one season
    python app/ingestion/nfl_player_game_stats.py --season 2024 --game-type REG

NOTE on destructuring: each team's entry in the roster's `entries[]` carries
`athlete.$ref` (which contains the numeric ESPN pid), `position.abbreviation`,
and `statistics.$ref`. For didNotPlay/bench players with no stats ref we build a
placeholder stats ref (0-row) so a row is ALWAYS created.
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time

import httpx
import psycopg2
from sqlalchemy import create_engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.db_urls import PSYCOPG2_DATABASE_URL

logger = logging.getLogger(__name__)

CORE_BASE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
USER_AGENT = "Mozilla/5.0 (Earl Knows Ball data ingest)"

# ────────────────────────────────────────────────────────────────────────────────
#  Stat name -> table column mapping (from ESPN core API category stat `name`s)
#  Format: <col> : <esps stat name>       (0 default when absent)
# ────────────────────────────────────────────────────────────────────────────────

COL_MAP = {
    # ── general ──
    "fumbles": "fumbles",
    "fumbles_lost": "fumblesLost",
    "fumbles_forced": "fumblesForced",
    "fumbles_recovered": "fumblesRecovered",
    "fumbles_recovered_yards": "fumblesRecoveredYards",
    # ── passing ──
    "pass_attempts": "passingAttempts",
    "pass_completions": "completions",
    "pass_yards": "passingYards",
    "pass_tds": "passingTouchdowns",
    "pass_int": "passing:interceptions",
    "pass_sacks": "passing:sacks",
    "pass_sack_yards": "sackYardsLost",
    "passer_rating": "QBRating",
    "long_pass": "longPassing",
    # ── rushing ──
    "rush_attempts": "rushingAttempts",
    "rush_yards": "rushingYards",
    "rush_tds": "rushingTouchdowns",
    "rush_long": "longRushing",
    "stuffs": "stuffs",
    # ── receiving ──
    "targets": "receivingTargets",
    "receptions": "receptions",
    "receiving_yards": "receivingYards",
    "receiving_tds": "receivingTouchdowns",
    "receiving_long": "longReception",
    "yards_after_catch": "receivingYardsAfterCatch",
    # ── defensive ──
    "tackles_solo": "soloTackles",
    "tackles_assist": "assistTackles",
    "tackles_combined": "totalTackles",
    "tackles_for_loss": "tacklesForLoss",
    "qb_hits": "QBHits",
    "hurries": "hurries",
    "sacks": "sacks",
    "sacks_assisted": "sacksAssisted",
    "sacks_unassisted": "sacksUnassisted",
    "safeties": "safeties",
    "passes_defended": "passesDefended",
    "passes_batted_down": "passesBattedDown",
    "interception_yards": "interceptionYards",
    "interception_tds": "interceptionTouchdowns",
    "defensive_points": "defensivePoints",
    # ── special teams: returns ──
    "kick_returns": "kickReturns",
    "kick_return_yards": "kickReturnYards",
    "kick_return_tds": "kickReturnTouchdowns",
    "long_kick_return": "longKickReturn",
    "punt_returns": "puntReturns",
    "punt_return_yards": "puntReturnYards",
    "punt_return_tds": "puntReturnTouchdowns",
    "long_punt_return": "longPuntReturn",
    # ── special teams: punting ──
    "punts": "punts",
    "punt_yards": "puntYards",
    "avg_punt_yards": "grossAvgPuntYards",
    "long_punt": "longPunt",
    "punts_inside_20": "puntsInside20",
    "punts_inside_10": "puntsInside10",
    "punts_over_50": "puntsOver50",
    "touchbacks_punting": "touchbacks",
    "fair_catches": "fairCatches",
    # ── special teams: kicking (from kicking category) ──
    "field_goals_made": "fieldGoalsMade",
    "field_goals_attempted": "fieldGoalAttempts",
    "long_field_goal": "longFieldGoalMade",
    "extra_points_made": "extraPointsMade",
    "extra_points_attempted": "extraPointAttempts",
    # ── defensive touchdowns (already present) / interceptions (already present) ──
    "interceptions": "interceptions",
    "defensive_tds": "defensiveTouchdowns",
}

# Column names we write (order used in the INSERT). Presence-conditional kept so
# the ON CONFLICT handles all of them.
ALL_COLS = list(COL_MAP.keys())


def _extract_stat_value(stat: dict) -> float | int | None:
    """Pull a numeric value from an ESPN core-API stat object (name/value/displayValue)."""
    if not isinstance(stat, dict):
        return None
    for key in ("value", "displayValue"):
        v = stat.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return v
        s = str(v).replace(",", "").strip()
        if s in ("", "-", "--", "n/a"):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    # percentile / shorthand may carry ints in 'abbreviation'
    pct = stat.get("abbreviation")
    if isinstance(pct, (int, float)):
        return pct
    return None


def _sv(merged: dict, stat_name: str) -> float | int | None:
    """Look up a stat by its ESPN name in the merged flat dict."""
    v = merged.get(stat_name)
    return _extract_stat_value(v) if isinstance(v, dict) else v


def _athlete_pid(athlete_ref: str | None) -> int | None:
    """Extract the numeric ESPN pid from an athlete $ref (…/athletes/12345?...)."""
    if not athlete_ref:
        return None
    m = re.search(r"/athletes/(\d+)", athlete_ref)
    return int(m.group(1)) if m else None


async def _fetch_json(client: httpx.AsyncClient, url: str, retries: int = 3,
                      backoff: float = 0.8) -> dict:
    """GET a JSON endpoint with basic retry + rate-limit backoff."""
    last = None
    for attempt in range(retries):
        try:
            r = await client.get(url, headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", "1")) or backoff * (attempt + 1)
                logger.info("429 on %s — sleeping %.1fs", url, wait)
                await asyncio.sleep(wait)
            else:
                last = f"HTTP {r.status_code}"
                await asyncio.sleep(backoff * (attempt + 1))
        except httpx.HTTPError as e:
            last = str(e)
            await asyncio.sleep(backoff * (attempt + 1))
    logger.warning("Failed to fetch %s after %d retries (%s)", url, retries, last)
    return {}


async def _match_and_save_espn_id(conn, pid: int, display_name: str) -> int | None:
    """Return the db player_id for an ESPN pid, creating a minimal player if
    unknown (so no athlete is ever dropped). Saves espn_id on match."""
    with conn.cursor() as cur:
        # 1) already mapped?
        cur.execute(
            "SELECT id FROM nfl.players WHERE espn_id = %s", (pid,))
        row = cur.fetchone()
        if row:
            return row[0]
        # 2) match by normalized name (best-effort) — CONSERVATIVE: only when the
        #    name matches EXACTLY ONE espn_id-less (canonical) player. If two
        #    canonical players share the name, do NOT guess (that's the risk Rich
        #    flagged) — fall through to placeholder creation and let the remap
        #    linker / manual review decide.
        if display_name:
            cur.execute(
                "SELECT id FROM nfl.players "
                "WHERE espn_id IS NULL AND LOWER(replace(name,'.','')) = LOWER(replace(%s,'.',''))",
                (display_name,))
            rows = cur.fetchall()
            if len(rows) == 1:
                cur.execute(
                    "UPDATE nfl.players SET espn_id = %s WHERE id = %s", (pid, rows[0][0]))
                conn.commit()
                return rows[0][0]
            if len(rows) > 1:
                # ambiguous duplicate-name -> do NOT arbitrarily assign; fall through
                # to placeholder so no wrong espn_id is attached to a same-named player.
                pass
        # 3) unknown -> create minimal player row (never drop)
        cur.execute(
            "INSERT INTO nfl.players (name, position, espn_id, team_id) "
            "VALUES (%s, 'UNK', %s, NULL) ON CONFLICT (espn_id) DO NOTHING RETURNING id",
            (display_name or f"ESPN-{pid}", pid))
        row = cur.fetchone()
        conn.commit()
        if row:
            return row[0]
        # conflict race -> refetch
        cur.execute("SELECT id FROM nfl.players WHERE espn_id = %s", (pid,))
        row = cur.fetchone()
        return row[0] if row else None


async def _fetch_athlete(client: httpx.AsyncClient, ref: str) -> dict:
    """GET an athlete's statistics $ref, merging ALL category stats into one flat dict.

    Category order from ESPN: general, passing, rushing, receiving, defensive,
    defensiveInterceptions, kicking, returning, punting, scoring. Two stat NAMES
    collide across categories: `sacks` (passing = sacks taken by QB; defensive =
    sacks earned) and `interceptions` (passing = INTs thrown; defensiveInterceptions
    = INTs caught). For the columns we store (sacks, interceptions) the DEFENSIVE
    meaning is what we want, so this merge gives defensive priority for those two.
    """
    d = await _fetch_json(client, ref)
    merged = {}
    for group in d.get("splits", {}).get("categories", []) or []:
        cat = group.get("name")
        for stat in group.get("stats", []) or []:
            name = stat.get("name")
            if not name:
                continue
            if name in ("sacks", "interceptions"):
                # These collision names mean DEFENSIVE stats in our schema (sacks
                # earned, INTs caught). Only source them from the defensive
                # categories; a QB's passing "sacks" (taken) / "interceptions"
                # (thrown) must NOT leak into the columns — they're captured via
                # the namespaced {cat}:{name} keys feed pass_sacks/pass_int.
                if cat in ("defensive", "defensiveInterceptions"):
                    merged[name] = stat
            else:
                merged[name] = stat
            # ALSO store a category-namespaced copy so a column can target the
            # PASSING category value for a colliding name (e.g. QB sacks taken,
            # passing INTs). Format: f"{cat}:{name}" e.g. "passing:sacks".
            merged.setdefault(f"{cat}:{name.replace(' ', '')}", stat)
    return merged


async def _process_game(client: httpx.AsyncClient, conn, gid: int, gap_fill: bool) -> int:
    """Ingest one game's full player stats. Returns number of rows upserted."""
    # Game metadata from our DB (id, season_id, week, game_type, both team ids).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT season_id, week, game_type, home_team_id, away_team_id "
            "FROM nfl.games WHERE id = %s", (gid,))
        grow = cur.fetchone()
    if not grow:
        logger.warning("Game %s not in nfl.games; skipping", gid)
        return 0
    season_id, week, gtype, home_team_id, away_team_id = grow

    # competitors (both teams)
    comp_url = f"{CORE_BASE}/events/{gid}/competitions/{gid}/competitors"
    comps = await _fetch_json(client, comp_url)
    comp_items = comps.get("items", [])
    if not comp_items:
        logger.warning("No competitors for game %s", gid)
        return 0

    # Resolve each competitor's espn team id -> db team_id via its season-
    # specific team doc abbreviation, normalized through ALIASES for relocations
    # and name changes (e.g. WSH->WAS, STL->LAR, SD->LAC, OAK->LV). Correct for
    # every era 2016+ while ESPN team ids stay stable.
    # module-level cache espn-id -> db team id
    ALIASES = {"WSH": "WAS", "STL": "LAR", "SD": "LAC", "OAK": "LV", "LA": "LAR"}
    # resolve alias -> db abbr -> db id caches once per process
    if not hasattr(_process_game, "_db_abbr_to_id"):
        with conn.cursor() as cur:
            cur.execute("SELECT id, abbreviation FROM nfl.teams")
            _process_game._db_abbr_to_id = {r[1].upper(): r[0] for r in cur.fetchall()}
    if not hasattr(_process_game, "_espid_cache"):
        _process_game._espid_cache = {}
    db_abbr_to_id = _process_game._db_abbr_to_id
    espid_cache = _process_game._espid_cache

    espid_to_dbid = {}
    for it in comp_items:
        cid = it.get("id")
        if cid in espid_cache:
            espid_to_dbid[cid] = espid_cache[cid]
            continue
        tref = (it.get("team") or {}).get("$ref")
        abbr = ""
        if tref:
            abbr = ((await _fetch_json(client, tref)).get("abbreviation") or "").upper()
        db_abbr = ALIASES.get(abbr, abbr)
        db_id = db_abbr_to_id.get(db_abbr)
        if not db_id:
            logger.warning("Game %s: no db team_id for espn id %s (abbr=%r)", gid, cid, abbr)
            return 0
        espid_cache[cid] = db_id
        espid_to_dbid[cid] = db_id

    # Sanity: every competitor's espn id must resolve.
    for it in comp_items:
        cid = it.get("id")
        if cid not in espid_to_dbid or not espid_to_dbid[cid]:
            tref = (it.get("team") or {}).get("$ref")
            abbr = (await _fetch_json(client, tref)).get("abbreviation", "") if tref else ""
            logger.warning("Game %s: no db team_id for espn id %s (abbr=%r)", gid, cid, abbr)
            return 0

    # Collect athletes: pid -> (stats_ref, team_id, opponent_id)
    athlete_refs: dict[int, tuple[str, int, int]] = {}
    for it in comp_items:
        cid = it.get("id")
        t_id = espid_to_dbid[cid]
        # opponent = the OTHER competitor's db team id
        o_id = next((espid_to_dbid[o["id"]] for o in comp_items if o.get("id") != cid), None)
        if o_id is None:
            logger.warning("Game %s: could not determine opponent for %s", gid, cid)
            return 0

        # full roster — EVERY athlete incl. didNotPlay. Retry harder + guard
        # against a silently-empty roster (that would drop a whole team's data).
        roster_url = f"{CORE_BASE}/events/{gid}/competitions/{gid}/competitors/{cid}/roster"
        roster = {}  # empty-sentinel so we can distinguish "failed" from 0
        for _attempt in range(5):
            roster = await _fetch_json(client, roster_url)
            if roster.get("entries"):
                break
            await asyncio.sleep(1.5 * (_attempt + 1))
        entries = roster.get("entries", []) or []
        if not entries:
            # A whole team's roster came back empty — this is a HARD failure.
            # Abort the game rather than write a partial box (breaks sum-to-team).
            raise RuntimeError(
                f"Game {gid}: roster for competitor {cid} (espn team {t_id}) "
                f"returned 0 entries after retries — refusing to write partial data")
        for entry in entries:
            pid = _athlete_pid((entry.get("athlete") or {}).get("$ref"))
            if pid is None:
                pid = entry.get("playerId") or _athlete_pid(str(entry.get("athlete")))
            if pid is None:
                continue
            sref = entry.get("statistics") or {}
            stats_ref = sref.get("$ref") if isinstance(sref, dict) else sref
            # NOTE: when a roster entry has NO stats ref (bench/didNotPlay), we
            # set stats_ref=None and write a plain zero-row WITHOUT an HTTP fetch
            # (a placeholder URL would 404 and needlessly retry).
            disp = entry.get("displayName") or entry.get("athleteDisplayName")
            if pid not in athlete_refs:
                athlete_refs[pid] = (stats_ref, t_id, o_id, disp)

        # category statistics — backup to roster (usually a subset)
        stats_url = f"{CORE_BASE}/events/{gid}/competitions/{gid}/competitors/{cid}/statistics"
        cats = await _fetch_json(client, stats_url)
        for cat in cats.get("categories", []) or []:
            for a in cat.get("athletes", []) or []:
                ref = a.get("$ref") if isinstance(a, dict) else None
                pid = ref.split("/athletes/")[-1].split("/")[0] if ref else None
                if pid and pid.isdigit() and int(pid) not in athlete_refs:
                    athlete_refs[int(pid)] = (ref, t_id, o_id, None)

    if not athlete_refs:
        logger.warning("No athletes resolved for game %s", gid)
        return 0

    # DELETE gate: unless gap-filling, clear existing rows so a re-ingest
    # reflects the FULL current data (never leave stale partial rows).
    if not gap_fill:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM nfl.player_weekly_stats WHERE game_id = %s", (gid,))
            conn.commit()

    # ── CONCURRENT stat fetch ── prefetch every athlete's merged stats in
    # parallel (capped) to avoid serial HTTP latency; then write rows serially.
    import asyncio as _asyncio

    async def _one(pid, ref):
        if ref:
            m = await _fetch_athlete(client, ref)
        else:
            m = {}  # bench/didNotPlay -> zero-row
        return pid, {col: _sv(m, stat_name) for col, stat_name in COL_MAP.items()}

    items = list(athlete_refs.items())
    sem = _asyncio.Semaphore(16)

    async def _capped(pid, ref):
        async with sem:
            return await _one(pid, ref)

    merged_map = {}
    for i in range(0, len(items), 64):
        chunk = items[i:i + 64]
        results = await _asyncio.gather(*(_capped(pid, ref) for pid, (ref, *_t) in chunk))
        for pid, vals in results:
            merged_map[pid] = vals

    # ── serial DB write ──
    inserted = 0
    for pid, (ref, t_id, o_id, disp) in athlete_refs.items():
        vals = merged_map[pid]

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, position FROM nfl.players WHERE espn_id = %s", (pid,))
            prow = cur.fetchone()
        if prow:
            db_pid, name, pos = prow
        else:
            db_pid = await _match_and_save_espn_id(conn, pid, disp or ("ESPN-" + str(pid)))
            name, pos = None, None

        if db_pid is None:
            logger.warning("Could not resolve pid %s -> db player; skipped", pid)
            continue

        cols = (["player_id", "game_id", "season_id", "week", "game_type",
                 "team_id", "opponent_id"] + ALL_COLS)
        placeholders = ", ".join(f"%({c})s" for c in cols)
        update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in ALL_COLS)
        sql = (
            f"INSERT INTO nfl.player_weekly_stats ({', '.join(cols)}) "
            f"VALUES ({placeholders}) ON CONFLICT (player_id, game_id) "
            f"DO UPDATE SET {update_set}"
        )
        row_data = {c: vals.get(c) for c in ALL_COLS}
        row_data.update({
            "player_id": db_pid, "game_id": gid, "season_id": season_id,
            "week": week, "game_type": gtype, "team_id": t_id,
            "opponent_id": o_id,
        })
        with conn.cursor() as cur:
            cur.execute(sql, row_data)
            inserted += cur.rowcount

    # Single commit for the whole game (batch — avoids 130+ commits/game).
    conn.commit()
    return inserted


async def _ingest_season(client: httpx.AsyncClient, conn, year: int,
                         game_type: str, gap_fill: bool) -> int:
    # which games in this season (REG or POST) are FINAL and exist in our DB
    with conn.cursor() as cur:
        cur.execute(
            "SELECT g.id FROM nfl.games g "
            "JOIN nfl.seasons s ON s.id = g.season_id "
            "WHERE s.year = %s AND g.game_type = %s AND g.status = 'FINAL'",
            (year, game_type))
        game_ids = [r[0] for r in cur.fetchall()]
    logger.info("%s %s: %d games to ingest", year, game_type, len(game_ids))
    total = 0
    for i, gid in enumerate(game_ids, 1):
        try:
            n = await _process_game(client, conn, gid, gap_fill)
            total += n
            if i % 5 == 0 or i == len(game_ids):
                logger.info("  %s %s: game %d/%d (%d rows so far)", year, game_type, i, len(game_ids), total)
        except Exception as e:  # keep going across games
            logger.error("Game %s failed: %s", gid, e)
        await asyncio.sleep(0.4)  # rate-limit politeness
    return total


async def _run(seasons: list[int] | None, game_type: str = "REG", gap_fill: bool = False) -> None:
    engine = create_engine(PSYCOPG2_DATABASE_URL, pool_pre_ping=True)
    conn = engine.raw_connection()
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
        for year in seasons or []:
            n = await _ingest_season(client, conn, year, game_type, gap_fill)
            logger.info("season %s %s done: %d player rows", year, game_type, n)
    conn.close()
    engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    # Quiet the noisy per-request httpx INFO logger (only warnings/errors).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None, help="Single season year (default 2016->current)")
    ap.add_argument("--game-type", default="REG", choices=["REG", "POST", "PRE"])
    ap.add_argument("--gap-fill", action="store_true",
                    help="Do not DELETE existing rows before update (safe re-run)")
    args = ap.parse_args()

    with psycopg2.connect(PSYCOPG2_DATABASE_URL) as c:
        cur = c.cursor()
        cur.execute("SELECT MAX(year) FROM nfl.seasons")
        max_year = cur.fetchone()[0] or 2025
    seasons = [args.season] if args.season else list(range(2016, max_year + 1))
    print(f"Ingesting NFL player stats: seasons={seasons} game_type={args.game_type} gap_fill={args.gap_fill}")
    asyncio.run(_run(seasons, args.game_type, args.gap_fill))


if __name__ == "__main__":
    main()
