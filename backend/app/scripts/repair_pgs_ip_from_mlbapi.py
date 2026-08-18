"""Repair MLB pitcher_game_stats.ip + boxscore pitching stats for seasons 7-14.

Historical pgs.ip (2012-2019) is broadly corrupted (inflated vs the authoritative MLB
StatsAPI). Re-fetches every completed game in those seasons, parses with the SAME
production logic (mlb_pitcher_stats.parse_pitchers -> decimal innings), and OVERWRITES
the boxscore pitching columns.

The standard ingest's ON CONFLICT DO UPDATE does NOT update ip/er/h/hr/k/bb, so a plain
reprocess won't fix ip. This script updates them explicitly.

Rate-limited + resumable (re-fetch is idempotent). TEST FIRST with --limit N.

Usage: cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/repair_pgs_ip_from_mlbapi.py [--limit N]
"""
import argparse
import asyncio
import time

import httpx
import sqlalchemy as sa

from app.database import async_session
from app.ingestion.mlb_pitcher_stats import fetch_game_boxscore, parse_pitchers

MIN_INTERVAL = 0.12


def log(msg: str) -> None:
    print(msg, flush=True)


async def repair(limit: int) -> None:
    t0 = time.time()
    fetched = updated = errors = no_row = 0

    async with async_session() as db:
        r = await db.execute(sa.text("""
            SELECT g.id, g.mlb_game_id, s.year, g.home_team_id, g.away_team_id
            FROM mlb.games g
            JOIN mlb.seasons s ON s.id = g.season_id
            WHERE g.mlb_game_id IS NOT NULL
              AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
              AND s.year BETWEEN 2012 AND 2019
            ORDER BY s.year, g.date, g.id
        """))
        games = [dict(row._mapping) for row in r.fetchall()]
        if limit:
            games = games[:limit]
        abbr_sql = sa.text("SELECT abbreviation FROM mlb.teams WHERE id = :tid")
        abbrs = {}
        for tid in {g["home_team_id"] for g in games} | {g["away_team_id"] for g in games}:
            a = (await db.execute(abbr_sql, {"tid": tid})).scalar()
            abbrs[tid] = a

    log(f"Games to re-fetch (2012-2019): {len(games)}")

    update_sql = sa.text("""
        UPDATE mlb.pitcher_game_stats SET
            ip = :ip, er = :er, runs_allowed = :runs_allowed,
            h = :h, hr = :hr, k = :k, bb = :bb, strikes = :strikes,
            batters_faced = :batters_faced
        WHERE mlb_game_id = :mlb_game_id AND pitcher_mlb_id = :pitcher_mlb_id
    """)

    async with httpx.AsyncClient(timeout=20) as client:
        async with async_session() as db:
            for i, g in enumerate(games, 1):
                try:
                    box = await fetch_game_boxscore(client, g["mlb_game_id"])
                except Exception as e:
                    log(f"  fetch err game {g['id']} ({g['mlb_game_id']}): {e}")
                    errors += 1
                    await asyncio.sleep(MIN_INTERVAL)
                    continue
                if not box:
                    errors += 1
                    if i % 200 == 0:
                        progress(i, len(games), fetched, updated, errors, no_row, t0)
                    await asyncio.sleep(MIN_INTERVAL)
                    continue

                try:
                    away_p = parse_pitchers(box, "away", abbrs[g["away_team_id"]])
                    home_p = parse_pitchers(box, "home", abbrs[g["home_team_id"]])
                except Exception as e:
                    log(f"  parse err game {g['id']}: {e}")
                    errors += 1
                    await asyncio.sleep(MIN_INTERVAL)
                    continue

                for p in away_p + home_p:
                    res = await db.execute(update_sql, {
                        "mlb_game_id": g["mlb_game_id"],
                        "pitcher_mlb_id": p["pitcher_mlb_id"],
                        "ip": p.get("ip"),
                        "er": p.get("er"),
                        "runs_allowed": p.get("runs_allowed"),
                        "h": p.get("h"),
                        "hr": p.get("hr"),
                        "k": p.get("k"),
                        "bb": p.get("bb"),
                        "strikes": p.get("strikes", 0),
                        "batters_faced": p.get("batters_faced", 0),
                    })
                    if res.rowcount and res.rowcount > 0:
                        updated += 1
                    else:
                        no_row += 1
                fetched += 1
                if i % 20 == 0:
                    await db.commit()
                if i % 200 == 0:
                    progress(i, len(games), fetched, updated, errors, no_row, t0)
                await asyncio.sleep(MIN_INTERVAL)
            await db.commit()

    log(f"\nDONE in {(time.time()-t0)/60:.1f}m  fetched={fetched} ip_updated={updated} "
        f"no_row={no_row} errors={errors}")


def progress(i, total, fetched, updated, errors, no_row, t0):
    log(f"  [{i}/{total}] fetched={fetched} ip_updated={updated} errors={errors} "
        f"no_row={no_row}  {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process first N games (test)")
    args = ap.parse_args()
    asyncio.run(repair(args.limit or None))
