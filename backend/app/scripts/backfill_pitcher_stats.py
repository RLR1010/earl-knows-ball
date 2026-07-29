#!/usr/bin/env python3
"""Backfill missing pitcher stats from MLB Stats API."""
import asyncio
import logging
import time
import httpx
from sqlalchemy import text, create_engine
from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://statsapi.mlb.com/api/v1/game"
MIN_INTERVAL = 0.15  # 0.15s between requests
MAX_CONCURRENT = 7

# Sync engine for DB reads
sync_engine = create_engine(settings.database_url_sync)


def get_missing_games() -> list[tuple[int, int]]:
    """Return list of (game_id, mlb_game_id) missing from pitcher_game_stats."""
    with sync_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT g.id, g.mlb_game_id
            FROM mlb.games g
            WHERE g.mlb_game_id IS NOT NULL
              AND g.home_score IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM mlb.pitcher_game_stats pgs WHERE pgs.game_id = g.id
              )
            ORDER BY g.mlb_game_id
        """)).all()
    return [(r[0], r[1]) for r in rows]


def insert_pitcher_rows(game_id: int, mlb_game_id: int, rows: list[dict]) -> int:
    """Bulk insert pitcher rows for a game."""
    if not rows:
        return 0
    with sync_engine.begin() as conn:
        for r in rows:
            conn.execute(text(f"""
                INSERT INTO mlb.pitcher_game_stats (
                    game_id, mlb_game_id, pitcher_name, pitcher_mlb_id, team_abbr, is_starter,
                    ip, er, h, k, bb, hr, runs_allowed,
                    hit_by_pitch, intentional_walks, batters_faced,
                    pitches_thrown, strikes, game_score, decision,
                    ground_outs, air_outs, fly_outs, pop_outs, line_outs,
                    ground_into_double_play, wild_pitches, balks,
                    saves, holds, blown_saves, wins, losses
                ) VALUES (
                    :game_id, :mlb_game_id, :pitcher_name, :pitcher_mlb_id, :team_abbr, :is_starter,
                    :ip, :er, :h, :k, :bb, :hr, :runs_allowed,
                    :hit_by_pitch, :intentional_walks, :batters_faced,
                    :pitches_thrown, :strikes, :game_score, :decision,
                    :ground_outs, :air_outs, :fly_outs, :pop_outs, :line_outs,
                    :ground_into_double_play, :wild_pitches, :balks,
                    :saves, :holds, :blown_saves, :wins, :losses
                )
            """), {**r, "game_id": game_id, "mlb_game_id": mlb_game_id})
    return len(rows)


async def fetch_boxscore(client: httpx.AsyncClient, game_pk: int) -> dict | None:
    """Fetch boxscore for a game."""
    url = f"{API_BASE}/{game_pk}/boxscore"
    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        logger.debug("Error fetching %d: %s", game_pk, e)
        return None


def parse_pitchers(boxscore: dict, side: str, team_abbr: str) -> list[dict]:
    """Parse all pitchers for one side."""
    team_data = boxscore.get("teams", {}).get(side, {})
    if not team_data:
        return []

    pitchers_list = team_data.get("pitchers", [])
    player_data = team_data.get("players", {})

    results = []
    for order, pid in enumerate(pitchers_list):
        entry = player_data.get(f"ID{pid}", {})
        stats = entry.get("stats", {}).get("pitching", {})
        if not stats:
            continue

        name = entry.get("person", {}).get("fullName", "Unknown")
        note = stats.get("note", "")

        row = {
            "pitcher_name": name,
            "pitcher_mlb_id": pid,
            "team_abbr": team_abbr,
            "is_starter": order == 0,
            "ip": stats.get("inningsPitched"),
            "er": stats.get("earnedRuns", 0),
            "h": stats.get("hits", 0),
            "k": stats.get("strikeOuts", 0),
            "bb": stats.get("baseOnBalls", 0),
            "hr": stats.get("homeRuns", 0),
            "runs_allowed": stats.get("runs", 0),
            
            "hit_by_pitch": stats.get("hitByPitch", 0),
            "intentional_walks": stats.get("intentionalWalks", 0),
            "batters_faced": stats.get("battersFaced", 0),
            "pitches_thrown": stats.get("numberOfPitches"),
            "strikes": stats.get("strikes", 0),
            "game_score": stats.get("gameScore"),
            "decision": note,
            "ground_outs": stats.get("groundOuts", 0),
            "air_outs": stats.get("airOuts", 0),
            "fly_outs": stats.get("flyOuts", 0),
            "pop_outs": stats.get("popOuts", 0),
            "line_outs": stats.get("lineOuts", 0),
            "ground_into_double_play": stats.get("groundIntoDoublePlay", 0),
            "wild_pitches": stats.get("wildPitches", 0),
            "balks": stats.get("balks", 0),
            "saves": stats.get("saves", 0),
            "holds": stats.get("holds", 0),
            "blown_saves": stats.get("blownSaves", 0),
            "wins": stats.get("wins", 0),
            "losses": stats.get("losses", 0),
        }
        results.append(row)

    return results


async def process_game(sem: asyncio.Semaphore, client: httpx.AsyncClient,
                        game_id: int, mlb_game_id: int) -> bool:
    """Fetch and insert pitcher data for one game."""
    async with sem:
        await asyncio.sleep(MIN_INTERVAL)
        boxscore = await fetch_boxscore(client, mlb_game_id)
        if not boxscore:
            return False

        # Get team abbreviations
        home_abbr = boxscore.get("teams", {}).get("home", {}).get("team", {}).get("abbreviation", "")
        away_abbr = boxscore.get("teams", {}).get("away", {}).get("team", {}).get("abbreviation", "")

        rows = []
        if home_abbr:
            rows.extend(parse_pitchers(boxscore, "home", home_abbr))
        if away_abbr:
            rows.extend(parse_pitchers(boxscore, "away", away_abbr))

        if not rows:
            return False

        insert_pitcher_rows(game_id, mlb_game_id, rows)
        return True


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, help="Limit number of games to process")
    parser.add_argument("--start-id", type=int, help="Start from this mlb_game_id (inclusive)")
    parser.add_argument("--year", type=int, help="Process only games from specific season year")
    args = parser.parse_args()

    games = get_missing_games()
    logger.info("Found %d games missing pitcher stats", len(games))

    if args.year:
        # Need to filter by year, so we need game dates
        with sync_engine.connect() as conn:
            year_filtered = conn.execute(text("""
                SELECT g.id, g.mlb_game_id
                FROM mlb.games g
                JOIN mlb.seasons s ON s.id = g.season_id
                WHERE s.year = :year
                  AND g.mlb_game_id IS NOT NULL
                  AND g.home_score IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM mlb.pitcher_game_stats pgs WHERE pgs.game_id = g.id
                  )
                ORDER BY g.mlb_game_id
            """), {"year": args.year}).all()
        games = [(r[0], r[1]) for r in year_filtered]
        logger.info("Filtered to year %d: %d games", args.year, len(games))

    if args.start_id:
        games = [(gid, mid) for gid, mid in games if mid >= args.start_id]
        logger.info("Filtered to mlb_game_id >= %d: %d games", args.start_id, len(games))

    if args.games:
        games = games[:args.games]

    logger.info("Processing %d games", len(games))

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with httpx.AsyncClient() as client:
        success = 0
        fail = 0
        for i, (game_id, mlb_game_id) in enumerate(games):
            ok = await process_game(sem, client, game_id, mlb_game_id)
            if ok:
                success += 1
            else:
                fail += 1
            if (i + 1) % 50 == 0:
                logger.info("  Progress: %d/%d (ok=%d fail=%d)", i + 1, len(games), success, fail)

    logger.info("Done: %d/%d successful, %d failed", success, len(games), fail)


if __name__ == "__main__":
    asyncio.run(main())
