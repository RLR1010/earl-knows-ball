"""
Backfill missing home_pitcher_name / away_pitcher_name in mlb.games.

Processes all dates where either pitcher column is NULL, calling the
MLB Stats API schedule endpoint (hydrate=probablePitcher) for each date.

Usage:
    python -m backend.scripts.backfill_mlb_pitchers
    python -m backend.scripts.backfill_mlb_pitchers --start 2026-07-01  # resume from date
"""

import asyncio
import logging
import sys
from argparse import ArgumentParser
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill.mlb_pitchers")

STATS_API = "https://statsapi.mlb.com"
SPORT_ID = 1  # MLB
CONCURRENCY = 1  # serial — avoids PG deadlocks from concurrent row updates


async def backfill(start_from: date | None = None):
    dsn = settings.database_url.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(dsn, pool_size=3, max_overflow=5)

    try:
        async with engine.connect() as conn:
            where_clause = "(home_pitcher_name IS NULL OR away_pitcher_name IS NULL)"
            if start_from:
                where_clause += " AND date::date >= CAST(:start_date AS date)"
            
            query = f"""
                SELECT DISTINCT date::date
                FROM mlb.games
                WHERE {where_clause}
                ORDER BY date::date ASC
            """
            binds = {"start_date": start_from.isoformat() if start_from else None}
            log.info(f"Binds: {binds}")
            r = await conn.execute(text(query), binds)
            all_dates = [row[0] for row in r.fetchall()]
            if all_dates:
                log.info(f"Date range: {all_dates[0]} to {all_dates[-1]} ({len(all_dates)} dates)")

        log.info(f"Found {len(all_dates)} dates with missing pitcher data")
        if not all_dates:
            log.info("Nothing to backfill!")
            return

        # Process dates with concurrent API fetches
        sem = asyncio.Semaphore(CONCURRENCY)
        total_updated = 0
        total_days = len(all_dates)
        batch_done = 0

        async def process_day(d: date) -> int:
            nonlocal total_updated
            date_str = d.isoformat()  # YYYY-MM-DD
            url = f"{STATS_API}/api/v1/schedule?date={date_str}&sportId={SPORT_ID}&hydrate=probablePitcher"

            async with sem:
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=15.0) as client:
                        resp = await client.get(url)
                        if resp.status_code != 200:
                            log.warning(f"  API {resp.status_code} for {date_str}, skipping")
                            return 0
                        data = resp.json()
                except Exception as e:
                    log.warning(f"  API error for {date_str}: {e}")
                    return 0

            games = []
            for d_entry in data.get("dates", []):
                for game in d_entry.get("games", []):
                    hp = (
                        game.get("teams", {})
                        .get("home", {})
                        .get("probablePitcher", {})
                        .get("fullName")
                    )
                    ap = (
                        game.get("teams", {})
                        .get("away", {})
                        .get("probablePitcher", {})
                        .get("fullName")
                    )
                    game_pk = game.get("gamePk")
                    if not game_pk:
                        continue
                    games.append((game_pk, hp, ap))

            if not games:
                return 0

            # Batch update the DB
            updated = 0
            async with engine.connect() as conn:
                for game_pk, hp, ap in games:
                    if not hp and not ap:
                        continue
                    updates = []
                    if hp:
                        updates.append("home_pitcher_name = :hp")
                    if ap:
                        updates.append("away_pitcher_name = :ap")
                    set_clause = ", ".join(updates)

                    r = await conn.execute(
                        text("""
                            UPDATE mlb.games
                            SET home_pitcher_name = COALESCE(CAST(:hp AS VARCHAR), home_pitcher_name),
                                away_pitcher_name = COALESCE(CAST(:ap AS VARCHAR), away_pitcher_name)
                            WHERE mlb_game_id = :game_pk
                              AND (home_pitcher_name IS NULL OR away_pitcher_name IS NULL)
                        """),
                        {"game_pk": game_pk, "hp": hp, "ap": ap},
                    )
                    updated += r.rowcount if r.rowcount else 0

                await conn.commit()

            return updated

        # Process dates in batches (API calls are concurrent within batch)
        # but commits are sequential to avoid DB contention
        batch_size = 50
        for i in range(0, len(all_dates), batch_size):
            batch = all_dates[i : i + batch_size]
            results = await asyncio.gather(*[process_day(d) for d in batch])
            day_updated = sum(results)
            total_updated += day_updated
            batch_done = min(i + batch_size, total_days)
            pct = batch_done / total_days * 100
            log.info(
                f"  [{batch_done}/{total_days} days, {pct:.0f}%] "
                f"Updated {day_updated} pitchers in this batch ({total_updated} total)"
            )

        log.info(f"✅ Backfill complete — updated {total_updated} pitcher entries across {total_days} days")

    except Exception as e:
        log.error(f"Backfill failed: {e}")
        raise
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = ArgumentParser(description="Backfill MLB pitcher names")
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Start date (YYYY-MM-DD) to resume from",
    )
    args = parser.parse_args()
    start = date.fromisoformat(args.start) if args.start else None

    asyncio.run(backfill(start_from=start))
