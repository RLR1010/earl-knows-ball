"""
Backfill mlb.batting_game_stats.sacrifice_flies / sacrifice_bunts.

The boxscore ingest previously read the wrong API key ("sacrificeFlies" /
"sacrificeBunts") when the MLB StatsAPI boxscore actually returns "sacFlies" /
"sacBunts", so SF/SB were stored as 0 for EVERY row, every season. That silently
inflated OBP/OPS (SF is in the OBP denominator). This re-fetches each game's
boxscore and UPDATEs just those two columns for every batter.

Resumable/idempotent: only UPDATEs rows where the value differs. Safe to re-run.
Skips games that already have any non-zero sacFlies recorded (already fixed).

Usage (non-login shell):
    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    ./venv/bin/python app/scripts/backfill_mlb_sacflies.py --limit 50     # test
    ./venv/bin/python app/scripts/backfill_mlb_sacflies.py                # full 2016-2026
"""
import asyncio, sys, os, time, logging, argparse
import asyncpg
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("earl.sacflies_backfill")

DB = (settings.database_url_sync or "")
if ":" in DB.replace("://", "", 1) and DB.startswith("postgresql"):
    DB = "postgresql://" + DB.split("://", 1)[1]  # asyncpg needs 'postgresql://' not '+psycopg2'
DB = DB.replace("postgresql+psycopg2://", "postgresql://")

GAMES_SQL = """
    SELECT g.id, g.mlb_game_id, g.date::date AS d, ht.abbreviation AS ha, at.abbreviation AS aa
    FROM mlb.games g
    JOIN mlb.teams ht ON ht.id = g.home_team_id
    JOIN mlb.teams at ON at.id = g.away_team_id
    WHERE g.mlb_game_id IS NOT NULL
      AND g.season_id BETWEEN 11 AND 21
      AND NOT EXISTS (
          SELECT 1 FROM mlb.batting_game_stats b JOIN mlb.games gg ON gg.id=b.game_id
          WHERE gg.id = g.id AND b.sacrifice_flies > 0
      )
    ORDER BY g.date
"""


async def fetch_boxscore(client, pk):
    url = f"https://statsapi.mlb.com/api/v1/game/{pk}/boxscore"
    resp = await client.get(url)
    if resp.status_code != 200:
        return None
    return resp.json()


def extract_sf(data):
    """Return {mlb_player_id: (sf, sb)} for batters in the boxscore."""
    out = {}
    for side in ("away", "home"):
        players = data.get("teams", {}).get(side, {}).get("players", {})
        for key, p in players.items():
            st = p.get("stats", {})
            bat = st.get("batting", {}) if isinstance(st, dict) else {}
            if not bat:
                continue
            pid = p.get("person", {}).get("id")
            if pid is None:
                continue
            sf = bat.get("sacFlies", 0) or 0
            sb = bat.get("sacBunts", 0) or 0
            if sf or sb:
                out[pid] = (sf, sb)
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    conn = await asyncpg.connect(DB)
    games = await conn.fetch(GAMES_SQL)
    # map current batting rows: (game_id -> {player_id: batting row id})
    logger.info(f"Discovered {len(games)} games with batting rows lacking sacFlies (>0)")
    if args.limit:
        games = games[: args.limit]

    # pass 1: find every game that has NO sacFlies recorded anywhere on it
    # (these are the games we must re-fetch)
    ok = 0
    no_change = 0
    errors = 0
    t0 = time.time()

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        for i, g in enumerate(games, 1):
            pk = g["mlb_game_id"]
            try:
                data = await fetch_boxscore(client, pk)
                if data is None:
                    errors += 1
                    continue
                sf_map = extract_sf(data)  # {mlb_player_id: (sf,sb)}
                if not sf_map:
                    no_change += 1
                    continue
                # resolve mlb_player_id -> our player_id; update matching rows
                updated = 0
                for mlb_pid, (sf, sb) in sf_map.items():
                    rows = await conn.fetch(
                        "SELECT b.id AS bstat_id FROM mlb.batting_game_stats b "
                        "JOIN mlb.players pl ON pl.id=b.player_id "
                        "WHERE b.game_id=$1 AND pl.mlb_id=$2",
                        g["id"], mlb_pid,
                    )
                    if not rows:
                        continue
                    # update all rows for that player in this game (should be 1)
                    await conn.execute(
                        "UPDATE mlb.batting_game_stats SET sacrifice_flies=$1, sacrifice_bunts=$2 "
                        "WHERE id = ANY($3::int[])",
                        sf, sb, [r["bstat_id"] for r in rows],
                    )
                    updated += 1
                ok += 1
            except Exception as e:
                errors += 1
                logger.warning(f"  [{i}/{len(games)}] game {g['id']} {g['d']} {g['ha']}@{g['aa']} ERR {e}")

            if i % 25 == 0:
                el = time.time() - t0
                rate = i / el
                logger.info(f"  [{i}/{len(games)}] updated-games={ok} done={no_change} errors={errors} "
                            f"({rate:.1f} games/s, ~{(len(games)-i)/rate:.0f}s left)")

    el = time.time() - t0
    logger.info(f"DONE: processed {len(games)} games, updated={ok}, no-changes={no_change}, "
                f"errors={errors} in {el:.0f}s")
    await conn.close()


asyncio.run(main())
