"""
NBA team game boxscore stats ingestion from ESPN's core API team-statistics
endpoint.

The nba.player_game_stats ingest captures per-player stats. This module captures
the TEAM-level stats that either (a) can't be derived by summing players
(estimatedPossessions, pointsInPaint, fastBreakPoints, turnoverPoints,
teamTurnovers/totalTurnovers split, leadChanges, largestLead, leadPercentage,
doubleDouble/tripleDouble team counts, technical/flagrant fouls, ejections,
disqualifications, NBARating, VORP, pre-computed ratios, scoring/shooting
efficiency, brickIndex, offensiveReboundPct...) or (b) are authoritative at the
team level from ESPN (real offensiveRebounds / defensiveRebounds — previously we
only stored total rebounds and approximated ORB with a 0.245 proxy).

Results are written to the nba.games home_*/away_* box-score columns added by
migrations/20260816_nba_game_boxscore_team_stats.sql.

Field mapping: ESPN stat name (camelCase) -> nba.games column suffix (snake_case,
without the home_/away_ prefix). All teams get both home_ and away_ variants.
"""

import asyncio
import logging
import re

import httpx
from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("nba-team-stats")

DB_URL = PSYCOPG2_DATABASE_URL
CORE_BASE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.nba.com/",
}

# ESPN stat name -> nba.games column suffix (no home_/away_ prefix). Float cast
# when the displayValue is a ratio/pct; int otherwise.
STAT_COLUMN_MAP = {
    # offensive
    "points": "points",
    "fieldGoalsMade": "field_goals_made",
    "fieldGoalsAttempted": "field_goals_attempted",
    "fieldGoalPct": "field_goal_pct",
    "threePointFieldGoalsMade": "three_points_made",
    "threePointFieldGoalsAttempted": "three_points_attempted",
    "threePointFieldGoalPct": "three_point_pct",
    "freeThrowsMade": "free_throws_made",
    "freeThrowsAttempted": "free_throws_attempted",
    "freeThrowPct": "free_throw_pct",
    "twoPointFieldGoalsMade": "two_point_field_goals_made",
    "twoPointFieldGoalsAttempted": "two_point_field_goals_attempted",
    "twoPointFieldGoalPct": "two_point_field_goal_pct",
    "offensiveRebounds": "offensive_rebounds",
    "assists": "assists",
    "turnovers": "turnovers",
    "teamTurnovers": "team_turnovers",
    "totalTurnovers": "total_turnovers",
    "pointsInPaint": "points_in_paint",
    "fastBreakPoints": "fast_break_points",
    "offensiveReboundPct": "offensive_rebound_pct",
    "estimatedPossessions": "estimated_possessions",
    "pointsPerEstimatedPossessions": "points_per_estimated_possessions",
    "scoringEfficiency": "scoring_efficiency",
    "shootingEfficiency": "shooting_efficiency",
    "brickIndex": "brick_index",
    "fieldGoalsThatMadePossession": "field_goals_that_made_possession",
    # defensive
    "blocks": "blocks",
    "defensiveRebounds": "defensive_rebounds",
    "steals": "steals",
    "turnoverPoints": "turnover_points",
    # general
    "largestLead": "largest_lead",
    "leadChanges": "lead_changes",
    "leadPercentage": "lead_percentage",
    "disqualifications": "disqualifications",
    "flagrantFouls": "flagrant_fouls",
    "fouls": "fouls",
    "ejections": "ejections",
    "technicalFouls": "technical_fouls",
    "rebounds": "rebounds",
    "VORP": "vorp",
    "NBARating": "nba_rating",
    "assistTurnoverRatio": "assist_turnover_ratio",
    "stealFoulRatio": "steal_foul_ratio",
    "blockFoulRatio": "block_foul_ratio",
    "teamAssistTurnoverRatio": "team_assist_turnover_ratio",
    "stealTurnoverRatio": "steal_turnover_ratio",
    "doubleDouble": "double_double",
    "tripleDouble": "triple_double",
}

# Columns that hold fractional values -> cast float; rest cast int.
FLOAT_COLS = {
    "field_goal_pct", "three_point_pct", "free_throw_pct", "two_point_field_goal_pct",
    "offensive_rebound_pct", "estimated_possessions", "points_per_estimated_possessions",
    "scoring_efficiency", "shooting_efficiency", "brick_index",
    "field_goals_that_made_possession", "lead_percentage", "vorp", "nba_rating",
    "assist_turnover_ratio", "steal_foul_ratio", "block_foul_ratio",
    "team_assist_turnover_ratio", "steal_turnover_ratio",
}

ALT_ABBR = {
    "GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "PHO": "PHX",
    "BK": "BKN", "UTAH": "UTA", "WSH": "WAS",
    # Charlotte: ESPN core API returns CHO; our DB stores CHA.
    "CHO": "CHA",
}


def _norm(s: str) -> str:
    """Normalize a team name/abbr for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _extract_stat_value(stats: list[dict], name: str):
    """Extract a named stat's displayValue from a stats list."""
    for s in stats:
        if s.get("name") == name:
            return s.get("displayValue")
    return None


def _to_col_val(stat_name: str, display_value) -> object:
    """Coerce a displayValue to the proper Python type for the target column."""
    if display_value is None:
        return None
    col = STAT_COLUMN_MAP[stat_name]
    if col in FLOAT_COLS:
        try:
            return float(display_value)
        except (TypeError, ValueError):
            return None
    try:
        return int(float(display_value))
    except (TypeError, ValueError):
        return None


def build_db_team_map(db_conn) -> dict:
    """Build {db_abbr: db_abbr} plus name keys so we resolve ESPN abbr/name to
    the DB abbreviation in one pass. Keys are normalized (lowercased, no junk).
    """
    m: dict = {}
    rows = db_conn.execute(text("SELECT abbreviation, name FROM nba.teams")).fetchall()
    for abbr, name in rows:
        m[abbr.upper()] = abbr
        m[_norm(abbr)] = abbr
        if name:
            m[_norm(name)] = abbr
            # also index contained city/mascot-ish tokens for fuzz
            for tok in _norm(name).split():
                if len(tok) >= 3:
                    m.setdefault(tok, abbr)
    return m


def _resolve_team(db_team_map: dict, espn_abbr: str, espn_name: str = "") -> str:
    """Resolve ESPN abbr/name to a DB abbreviation via the prebuilt map."""
    abbr = ALT_ABBR.get(espn_abbr, espn_abbr)
    if abbr.upper() in db_team_map:
        return db_team_map[abbr.upper()]
    if espn_name:
        key = _norm(espn_name)
        if key in db_team_map:
            return db_team_map[key]
        for tok in key.split():
            if len(tok) >= 3 and tok in db_team_map:
                return db_team_map[tok]
        for cand, db_abbr in db_team_map.items():
            if db_abbr and (key in cand or cand in key):
                return db_abbr
    return None


async def fetch_team_stats(client: httpx.AsyncClient, espn_game_id: str,
                           db_team_map: dict = None) -> dict:
    """
    Fetch the per-team statistics for one game.

    Returns {home_db_abbr: {col: val, ...}, away_db_abbr: {col: val, ...}}
    keyed by DB team abbreviation (normalized), where col is the nba.games
    column suffix and val is a bool/int/float. Only the stats present in the
    payload are included. Returns {"home": None, "away": None} on failure.
    """
    result = {"home": None, "away": None}

    # Competitor list
    comp_url = f"{CORE_BASE}/events/{espn_game_id}/competitions/{espn_game_id}/competitors"
    try:
        resp = await client.get(comp_url, timeout=15)
        resp.raise_for_status()
        comp_data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("competitors fetch failed for %s: %s", espn_game_id, exc)
        return result

    competitors = []
    for item in comp_data.get("items", []):
        ref = item.get("$ref", "")
        try:
            r2 = await client.get(ref, timeout=10)
            if r2.status_code != 200:
                continue
            c = r2.json()
            competitors.append({
                "comp_id": int(ref.split("/")[-1].split("?")[0]),
                "home_away": c.get("homeAway", ""),
                "team_ref": c.get("team", {}).get("$ref", ""),
            })
        except Exception:  # noqa: BLE001
            continue

    if not competitors:
        return result

    # Map each competitor to a DB abbreviation
    for comp in competitors:
        team_ref = comp["team_ref"]
        if not team_ref:
            continue
        try:
            r3 = await client.get(team_ref, timeout=10)
            if r3.status_code != 200:
                continue
            t = r3.json()
            abbr = t.get("abbreviation", "")
            tname = t.get("shortName") or t.get("displayName") or t.get("name") or ""
            if not db_team_map:
                abbr = ALT_ABBR.get(abbr, abbr)
            else:
                abbr = _resolve_team(db_team_map, abbr, tname) or abbr
        except Exception:  # noqa: BLE001
            continue

        # Fetch the team statistics endpoint
        stats_url = (
            f"{CORE_BASE}/events/{espn_game_id}/competitions/{espn_game_id}"
            f"/competitors/{comp['comp_id']}/statistics"
        )
        try:
            sr = await client.get(stats_url, timeout=15)
            if sr.status_code != 200:
                continue
            sd = sr.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("team stats fetch failed comp=%s: %s", comp["comp_id"], exc)
            continue

        # Flatten all categories' stats into one {stat_name: displayValue} map
        merged = {}
        for cat in sd.get("splits", {}).get("categories", []):
            for s in cat.get("stats", []):
                merged[s.get("name")] = s.get("displayValue")

        # Build column->value for all mapped stats that are present
        col_vals = {}
        for stat_name, col in STAT_COLUMN_MAP.items():
            if stat_name in merged:
                col_vals[col] = _to_col_val(stat_name, merged[stat_name])

        if comp["home_away"] == "home":
            result["home"] = (abbr, col_vals)
        else:
            result["away"] = (abbr, col_vals)

    return result


def _existing_box_cols(db_conn) -> set:
    """Return the set of home_*/away_* column names currently on nba.games.

    Writes are filtered against this so we never try to UPDATE a column that
    does not exist (e.g. some stats in STAT_COLUMN_MAP map to legacy columns
    that don't exist, or aren't needed). Called once per process.
    """
    rows = db_conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='nba' AND table_name='games' "
            "AND (column_name LIKE 'home_%' OR column_name LIKE 'away_%')"
        )
    ).fetchall()
    return {r[0] for r in rows}


async def process_game(client: httpx.AsyncClient, db_conn, espn_game_id: str, db_game_id: int,
                       valid_cols: set = None, db_team_map: dict = None) -> int:
    """
    Fetch ESPN team stats for a game and persist them to nba.games.
    Writes are limited to columns present in `valid_cols` (defaults to a fresh
    query of existing nba.games box-score columns). `db_team_map` resolves
    ESPN team identities to DB abbreviations (see build_db_team_map).
    Returns number of sides updated (0, 1, or 2).
    """
    if valid_cols is None:
        valid_cols = _existing_box_cols(db_conn)
    if db_team_map is None:
        db_team_map = build_db_team_map(db_conn)
    stats = await fetch_team_stats(client, espn_game_id, db_team_map)
    updates = []

    for slot, side in (("home", stats["home"]), ("away", stats["away"])):
        if not side:
            continue
        abbr, col_vals = side
        if not col_vals:
            continue
        # Verify the abbr matches the DB side to avoid writing to the wrong row
        side_col = "home_team_id" if slot == "home" else "away_team_id"
        row = db_conn.execute(
            text(
                f"SELECT t.abbreviation FROM nba.games g JOIN nba.teams t ON t.id = g.{side_col} "
                "WHERE g.id = :gid"
            ),
            {"gid": db_game_id},
        ).fetchone()
        if not row or (row[0] != abbr):
            logger.warning(
                "abbr mismatch for game %s %s side: ESPN=%s DB=%s (skipping)",
                db_game_id, slot, abbr, row[0] if row else None,
            )
            continue

        sets = []
        params = {"gid": db_game_id}
        for col, val in col_vals.items():
            full_col = f"{slot}_{col}"
            if full_col not in valid_cols:
                # Skip stats whose target column doesn't exist (legacy overlap).
                continue
            key = f"v_{col}"
            sets.append(f"{full_col} = :{key}")
            params[key] = val
        if sets:
            updates.append((sets, params))

    if not updates:
        return 0

    for sets, params in updates:
        db_conn.execute(
            text(f"UPDATE nba.games SET {', '.join(sets)} WHERE id = :gid"),
            params,
        )
    return len(updates)


async def run_for_games(espn_ids_to_db: dict, throttle: float = 0.15):
    """Process a {espn_game_id: db_game_id} map with throttle between games."""
    engine = create_engine(DB_URL.replace("+asyncpg", "+psycopg2"))
    updated = 0
    errors = 0
    with engine.connect() as conn:
        valid_cols = _existing_box_cols(conn)
        db_team_map = build_db_team_map(conn)
        async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
            for espn_id, db_id in espn_ids_to_db.items():
                try:
                    n = await process_game(client, conn, str(espn_id), db_id, valid_cols, db_team_map)
                    updated += n
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    errors += 1
                    logger.error("game %s (db %s) failed: %s", espn_id, db_id, exc)
                    conn.rollback()
                await asyncio.sleep(throttle)
    logger.info("Done: %d sides updated, %d errors across %d games", updated, errors, len(espn_ids_to_db))
    return updated, errors
