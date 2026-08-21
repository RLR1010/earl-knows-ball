"""
Complete the 2020 regular-season boxscore gap.

Root cause: 69 covid-rescheduled 2020 games were stored in mlb.games with
status='SCHEDULED' (NULL scores) and never went through boxscore ingest, even
though they were actually played. They have real FINAL results in the MLB API.
This script:
  1. Finds all 2020 R games in mlb.games whose status != 'FINAL'.
  2. Confirms each has a real Final result in the MLB API (else skip).
  3. Updates status='FINAL' + home/away scores + mlb_game_id.
  4. Runs the standard boxscore ingest (process_game, process_pitchers) to add
     batting + pitching rows for every player.

Idempotent / resumable: only touches non-FINAL games; re-running is a no-op.

Usage (non-login shell):
    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    ./venv/bin/python app/scripts/backfill_2020_games.py --limit 6   # test
    ./venv/bin/python app/scripts/backfill_2020_games.py              # full
"""
import asyncio, sys, os, time, logging, argparse
import asyncpg, httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core.config import settings
from app.ingestion.boxscore_ingest import (
    process_game,
    process_pitchers,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("earl.backfill2020")

DB = settings.database_url_sync
DB = DB.replace("postgresql+psycopg2://", "postgresql://")

GAMES_SQL = """
    SELECT g.id, g.mlb_game_id, g.date::date AS d, g.status, g.home_score, g.away_score
    FROM mlb.games g
    WHERE g.season_id = 15 AND g.game_type = 'R' AND g.status <> 'FINAL'
    ORDER BY g.date, g.id
"""


async def api_final(client, pk) -> bool:
    try:
        r = await client.get(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore")
        if r.status_code != 200:
            return False
        d = r.json()
        home = d.get("teams", {}).get("home", {})
        away = d.get("teams", {}).get("away", {})
        home_runs = (home.get("teamStats") or {}).get("batting", {}).get("runs")
        away_runs = (away.get("teamStats") or {}).get("batting", {}).get("runs")
        return home_runs is not None and away_runs is not None
    except Exception:
        return False


async def get_api_scores(client, pk):
    r = await client.get(f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore")
    d = r.json()
    home = d.get("teams", {}).get("home", {})
    away = d.get("teams", {}).get("away", {})
    return {
        "home": (home.get("teamStats") or {}).get("batting", {}).get("runs"),
        "away": (away.get("teamStats") or {}).get("batting", {}).get("runs"),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = await asyncpg.connect(DB)
    games = await conn.fetch(GAMES_SQL)
    logger.info(f"Found {len(games)} non-FINAL 2020 R games in mlb.games")
    if args.limit:
        games = games[: args.limit]

    finalized = 0
    skipped = 0
    errors = 0
    t0 = time.time()

    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        for i, g in enumerate(games, 1):
            pk = g["mlb_game_id"]
            if not pk:
                logger.warning(f"  [{i}] {g['d']} game {g['id']} has no mlb_game_id, skip")
                skipped += 1
                continue
            try:
                if not await api_final(client, pk):
                    logger.info(f"  [{i}] {g['d']} pk={pk} NOT final in API, skip")
                    skipped += 1
                    continue
                scores = await get_api_scores(client, pk)
                # mark FINAL
                await conn.execute(
                    "UPDATE mlb.games SET status='FINAL', home_score=$1, away_score=$2 WHERE id=$3",
                    scores["home"], scores["away"], g["id"],
                )
                game_dict = {"id": g["id"], "mlb_game_id": pk, "date": str(g["d"])}
                bat = await process_game(conn, game_dict)
                pit = await process_pitchers(conn, game_dict)
                finalized += 1
                if i % 5 == 0:
                    el = time.time() - t0
                    logger.info(f"  [{i}/{len(games)}] finalized {g['id']} pk={pk} {g['d']} "
                                f"({finalized} ok, {skipped} skip, {errors} err, {i/el:.1f}/s)")
            except Exception as e:
                errors += 1
                logger.warning(f"  [{i}] {g['d']} pk={pk} ERR {e}")

    el = time.time() - t0
    logger.info(f"DONE: finalized={finalized}, skipped={skipped}, errors={errors} in {el:.0f}s")
    await conn.close()


asyncio.run(main())
