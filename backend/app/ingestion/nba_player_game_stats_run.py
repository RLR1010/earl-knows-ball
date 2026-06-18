"""
Resume-able runner for NBA player game stats ingestion.

Processes all remaining games from the ESPN core API with rate limit
protection: 300ms delay between games, 30s cooldown every 100 games.

If rate-limited (403), waits 60s and retries, up to 5 retries.
"""

import asyncio
import logging
import re
from typing import Optional

import httpx
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("nba-espn-stats")

DB_URL = "postgresql://earl:earl2025@localhost:5432/earl_knows_football"
CORE_BASE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"


def _extract_val(stats: list[dict], name: str):
    for s in stats:
        if s.get("name") == name:
            dv = s.get("displayValue", "")
            if not dv:
                return None
            try:
                if name == "minutes":
                    return dv
                elif name in ("fieldGoalPct", "threePointFieldGoalPct", "freeThrowPct", "plusMinus", "fantasyPoints"):
                    return float(dv)
                else:
                    return int(float(dv))
            except (ValueError, TypeError):
                return None
    return None


async def process_single_game(
    client: httpx.AsyncClient,
    db_conn,
    espn_game_id: str,
    db_game_id: int,
    db_home_team_id: int,
    db_away_team_id: int,
    home_abbr: str,
    away_abbr: str,
    espn_cache: dict,
) -> int:
    """Process one game. Returns rows inserted."""
    # Get competitors
    comp_url = f"{CORE_BASE}/events/{espn_game_id}/competitions/{espn_game_id}/competitors"
    resp = await client.get(comp_url, timeout=15)
    if resp.status_code == 403:
        return -1  # Rate limited
    if resp.status_code != 200:
        return 0
    comp_data = resp.json()

    # Map competitors to DB teams  
    comp_to_db = {}
    for item in comp_data.get("items", []):
        ref = item.get("$ref", "")
        comp_id = int(ref.split("/")[-1].split("?")[0])
        try:
            r2 = await client.get(ref, timeout=10)
            if r2.status_code != 200:
                continue
            c = r2.json()
            home_away = c.get("homeAway", "")
            team_ref = c.get("team", {}).get("$ref", "")
            if team_ref:
                r3 = await client.get(team_ref, timeout=10)
                if r3.status_code == 200:
                    t = r3.json()
                    abbr = t.get("abbreviation", "")
                    # Map to DB team ID
                    aliases = {"GS":"GSW","NY":"NYK","SA":"SAS","NO":"NOP",
                               "PHO":"PHX","BK":"BKN","UTAH":"UTA","CHA":"CHO"}
                    db_abbr = aliases.get(abbr, abbr)
                    tid_row = db_conn.execute(
                        text("SELECT id FROM nba.teams WHERE abbreviation = :abbr"),
                        {"abbr": db_abbr}
                    ).fetchone()
                    if tid_row:
                        comp_to_db[comp_id] = tid_row[0]
        except Exception:
            continue

    if not comp_to_db:
        return 0

    # Fetch athlete stats per competitor
    total_rows = 0
    for comp_id, db_team_id in comp_to_db.items():
        stats_url = f"{CORE_BASE}/events/{espn_game_id}/competitions/{espn_game_id}/competitors/{comp_id}/statistics"
        resp = await client.get(stats_url, timeout=15)
        if resp.status_code != 200:
            continue

        data = resp.json()
        categories = data.get("splits", {}).get("categories", [])
        
        # Collect unique athlete stat refs
        athlete_refs = {}
        for cat in categories:
            for entry in cat.get("athletes", []):
                stats_ref = entry.get("statistics", {}).get("$ref", "")
                if stats_ref:
                    aid = int(stats_ref.split("/")[-3])
                    if aid not in athlete_refs:
                        athlete_refs[aid] = stats_ref

        if not athlete_refs:
            continue

        # Fetch per-athlete stats
        for aid, ref in athlete_refs.items():
            resp = await client.get(ref, timeout=15)
            if resp.status_code != 200:
                continue
            d = resp.json()
            all_cats = d.get("splits", {}).get("categories", [])
            merged = {}
            for cat in all_cats:
                merged["minutes"] = merged.get("minutes") or _extract_val(cat.get("stats", []), "minutes")
                merged["fieldGoalsMade"] = merged.get("fieldGoalsMade") or _extract_val(cat.get("stats", []), "fieldGoalsMade")
                merged["fieldGoalsAttempted"] = merged.get("fieldGoalsAttempted") or _extract_val(cat.get("stats", []), "fieldGoalsAttempted")
                merged["fieldGoalPct"] = merged.get("fieldGoalPct") or _extract_val(cat.get("stats", []), "fieldGoalPct")
                merged["threePointFieldGoalsMade"] = merged.get("threePointFieldGoalsMade") or _extract_val(cat.get("stats", []), "threePointFieldGoalsMade")
                merged["threePointFieldGoalsAttempted"] = merged.get("threePointFieldGoalsAttempted") or _extract_val(cat.get("stats", []), "threePointFieldGoalsAttempted")
                merged["threePointFieldGoalPct"] = merged.get("threePointFieldGoalPct") or _extract_val(cat.get("stats", []), "threePointFieldGoalPct")
                merged["freeThrowsMade"] = merged.get("freeThrowsMade") or _extract_val(cat.get("stats", []), "freeThrowsMade")
                merged["freeThrowsAttempted"] = merged.get("freeThrowsAttempted") or _extract_val(cat.get("stats", []), "freeThrowsAttempted")
                merged["freeThrowPct"] = merged.get("freeThrowPct") or _extract_val(cat.get("stats", []), "freeThrowPct")
                merged["totalRebounds"] = merged.get("totalRebounds") or _extract_val(cat.get("stats", []), "totalRebounds")
                merged["offensiveRebounds"] = merged.get("offensiveRebounds") or _extract_val(cat.get("stats", []), "offensiveRebounds")
                merged["defensiveRebounds"] = merged.get("defensiveRebounds") or _extract_val(cat.get("stats", []), "defensiveRebounds")
                merged["assists"] = merged.get("assists") or _extract_val(cat.get("stats", []), "assists")
                merged["steals"] = merged.get("steals") or _extract_val(cat.get("stats", []), "steals")
                merged["blocks"] = merged.get("blocks") or _extract_val(cat.get("stats", []), "blocks")
                merged["turnovers"] = merged.get("turnovers") or _extract_val(cat.get("stats", []), "turnovers")
                merged["personalFouls"] = merged.get("personalFouls") or _extract_val(cat.get("stats", []), "personalFouls")
                merged["points"] = merged.get("points") or _extract_val(cat.get("stats", []), "points")
                merged["plusMinus"] = merged.get("plusMinus") or _extract_val(cat.get("stats", []), "plusMinus")
                merged["fantasyPoints"] = merged.get("fantasyPoints") or _extract_val(cat.get("stats", []), "fantasyPoints")

            # Map athlete to DB player
            db_player_id = espn_cache.get(aid)
            if db_player_id is None:
                # Try by name
                ath_ref = d.get("athlete", {}).get("$ref", "")
                athlete_name = ""
                if ath_ref:
                    try:
                        r2 = await client.get(ath_ref, timeout=10)
                        if r2.status_code == 200:
                            athlete_name = r2.json().get("displayName", "")
                    except Exception:
                        pass
                
                if athlete_name:
                    # Lookup by last name
                    parts = athlete_name.split()
                    if len(parts) >= 2:
                        last = parts[-1]
                        candidates = db_conn.execute(
                            text("SELECT id FROM nba.players WHERE LOWER(name) LIKE LOWER(:patt) LIMIT 3"),
                            {"patt": f"%{last}%"},
                        ).fetchall()
                        if len(candidates) == 1:
                            db_player_id = candidates[0][0]
                            db_conn.execute(
                                text("UPDATE nba.players SET espn_id = :eid WHERE id = :pid AND espn_id IS NULL"),
                                {"eid": aid, "pid": db_player_id},
                            )
                            db_conn.commit()
                            espn_cache[aid] = db_player_id
            
            if db_player_id is None:
                continue

            try:
                db_conn.execute(text("""
                    INSERT INTO nba.player_game_stats
                        (game_id, player_id, team_id, nba_game_id, nba_player_id,
                         minutes, field_goals_made, field_goals_attempted, field_goal_pct,
                         three_pointers_made, three_pointers_attempted, three_pointer_pct,
                         free_throws_made, free_throws_attempted, free_throw_pct,
                         rebounds_offensive, rebounds_defensive, rebounds_total,
                         assists, steals, blocks, turnovers, fouls_personal,
                         points, plus_minus, fantasy_points)
                    VALUES
                        (:gid, :pid, :tid, :ngid, :npaid,
                         :m, :fgm, :fga, :fgp, :tpm, :tpa, :tpp,
                         :ftm, :fta, :ftp, :orb, :drb, :trb,
                         :ast, :stl, :blk, :tov, :pf,
                         :pts, :pm, :fp)
                    ON CONFLICT (game_id, player_id) DO NOTHING
                """), {
                    "gid": db_game_id, "pid": db_player_id, "tid": db_team_id,
                    "ngid": espn_game_id, "npaid": aid,
                    "m": merged.get("minutes"), "fgm": merged.get("fieldGoalsMade"),
                    "fga": merged.get("fieldGoalsAttempted"), "fgp": merged.get("fieldGoalPct"),
                    "tpm": merged.get("threePointFieldGoalsMade"),
                    "tpa": merged.get("threePointFieldGoalsAttempted"),
                    "tpp": merged.get("threePointFieldGoalPct"),
                    "ftm": merged.get("freeThrowsMade"), "fta": merged.get("freeThrowsAttempted"),
                    "ftp": merged.get("freeThrowPct"), "orb": merged.get("offensiveRebounds"),
                    "drb": merged.get("defensiveRebounds"), "trb": merged.get("totalRebounds"),
                    "ast": merged.get("assists"), "stl": merged.get("steals"),
                    "blk": merged.get("blocks"), "tov": merged.get("turnovers"),
                    "pf": merged.get("personalFouls"), "pts": merged.get("points"),
                    "pm": merged.get("plusMinus"), "fp": merged.get("fantasyPoints"),
                })
                total_rows += 1
            except Exception as e:
                logger.warning(f"  DB error: {e}")

    return total_rows


async def run_remaining(season_year: int = 2025):
    """Process remaining games missing player stats."""
    engine = create_engine(DB_URL)
    
    with engine.connect() as db_conn:
        # Load existing espn_id cache
        espn_rows = db_conn.execute(
            text("SELECT id, espn_id FROM nba.players WHERE espn_id IS NOT NULL")
        ).fetchall()
        espn_cache = {int(eid): pid for pid, eid in espn_rows}
        logger.info(f"{len(espn_cache)} players with espn_id")

        # Get remaining games
        games = db_conn.execute(text("""
            SELECT g.id, g.nba_game_id, g.date::date,
                   h.abbreviation as home, a.abbreviation as away,
                   h.id as home_id, a.id as away_id
            FROM nba.games g
            JOIN nba.seasons s ON s.id = g.season_id
            JOIN nba.teams h ON h.id = g.home_team_id
            JOIN nba.teams a ON a.id = g.away_team_id
            WHERE s.year = :year AND g.game_type = 'REG' AND g.status::text = 'FINAL'
              AND NOT EXISTS (
                  SELECT 1 FROM nba.player_game_stats pgs WHERE pgs.game_id = g.id
              )
            ORDER BY g.date
        """), {"year": season_year}).fetchall()
        
        logger.info(f"Remaining: {len(games)} games")
        if not games:
            logger.info("All done!")
            engine.dispose()
            return

        total = 0
        success = 0
        errors = 0
        rate_limited = 0

        async with httpx.AsyncClient(timeout=30) as client:
            for idx, (db_gid, nba_gid, date, home, away, home_id, away_id) in enumerate(games, 1):
                if not nba_gid:
                    continue

                # Core delay
                await asyncio.sleep(0.3)

                rows = await process_single_game(
                    client, db_conn, nba_gid, db_gid, home_id, away_id,
                    home, away, espn_cache,
                )

                if rows == -1:  # Rate limited
                    rate_limited += 1
                    if rate_limited >= 3:
                        logger.warning(f"  [{idx}/{len(games)}] 3 consecutive 403s, waiting 60s...")
                        await asyncio.sleep(60)
                        rate_limited = 0
                        continue
                    logger.warning(f"  [{idx}/{len(games)}] 403 rate limit, retrying after 15s...")
                    await asyncio.sleep(15)
                    # Retry
                    rows = await process_single_game(
                        client, db_conn, nba_gid, db_gid, home_id, away_id,
                        home, away, espn_cache,
                    )
                else:
                    rate_limited = 0

                if rows > 0:
                    total += rows
                    success += 1
                    db_conn.commit()
                elif rows == 0:
                    errors += 1

                if idx % 50 == 0 or idx == len(games):
                    logger.info(f"  [{idx}/{len(games)}] {total} rows ({success} games), {errors} empty, {1 if rate_limited else 0} rate limited")

        db_conn.commit()
        logger.info(f"\nDone: {total} rows from {success}/{len(games)} games ({errors} empty, {rate_limited} rate-limited)")
    
    engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_remaining())
