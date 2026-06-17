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
            "pitcher_mlb_id": pid,
            "pitcher_name": person.get("fullName", "Unknown"),
            "team_abbr": team_abbr,
            "is_starter": is_starter,
            "ip": float(ip) if ip is not None else None,
            "er": pitching.get("earnedRuns"),
            "h": pitching.get("hits"),
            "k": pitching.get("strikeOuts"),
            "bb": pitching.get("baseOnBalls"),
            "hr": pitching.get("homeRuns"),
            "pitches_thrown": pitching.get("numberOfPitches"),
            "game_score": pitching.get("gameScore"),
            "decision": pitching.get("note", ""),
        })
    return rows


async def ingest_pitcher_stats(year_from: int = 2011, game_limit: int = None, year_only: int = None):
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
              AND g.mlb_game_id NOT IN (
                  SELECT mlb_game_id FROM mlb.pitcher_game_stats
              )
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
                                 team_abbr, is_starter, ip, er, h, k, bb, hr,
                                 pitches_thrown, game_score, decision)
                            VALUES
                                (:game_id, :mlb_game_id, :pitcher_mlb_id, :pitcher_name,
                                 :team_abbr, :is_starter, :ip, :er, :h, :k, :bb, :hr,
                                 :pitches_thrown, :game_score, :decision)
                            ON CONFLICT (mlb_game_id, pitcher_mlb_id) DO NOTHING
                        """), {
                            "game_id": g["id"],
                            "mlb_game_id": g["mlb_game_id"],
                            "pitcher_mlb_id": p["pitcher_mlb_id"],
                            "pitcher_name": p["pitcher_name"],
                            "team_abbr": p["team_abbr"],
                            "is_starter": p["is_starter"],
                            "ip": p["ip"],
                            "er": p["er"],
                            "h": p["h"],
                            "k": p["k"],
                            "bb": p["bb"],
                            "hr": p["hr"],
                            "pitches_thrown": p["pitches_thrown"],
                            "game_score": p["game_score"],
                            "decision": p["decision"],
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
    args = parser.parse_args()

    await ingest_pitcher_stats(
        year_from=args.from_year,
        game_limit=args.games,
        year_only=args.year,
    )


if __name__ == "__main__":
    asyncio.run(main())
