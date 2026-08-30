"""
Standalone NBA player game stats ingestion.

Fetches per-athlete boxscore data from ESPN's core API for every FINAL
regular season game in a given season, then stores it in nba.player_game_stats.

Approach:
  1. For each NBA game in DB, fetch competitor list from ESPN core API
  2. For each competitor, fetch the per-athlete statistics
  3. Match athletes to our DB players (by espn_id or name)
  4. Insert into nba.player_game_stats
"""

import asyncio
import logging
import re
from typing import Optional

import httpx
from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
# Suppress httpx info logging (very verbose with per-request logging)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("nba-pgl-stats")

DB_URL = PSYCOPG2_DATABASE_URL  # from app.db_urls
CORE_BASE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.nba.com/",
}


def _extract_stat_value(stats: list[dict], name: str):
    """Extract a named stat's displayValue from a stats list."""
    for s in stats:
        if s.get("name") == name:
            dv = s.get("displayValue", "")
            if not dv:
                return None
            try:
                if name == "minutes":
                    return dv
                elif name == "fieldGoalPct":
                    return float(dv)
                elif name == "threePointFieldGoalPct":
                    return float(dv)
                elif name == "freeThrowPct":
                    return float(dv)
                elif name in ("plusMinus",):
                    return float(dv)
                else:
                    return int(float(dv))
            except (ValueError, TypeError):
                return None
    return None


# ── Name Matching ────────────────────────────────────────────────────────────

def match_and_save_espn_id(espn_id: int, athlete_name: str, db_conn) -> Optional[int]:
    """Try to match an ESPN athlete to our DB by name, save espn_id if found."""
    if not athlete_name:
        return None

    # Try exact match first
    row = db_conn.execute(
        text("SELECT id FROM nba.players WHERE LOWER(name) = LOWER(:name) LIMIT 1"),
        {"name": athlete_name},
    ).fetchone()
    if row:
        pid = row[0]
        db_conn.execute(
            text("UPDATE nba.players SET espn_id = :eid WHERE id = :pid AND espn_id IS NULL"),
            {"eid": espn_id, "pid": pid},
        )
        db_conn.commit()
        return pid

    # STRICT normalized exact-name match only.
    # NO last-name-only or first-initial matching: those collapsed distinct players
    # (e.g. assigned a star's espn_id onto every same-last-name player) and caused the
    # espn_cache collisions / phantom statlines that were cleaned in the 2026-08-16 rebuild.
    target = _normalize_name(athlete_name)
    if not target:
        return None
    cands = db_conn.execute(
        text("""
            SELECT id FROM nba.players
            WHERE lower(regexp_replace(name, '\\s+.*', '')) = :first
               OR name ILIKE :target
            ORDER BY LENGTH(name) LIMIT 50
        """),
        {"first": target.split()[0] if target.split() else '', "target": f"%{target}%"},
    ).fetchall()
    for (pid,) in cands:
        cname = db_conn.execute(text("SELECT name FROM nba.players WHERE id=:p"), {"p": pid}).fetchone()[0]
        if _normalize_name(cname) == target:
            db_conn.execute(
                text("UPDATE nba.players SET espn_id = :eid WHERE id = :pid AND espn_id IS NULL"),
                {"eid": espn_id, "pid": pid},
            )
            db_conn.commit()
            return pid
    # No exact match: do NOT auto-assign to a same-last-name player. Return None so the
    # caller can decide (skip or create a dedicated row) rather than mis-attribute.
    return None


def _normalize_name(name: str) -> str:
    """Normalize a player name for exact comparison: lowercase, strip accents,
    punctuation, suffixes (Jr/Sr/II/III/IV) and collapse whitespace."""
    if not name:
        return ""
    import unicodedata as _uc
    n = _uc.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    n = n.lower()
    for suffix in (" jr", " sr", " ii", " iii", " iv"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    n = "".join(c for c in n if c.isalnum() or c.isspace())
    return " ".join(n.split())


# ── Athlete Info ─────────────────────────────────────────────────────────────

async def get_athlete_name(client: httpx.AsyncClient, stats_ref: str) -> str:
    """Fetch athlete display name from the stats response."""
    try:
        r = await client.get(stats_ref, timeout=10)
        if r.status_code == 200:
            d = r.json()
            athlete_ref = d.get("athlete", {}).get("$ref", "")
            if athlete_ref:
                r2 = await client.get(athlete_ref, timeout=10)
                if r2.status_code == 200:
                    return r2.json().get("displayName", "")
    except Exception:
        pass
    return ""


# ── Single Game Processor ────────────────────────────────────────────────────

async def process_game(
    client: httpx.AsyncClient,
    db_conn,
    espn_game_id: str,
    db_game_id: int,
    home_abbr: str,
    away_abbr: str,
    espn_cache: dict,
) -> int:
    """Fetch and insert player stats for a single game. Returns rows inserted."""
    # Step 1: Get competitors
    comp_url = f"{CORE_BASE}/events/{espn_game_id}/competitions/{espn_game_id}/competitors"
    try:
        resp = await client.get(comp_url, timeout=15)
        resp.raise_for_status()
        comp_data = resp.json()
    except Exception:
        return 0

    # Map competitor_id -> {team_abbr, home/away}
    competitors = {}
    for item in comp_data.get("items", []):
        ref = item.get("$ref", "")
        comp_id = int(ref.split("/")[-1].split("?")[0])
        try:
            r2 = await client.get(ref, timeout=10)
            if r2.status_code == 200:
                c = r2.json()
                home_away = c.get("homeAway", "")
                team_ref = c.get("team", {}).get("$ref", "")
                if team_ref:
                    r3 = await client.get(team_ref, timeout=10)
                    if r3.status_code == 200:
                        t = r3.json()
                        espn_abbr = t.get("abbreviation", "")
                        competitors[comp_id] = {
                            "abbr": espn_abbr,
                            "home_away": home_away,
                        }
        except Exception:
            continue

    if not competitors:
        return 0

    # Step 2: Map competitors to DB team IDs
    abbr_to_db = {}
    for abbr in list(set(c["abbr"] for c in competitors.values())):
        db_id = db_conn.execute(
            text("SELECT id FROM nba.teams WHERE abbreviation = :abbr"),
            {"abbr": abbr},
        ).fetchone()
        if db_id:
            abbr_to_db[abbr] = db_id[0]
    # Handle alt abbreviations
    alt_map = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP",
               "PHO": "PHX", "BK": "BKN", "UTAH": "UTA", "CHA": "CHO",
               "NOH": "NOP", "NOK": "NOP", "NJ": "NJN", "SEA": "SEA", "VAN": "VAN",
               "WSH": "WAS"}
    for espn_abbr, db_abbr in alt_map.items():
        if espn_abbr in abbr_to_db:
            continue
        db_id = db_conn.execute(
            text("SELECT id FROM nba.teams WHERE abbreviation = :abbr"),
            {"abbr": db_abbr},
        ).fetchone()
        if db_id:
            abbr_to_db[espn_abbr] = db_id[0]

    # Step 3: Fetch athlete stats per competitor
    total_rows = 0
    for comp_id, info in competitors.items():
        team_abbr = info["abbr"]
        db_team_id = abbr_to_db.get(team_abbr)
        if not db_team_id:
            continue

        stats_url = f"{CORE_BASE}/events/{espn_game_id}/competitions/{espn_game_id}/competitors/{comp_id}/statistics"
        try:
            resp = await client.get(stats_url, timeout=15)
            if resp.status_code != 200:
                continue
        except Exception:
            continue

        data = resp.json()
        categories = data.get("splits", {}).get("categories", [])

        # Collect athlete refs (deduplicated)
        athlete_refs = {}
        for cat in categories:
            for entry in cat.get("athletes", []):
                stats_ref = entry.get("statistics", {}).get("$ref", "")
                if stats_ref:
                    aid = int(stats_ref.split("/")[-3])
                    if aid not in athlete_refs:
                        athlete_refs[aid] = stats_ref

        # Full-roster augmentation: ESPN caps each stat-category's athletes[] to
        # top-N, so players in no category's top-N (some stars + bench/rotation)
        # get dropped from the statistics endpoint. The competitor's /roster
        # endpoint lists EVERY athlete (active + didNotPlay) on the team, each
        # with its own per-game statistics $ref (splits: general/offensive/
        # defensive). Augment athlete_refs from it (dedup by playerId/aid) so
        # those players are also fetched via fetch_athlete().
        # 🔴 ACCURACY: this must NEVER silently drop players. If the roster call
        # fails, log a loud warning so an incomplete boxscore is never accepted.
        roster_error = None
        box_r = None
        for attempt in range(3):
            try:
                roster_url = (
                    f"{CORE_BASE}/events/{espn_game_id}/competitions/{espn_game_id}"
                    f"/competitors/{comp_id}/roster"
                )
                box_r = await client.get(roster_url, timeout=30)
                if box_r.status_code == 200:
                    break
                roster_error = f"roster HTTP {box_r.status_code}"
            except Exception as e:
                roster_error = f"roster fetch error: {e}"
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
        else:
            # All retries failed -> LOUD, never silent.
            logger.error(
                "[ACCURACY] Failed to fetch full roster for event=%s comp=%s after 3 tries (%s). "
                "pgs boxscore may be INCOMPLETE. retry event to backfill.",
                espn_game_id, comp_id, roster_error,
            )
        if box_r is not None and box_r.status_code == 200:
            for entry in (box_r.json().get("entries", []) or []):
                # 🔴 ACCURACY (2026-08-16): do NOT skip didNotPlay players here.
                # A DNP/rest/injury/healthy-scratch player still gets a pgs row
                # (minutes=0, points=0) so the roster row EXISTS and the boxscore
                # is complete/verifiable. Dropping them makes boxes never sum to
                # game totals and hides whether a "star" was active or not. The
                # minutes=0 is what star features/algorithms use to treat them as
                # not-playing.
                stats_ref = (entry.get("statistics", {}) or {}).get("$ref", "")
                if not stats_ref:
                    # keep the athlete anyway (no stats ref yet) via a placeholder
                    # so they are NOT silently dropped; fetch_athlete will handle
                    # missing stats gracefully.
                    stats_ref = (
                        f"{CORE_BASE}/events/{espn_game_id}/competitions/"
                        f"{espn_game_id}/competitors/{comp_id}/athletes/"
                        f"{entry.get('playerId')}"
                    )
                try:
                    aid = int(stats_ref.split("/")[-3])
                except (ValueError, IndexError):
                    aid = entry.get("playerId")
                if aid is not None and aid not in athlete_refs:
                    athlete_refs[aid] = stats_ref


        if not athlete_refs:
            continue

        # Fetch per-athlete stats
        async def fetch_athlete(pid: int, ref: str):
            try:
                r = await client.get(ref, timeout=15)
                if r.status_code == 200:
                    d = r.json()
                    all_cats = d.get("splits", {}).get("categories", [])
                    merged = {"_athlete_name": "", "_athlete_position": ""}
                    for cat in all_cats:
                        stats = cat.get("stats", [])
                        for s in stats:
                            name = s.get("name")
                            dv = s.get("displayValue", "")
                            merged[name] = dv
                    # Get athlete name
                    ath_ref = d.get("athlete", {}).get("$ref", "")
                    if ath_ref:
                        try:
                            r2 = await client.get(ath_ref, timeout=10)
                            if r2.status_code == 200:
                                merged["_athlete_name"] = r2.json().get("displayName", "")
                                merged["_athlete_position"] = r2.json().get("position", {}).get("abbreviation", "") or ""
                        except Exception:
                            pass
                    return (pid, merged)
            except Exception:
                return None

        tasks = [asyncio.create_task(fetch_athlete(aid, ref)) for aid, ref in athlete_refs.items()]
        results = await asyncio.gather(*tasks)

        for result in results:
            if result is None:
                continue
            pid, stats = result

            # Map to DB player
            db_player_id = espn_cache.get(pid)
            if db_player_id is None:
                athlete_name = stats.get("_athlete_name", "")
                db_player_id = match_and_save_espn_id(pid, athlete_name, db_conn)
                if db_player_id:
                    espn_cache[pid] = db_player_id

            if db_player_id is None:
                # ESPN returned an athlete we have no DB row for (e.g. retired/
                # historical players absent from nba.players). To keep game stats
                # COMPLETE, auto-create a minimal player row and link the ESPN id
                # instead of silently dropping this player's stats.
                athlete_name = (stats.get("_athlete_name") or "").strip()
                if athlete_name:
                    pos = (stats.get("_athlete_position") or "").strip()[:4] or "F"
                    try:
                        ins = db_conn.execute(text("""
                            INSERT INTO nba.players
                                (name, position, team_id, espn_id, active)
                            VALUES (:name, :pos, :team_id, :espn_id, 0)
                            RETURNING id
                        """), {
                            "name": athlete_name,
                            "pos": pos,
                            "team_id": db_team_id,
                            "espn_id": pid,
                        })
                        row = ins.fetchone()
                        if row:
                            db_player_id = row[0]
                            espn_cache[pid] = db_player_id
                            db_conn.commit()
                            logger.info(
                                f"  auto-created nba.players id={db_player_id} "
                                f"'{athlete_name}' ({pos}, espn {pid})"
                            )
                    except Exception as e:
                        logger.warning(f"  auto-create player failed for {athlete_name}: {e}")

            if db_player_id is None:
                continue

            # Extract individual stat values
            def sv(name):
                v = stats.get(name)
                if v is None or v == "":
                    return None
                try:
                    if name == "minutes":
                        return v
                    elif name in ("fieldGoalPct", "threePointFieldGoalPct", "freeThrowPct", "plusMinus"):
                        return float(v)
                    else:
                        return int(float(v))
                except (ValueError, TypeError):
                    return None

            # Insert
            try:
                db_conn.execute(text("""
                    INSERT INTO nba.player_game_stats
                        (game_id, player_id, team_id, nba_game_id, nba_player_id,
                         minutes, field_goals_made, field_goals_attempted, field_goal_pct,
                         three_pointers_made, three_pointers_attempted, three_pointer_pct,
                         free_throws_made, free_throws_attempted, free_throw_pct,
                         rebounds_offensive, rebounds_defensive, rebounds_total,
                         assists, steals, blocks, turnovers, fouls_personal,
                         points, plus_minus)
                    VALUES
                        (:game_id, :player_id, :team_id, :nba_game_id, :nba_player_id,
                         :min, :fgm, :fga, :fgp, :tpm, :tpa, :tpp, :ftm, :fta, :ftp,
                         :oreb, :dreb, :treb, :ast, :stl, :blk, :tov, :pf,
                         :pts, :pm)
                    ON CONFLICT (game_id, player_id) DO NOTHING
                """), {
                    "game_id": db_game_id,
                    "player_id": db_player_id,
                    "team_id": db_team_id,
                    "nba_game_id": espn_game_id,
                    "nba_player_id": pid,
                    "min": sv("minutes"),
                    "fgm": sv("fieldGoalsMade"),
                    "fga": sv("fieldGoalsAttempted"),
                    "fgp": sv("fieldGoalPct"),
                    "tpm": sv("threePointFieldGoalsMade"),
                    "tpa": sv("threePointFieldGoalsAttempted"),
                    "tpp": sv("threePointFieldGoalPct"),
                    "ftm": sv("freeThrowsMade"),
                    "fta": sv("freeThrowsAttempted"),
                    "ftp": sv("freeThrowPct"),
                    "oreb": sv("offensiveRebounds"),
                    "dreb": sv("defensiveRebounds"),
                    "treb": sv("totalRebounds"),
                    "ast": sv("assists"),
                    "stl": sv("steals"),
                    "blk": sv("blocks"),
                    "tov": sv("turnovers"),
                    "pf": sv("personalFouls"),
                    "pts": sv("points"),
                    "pm": sv("plusMinus"),
                })
                total_rows += 1
            except Exception as e:
                logger.warning(f"  DB insert error game {espn_game_id} player {pid}: {e}")

    return total_rows


# ── Orchestrator ─────────────────────────────────────────────────────────────

async def ingest_season(season_year: int, game_type: str = "REG", gap_fill: bool = False):
    """Ingest player game stats for all FINAL games of a given season.

    gap_fill=True: skip the >24000 row gate AND skip the destructive DELETE,
    relying on the per-player `ON CONFLICT (game_id, player_id) DO NOTHING`
    upsert to fill only missing player rows (safe to re-run on final games).
    """
    engine = create_engine(DB_URL)

    with engine.connect() as db_conn:
        # Load existing espn_id cache
        rows = db_conn.execute(
            text("SELECT id, espn_id FROM nba.players WHERE espn_id IS NOT NULL")
        ).fetchall()
        espn_cache = {int(eid): pid for pid, eid in rows}

        # Get all FINAL games for this season
        games = db_conn.execute(text("""
            SELECT g.id, g.nba_game_id, h.abbreviation, a.abbreviation
            FROM nba.games g
            JOIN nba.seasons s ON s.id = g.season_id
            JOIN nba.teams h ON h.id = g.home_team_id
            JOIN nba.teams a ON a.id = g.away_team_id
            WHERE s.year = :year AND g.game_type = :gtype
              AND g.status::text = 'FINAL'
            ORDER BY g.date
        """), {"year": season_year, "gtype": game_type}).fetchall()

        # Check existing
        existing = db_conn.execute(text("""
            SELECT COUNT(*) FROM nba.player_game_stats pgs
            JOIN nba.games g ON g.id = pgs.game_id
            JOIN nba.seasons s ON s.id = g.season_id
            WHERE s.year = :year AND g.game_type = :gtype
        """), {"year": season_year, "gtype": game_type}).scalar()

        logger.info(f"[{season_year} {game_type}] {len(games)} games, {len(espn_cache)} players with espn_id, {existing} existing player stats")

        if existing > 24000 and not gap_fill:
            logger.info(f"  ✅ Already has {existing} rows, skipping (use --gap-fill to re-process)")
            engine.dispose()
            return

        # Clear existing partial stats (only when NOT gap-filling, so a gap-fill
        # run never deletes existing rows -- it fills missing ones via upsert)
        if existing > 0 and not gap_fill:
            db_conn.execute(text("""
                DELETE FROM nba.player_game_stats pgs
                USING nba.games g, nba.seasons s
                WHERE pgs.game_id = g.id AND g.season_id = s.id
                  AND s.year = :year AND g.game_type = :gtype
            """), {"year": season_year, "gtype": game_type})
            db_conn.commit()

        total = 0
        errors = 0
        games_with_data = 0
        commit_counter = 0

        async with httpx.AsyncClient(timeout=30) as client:
            consecutive_403 = 0
            cooldown_counter = 0

            for idx, (db_gid, nba_gid, home_abbr, away_abbr) in enumerate(games, 1):
                if not nba_gid:
                    continue

                # Rate limit protection
                await asyncio.sleep(0.3)
                cooldown_counter += 1

                # Every 100 games, take a longer cooldown
                if cooldown_counter >= 100:
                    logger.info(f"    Taking 30s cooldown after {idx} games...")
                    await asyncio.sleep(30)
                    cooldown_counter = 0

                rows = await process_game(
                    client, db_conn, nba_gid, db_gid, home_abbr, away_abbr, espn_cache
                )
                if rows:
                    total += rows
                    games_with_data += 1
                else:
                    errors += 1

                commit_counter += 1
                if commit_counter >= 20:
                    db_conn.commit()
                    commit_counter = 0

                if idx % 50 == 0 or idx == len(games):
                    logger.info(f"  [{idx}/{len(games)}] {total} rows from {games_with_data} games, {errors} empty")

        # Final commit
        if commit_counter > 0:
            db_conn.commit()

        logger.info(f"[{season_year} {game_type}] ✅ {total} player stat rows from {games_with_data}/{len(games)} games")

    engine.dispose()
    return total


async def main():
    """CLI: backfill NBA player game stats for given season(s)."""
    import argparse
    parser = argparse.ArgumentParser(description="NBA player game stats ingest")
    parser.add_argument("--seasons", nargs="*", type=int,
                        help="Calendar years to process (e.g. 2023 2024 2025). Default: [2025]")
    parser.add_argument("--game-type", default="REG",
                        help="game_type filter: REG (default), POST, PLAYIN")
    parser.add_argument("--gap-fill", action="store_true",
                        help="Fill missing player rows only (skip >24000 gate + skip DELETE). Safe on finals.")
    args = parser.parse_args()

    seasons = args.seasons or [2025]
    total_all = 0
    for yr in seasons:
        logger.info(f"\n===== INGEST season {yr} ({args.game_type}) gap_fill={args.gap_fill} =====")
        total_all += await ingest_season(yr, args.game_type, gap_fill=args.gap_fill)
    logger.info(f"\nDONE: {total_all} rows across seasons {seasons}")


if __name__ == "__main__":
    asyncio.run(main())
