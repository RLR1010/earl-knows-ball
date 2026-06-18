"""
Ingest per-game pitcher stats from MLB Stats API.

Fetches every pitcher who appeared in each completed game since 2011,
including innings pitched, earned runs, hits, strikeouts, walks, etc.

Usage:
    docker exec earl-knows-football-api-1 python -m app.ingestion.mlb_pitcher_stats
    docker exec earl-knows-football-api-1 python -m app.ingestion.mlb_pitcher_stats --games 500
    docker exec earl-knows-football-api-1 python -m app.ingestion.mlb_pitcher_stats --year 2024
"""
import asyncio
import logging
import time
import httpx
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger().info

DB = "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football"
API_BASE = "https://statsapi.mlb.com/api/v1.1/game"

# Rate limiting: max 10 calls/sec, but be nice
MIN_INTERVAL = 0.15  # seconds between calls (~6.7/sec)


async def fetch_game_boxscore(client: httpx.AsyncClient, game_pk: int) -> dict | None:
    """Fetch the live feed for a game and extract boxscore."""
    url = f"{API_BASE}/{game_pk}/feed/live"
    try:
        resp = await client.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        ls = data.get("liveData", {})
        return ls.get("boxscore")
    except (httpx.TimeoutException, httpx.HTTPError, Exception) as e:
        log(f"  Error fetching {game_pk}: {e}")
        return None


def parse_pitchers(boxscore: dict, side: str, team_abbr: str) -> list[dict]:
    """Parse all pitchers for one side of a boxscore."""
    team_data = boxscore.get("teams", {}).get(side, {})
    if not team_data:
        return []

    players = team_data.get("players", {})
    pitcher_ids = team_data.get("pitchers", [])
    starters = set(pitcher_ids[:1]) if pitcher_ids else set()  # first pitcher listed = starter

    rows = []
    for pid_str, p in players.items():
        # pid_str is like "ID123456"
        try:
            pid = int(pid_str.replace("ID", ""))
        except ValueError:
            continue

        stats = p.get("stats", {})
        pitching = stats.get("pitching", {})
        if not pitching:
            continue

        person = p.get("person", {})
        is_starter = pid in starters

        ip = pitching.get("inningsPitched")
        # Convert "7.0" or "7.1" or "7.2" to decimal (MLB uses .1 = 1/3, .2 = 2/3)
        if ip is not None and isinstance(ip, str):
            if "." in ip:
                whole, frac = ip.split(".")
                if frac in ("1", "2"):
                    ip = int(whole) + int(frac) / 3
                else:
                    ip = float(ip)
            else:
                ip = float(ip)

        rows.append({
            # Identifiers
            "pitcher_mlb_id": pid,
            "pitcher_name": person.get("fullName", "Unknown"),
            "team_abbr": team_abbr,
            "is_starter": is_starter,

            # Core counting stats
            "ip": float(ip) if ip is not None else None,
            "er": pitching.get("earnedRuns"),
            "runs_allowed": pitching.get("runs", 0),
            "h": pitching.get("hits"),
            "hr": pitching.get("homeRuns"),
            "k": pitching.get("strikeOuts"),
            "bb": pitching.get("baseOnBalls"),
            "intentional_walks": pitching.get("intentionalWalks", 0),
            "hit_by_pitch": pitching.get("hitByPitch", 0),

            # Pitch count / sequencing
            "pitches_thrown": pitching.get("numberOfPitches"),
            "strikes": pitching.get("strikes", 0),
            "batters_faced": pitching.get("battersFaced", 0),

            # Batted ball type
            "ground_outs": pitching.get("groundOuts", 0),
            "air_outs": pitching.get("airOuts", 0),
            "fly_outs": pitching.get("flyOuts", 0),
            "pop_outs": pitching.get("popOuts", 0),
            "line_outs": pitching.get("lineOuts", 0),
            "gidp": pitching.get("groundIntoDoublePlay", 0),

            # Game script
            "game_score": pitching.get("gameScore"),
            "decision": pitching.get("note", ""),
            "saves": pitching.get("saves", 0),
            "holds": pitching.get("holds", 0),
            "blown_saves": pitching.get("blownSaves", 0),
            "wins": pitching.get("wins", 0),
            "losses": pitching.get("losses", 0),

            # Misc
            "wild_pitches": pitching.get("wildPitches", 0),
            "balks": pitching.get("balks", 0),
        })
    return rows


async def ingest_pitcher_stats(year_from: int = 2011, game_limit: int = None, year_only: int = None, reprocess: bool = False):
    """Ingest pitcher stats for all completed games from year_from onwards."""
    engine = create_async_engine(DB)
    t0 = time.time()
    total_inserted = 0
    total_errors = 0

    async with engine.connect() as conn:
        # Get games with mlb_game_id that have scores
        if year_only:
            year_filter = f"AND s.year = {year_only}"
        else:
            year_filter = f"AND s.year >= {year_from}"

        has_check = "" if reprocess else \
            "AND g.mlb_game_id NOT IN (SELECT mlb_game_id FROM mlb.pitcher_game_stats)"

        r = await conn.execute(text(f"""
            SELECT g.id, g.mlb_game_id, s.year,
                   (SELECT abbreviation FROM mlb.teams WHERE id = g.away_team_id) as away_abbr,
                   (SELECT abbreviation FROM mlb.teams WHERE id = g.home_team_id) as home_abbr
            FROM mlb.games g
            JOIN mlb.seasons s ON s.id = g.season_id
            WHERE g.mlb_game_id IS NOT NULL
              AND g.home_score IS NOT NULL
              AND g.away_score IS NOT NULL
              {year_filter}
              {has_check}
            ORDER BY s.year, g.date, g.id
        """))
        games = [dict(r._mapping) for r in r.fetchall()]

    total = len(games)
    log(f"Games to process: {total}")

    if game_limit:
        games = games[:game_limit]
        log(f"Limiting to {game_limit} games")

    async with httpx.AsyncClient() as client:
        for i, g in enumerate(games):
            boxscore = await fetch_game_boxscore(client, g["mlb_game_id"])
            if not boxscore:
                total_errors += 1
                if i > 0 and i % 100 == 0:
                    log(f"  [{i}/{total}] {total_errors} errors so far")
                await asyncio.sleep(MIN_INTERVAL)
                continue

            # Parse both sides
            away_pitchers = parse_pitchers(boxscore, "away", g["away_abbr"])
            home_pitchers = parse_pitchers(boxscore, "home", g["home_abbr"])
            all_pitchers = away_pitchers + home_pitchers

            if not all_pitchers:
                total_errors += 1
                await asyncio.sleep(MIN_INTERVAL)
                continue

            # Filter out position players (0.0 IP, no game score, no stats)
            all_pitchers = [p for p in all_pitchers if p["ip"] is not None and p["ip"] > 0]
            if not all_pitchers:
                total_errors += 1
                await asyncio.sleep(MIN_INTERVAL)
                continue

            # Insert to DB (each row in its own transaction to avoid cascade failures)
            inserted = 0
            for p in all_pitchers:
                try:
                    async with engine.connect() as conn:
                        await conn.execute(text("""
                            INSERT INTO mlb.pitcher_game_stats
                                (game_id, mlb_game_id, pitcher_mlb_id, pitcher_name,
                                 team_abbr, is_starter,
                                 ip, er, runs_allowed, h, hr, k, bb, intentional_walks, hit_by_pitch,
                                 pitches_thrown, strikes, batters_faced,
                                 ground_outs, air_outs, fly_outs, pop_outs, line_outs,
                                 ground_into_double_play,
                                 game_score, decision,
                                 saves, holds, blown_saves, wins, losses,
                                 wild_pitches, balks)
                            VALUES
                                (:game_id, :mlb_game_id, :pitcher_mlb_id, :pitcher_name,
                                 :team_abbr, :is_starter,
                                 :ip, :er, :runs_allowed, :h, :hr, :k, :bb, :intentional_walks, :hit_by_pitch,
                                 :pitches_thrown, :strikes, :batters_faced,
                                 :ground_outs, :air_outs, :fly_outs, :pop_outs, :line_outs,
                                 :gidp,
                                 :game_score, :decision,
                                 :saves, :holds, :blown_saves, :wins, :losses,
                                 :wild_pitches, :balks)
                            ON CONFLICT (mlb_game_id, pitcher_mlb_id) DO UPDATE SET
                                runs_allowed = EXCLUDED.runs_allowed,
                                intentional_walks = EXCLUDED.intentional_walks,
                                hit_by_pitch = EXCLUDED.hit_by_pitch,
                                strikes = EXCLUDED.strikes,
                                batters_faced = EXCLUDED.batters_faced,
                                ground_outs = EXCLUDED.ground_outs,
                                air_outs = EXCLUDED.air_outs,
                                fly_outs = EXCLUDED.fly_outs,
                                pop_outs = EXCLUDED.pop_outs,
                                line_outs = EXCLUDED.line_outs,
                                ground_into_double_play = EXCLUDED.ground_into_double_play,
                                saves = EXCLUDED.saves,
                                holds = EXCLUDED.holds,
                                blown_saves = EXCLUDED.blown_saves,
                                wins = EXCLUDED.wins,
                                losses = EXCLUDED.losses,
                                wild_pitches = EXCLUDED.wild_pitches,
                                balks = EXCLUDED.balks
                        """), {
                            "game_id": g["id"],
                            "mlb_game_id": g["mlb_game_id"],
                            "pitcher_mlb_id": p["pitcher_mlb_id"],
                            "pitcher_name": p["pitcher_name"],
                            "team_abbr": p["team_abbr"],
                            "is_starter": p["is_starter"],
                            "ip": p["ip"],
                            "er": p["er"],
                            "runs_allowed": p["runs_allowed"],
                            "h": p["h"],
                            "hr": p["hr"],
                            "k": p["k"],
                            "bb": p["bb"],
                            "intentional_walks": p["intentional_walks"],
                            "hit_by_pitch": p["hit_by_pitch"],
                            "pitches_thrown": p["pitches_thrown"],
                            "strikes": p["strikes"],
                            "batters_faced": p["batters_faced"],
                            "ground_outs": p["ground_outs"],
                            "air_outs": p["air_outs"],
                            "fly_outs": p["fly_outs"],
                            "pop_outs": p["pop_outs"],
                            "line_outs": p["line_outs"],
                            "gidp": p["gidp"],
                            "game_score": p["game_score"],
                            "decision": p["decision"],
                            "saves": p["saves"],
                            "holds": p["holds"],
                            "blown_saves": p["blown_saves"],
                            "wins": p["wins"],
                            "losses": p["losses"],
                            "wild_pitches": p["wild_pitches"],
                            "balks": p["balks"],
                        })
                        await conn.commit()
                    inserted += 1
                except Exception as e:
                    log(f"  DB insert error: {p['pitcher_name']} (game {g['mlb_game_id']}): {e}")
                    continue

            total_inserted += len(all_pitchers)

            if i > 0 and i % 100 == 0:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed
                remaining = max(0, (total - i - 1) / rate) if rate > 0 else 0
                log(f"  [{i}/{total}] {total_inserted} pitcher lines, "
                    f"{total_errors} errors, {rate:.1f}/sec, "
                    f"{remaining/60:.1f}m remaining")

            await asyncio.sleep(MIN_INTERVAL)

    elapsed = time.time() - t0
    log(f"\nDone! {total_inserted} pitcher lines from {total - total_errors}/{total} games")
    log(f"Time: {elapsed:.0f}s ({elapsed/60:.1f}m)")
    await engine.dispose()


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Ingest MLB pitcher game stats")
    parser.add_argument("--year", type=int, default=None, help="Only process this year")
    parser.add_argument("--games", type=int, default=None, help="Max games to process")
    parser.add_argument("--from-year", type=int, default=2011, help="Process from this year")
    parser.add_argument("--reprocess", action="store_true", help="Re-fetch existing games to populate new columns")
    args = parser.parse_args()

    await ingest_pitcher_stats(
        year_from=args.from_year,
        game_limit=args.games,
        year_only=args.year,
        reprocess=args.reprocess,
    )


if __name__ == "__main__":
    asyncio.run(main())
