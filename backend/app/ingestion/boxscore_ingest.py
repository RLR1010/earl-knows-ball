"""
Ingest per-game batting box scores from the MLB Stats API.

For each game with mlb_game_id populated, fetches the boxscore and stores
every player's batting line in mlb.batting_game_stats.

Usage:
    # Full run (2021-present):
    docker exec earl-knows-football-api-1 python -m app.ingestion.boxscore_ingest --from 2021

    # Single season:
    docker exec earl-knows-football-api-1 python -m app.ingestion.boxscore_ingest --from 2026

    # Re-check missing games only:
    docker exec earl-knows-football-api-1 python -m app.ingestion.boxscore_ingest --fill-missing
"""

import asyncio
import logging
import sys
import argparse
import time
from datetime import datetime as dt
from typing import Optional

import asyncpg
import urllib.request
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("earl.boxscore_ingest")

DB = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"
API_BASE = "https://statsapi.mlb.com/api/v1/game"


async def create_table_if_not_exists(conn):
    """Ensure the batting_game_stats table exists."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS mlb.batting_game_stats (
            id SERIAL PRIMARY KEY,
            game_id INTEGER REFERENCES mlb.games(id) ON DELETE CASCADE NOT NULL,
            player_id INTEGER REFERENCES mlb.players(id) ON DELETE CASCADE,
            team_side VARCHAR(4) NOT NULL,

            -- Core counting stats
            plate_appearances INTEGER DEFAULT 0,
            at_bats INTEGER DEFAULT 0,
            runs INTEGER DEFAULT 0,
            hits INTEGER DEFAULT 0,
            doubles INTEGER DEFAULT 0,
            triples INTEGER DEFAULT 0,
            home_runs INTEGER DEFAULT 0,
            runs_batted_in INTEGER DEFAULT 0,
            base_on_balls INTEGER DEFAULT 0,
            intentional_walks INTEGER DEFAULT 0,
            strikeouts INTEGER DEFAULT 0,
            stolen_bases INTEGER DEFAULT 0,
            caught_stealing INTEGER DEFAULT 0,
            hit_by_pitch INTEGER DEFAULT 0,
            sacrifice_flies INTEGER DEFAULT 0,
            sacrifice_bunts INTEGER DEFAULT 0,
            left_on_base INTEGER DEFAULT 0,
            total_bases INTEGER DEFAULT 0,

            -- Batted ball type
            ground_outs INTEGER DEFAULT 0,
            air_outs INTEGER DEFAULT 0,
            fly_outs INTEGER DEFAULT 0,
            line_outs INTEGER DEFAULT 0,
            pop_outs INTEGER DEFAULT 0,

            -- Situational
            ground_into_double_play INTEGER DEFAULT 0,
            ground_into_triple_play INTEGER DEFAULT 0,
            catchers_interference INTEGER DEFAULT 0,
            pickoffs INTEGER DEFAULT 0,

            -- Derived slash line
            avg DOUBLE PRECISION,
            obp DOUBLE PRECISION,
            slg DOUBLE PRECISION,
            ops DOUBLE PRECISION,

            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(game_id, player_id)
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bgs_game ON mlb.batting_game_stats(game_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_bgs_player ON mlb.batting_game_stats(player_id)")
    logger.info("Table mlb.batting_game_stats ready")


async def get_games_to_process(conn, from_year: Optional[int], fill_missing: bool) -> list[dict]:
    """Get games that need boxscore data."""
    if fill_missing:
        year_filter = from_year if from_year else 2021
        rows = await conn.fetch("""
            SELECT g.id, g.mlb_game_id, g.date::date, ht.abbreviation as ha, at.abbreviation as aa
            FROM mlb.games g
            JOIN mlb.seasons s ON s.id = g.season_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.mlb_game_id IS NOT NULL
              AND g.home_score IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM mlb.batting_game_stats bgs WHERE bgs.game_id = g.id)
              AND s.year >= $1
            ORDER BY g.date
        """, year_filter)
    elif from_year:
        rows = await conn.fetch("""
            SELECT g.id, g.mlb_game_id, g.date::date, ht.abbreviation as ha, at.abbreviation as aa
            FROM mlb.games g
            JOIN mlb.seasons s ON s.id = g.season_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.mlb_game_id IS NOT NULL
              AND g.home_score IS NOT NULL
              AND s.year >= $1
            ORDER BY g.date
        """, from_year)
    else:
        # Most recent season
        rows = await conn.fetch("""
            SELECT g.id, g.mlb_game_id, g.date::date, ht.abbreviation as ha, at.abbreviation as aa
            FROM mlb.games g
            JOIN mlb.seasons s ON s.id = g.season_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.mlb_game_id IS NOT NULL
              AND g.home_score IS NOT NULL
              AND s.year = (SELECT MAX(year) FROM mlb.seasons)
            ORDER BY g.date
        """)
    logger.info(f"Found {len(rows)} games to process")
    return [dict(r) for r in rows]


def fetch_boxscore(mlb_game_id: int) -> Optional[dict]:
    """Fetch boxscore from MLB Stats API, with retry."""
    url = f"{API_BASE}/{mlb_game_id}/boxscore"
    for attempt in range(3):
        try:
            req = urllib.request.urlopen(url, timeout=15)
            return json.loads(req.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            logger.warning(f"  HTTP {e.code} for game {mlb_game_id}, attempt {attempt+1}")
        except Exception as e:
            logger.warning(f"  Error for game {mlb_game_id}: {e}, attempt {attempt+1}")
        time.sleep(1)
    return None


def lookup_player_id(conn, mlb_id: int, player_name: str) -> Optional[int]:
    """Find our player_id from mlb_id or name."""
    # Try by mlb_id first
    if mlb_id:
        r = conn.fetchrow("SELECT id FROM mlb.players WHERE mlb_id = $1", mlb_id)
        if r:
            return r["id"]
    # Try by name
    if player_name:
        r = conn.fetchrow(
            "SELECT id FROM mlb.players WHERE name ILIKE $1 LIMIT 1",
            player_name.replace("'", "''"),
        )
        if r:
            return r["id"]
    return None


async def process_game(conn, game: dict) -> int:
    """Fetch and store boxscore for one game. Returns number of batting rows saved."""
    mlb_gid = game["mlb_game_id"]
    our_gid = game["id"]

    box = fetch_boxscore(mlb_gid)
    if box is None:
        return 0

    teams = box.get("teams", {})
    rows_saved = 0

    for side in ["home", "away"]:
        team_data = teams.get(side, {})
        players = team_data.get("players", {})

        for pid_str, pdata in players.items():
            stats = pdata.get("stats", {}).get("batting", {})
            if not stats or stats.get("atBats", 0) == 0:
                continue

            person = pdata.get("person", {})
            mlb_player_id = person.get("id")
            full_name = person.get("fullName", "")

            # Lookup our player_id
            our_pid = None
            if mlb_player_id:
                r = await conn.fetchrow(
                    "SELECT id FROM mlb.players WHERE mlb_id = $1", mlb_player_id
                )
                if r:
                    our_pid = r["id"]

            # ---- core counting ----
            pa = stats.get("plateAppearances", 0)
            ab = stats.get("atBats", 0)
            runs = stats.get("runs", 0)
            h = stats.get("hits", 0)
            dbl = stats.get("doubles", 0)
            tri = stats.get("triples", 0)
            hr = stats.get("homeRuns", 0)
            rbi = stats.get("rbi", 0)
            bb = stats.get("baseOnBalls", 0)
            ibb = stats.get("intentionalWalks", 0)
            so = stats.get("strikeOuts", 0)
            sb = stats.get("stolenBases", 0)
            cs = stats.get("caughtStealing", 0)
            hbp = stats.get("hitByPitch", 0)
            sf = stats.get("sacrificeFlies", 0)
            sh = stats.get("sacrificeBunts", 0)
            lob = stats.get("leftOnBase", 0)
            tb = stats.get("totalBases", 0)

            # ---- batted ball type ----
            go = stats.get("groundOuts", 0)
            ao = stats.get("airOuts", 0)
            fo = stats.get("flyOuts", 0)
            lo = stats.get("lineOuts", 0)
            po = stats.get("popOuts", 0)

            # ---- situational ----
            gidp = stats.get("groundIntoDoublePlay", 0)
            gitp = stats.get("groundIntoTriplePlay", 0)
            ci = stats.get("catchersInterference", 0)
            pick = stats.get("pickoffs", 0)

            # ---- derived slash line ----
            # MLB Stats API doesn't return avg/obp/slg/ops in the boxscore endpoint,
            # so we compute them from the captured counting stats.
            avg = round(h / ab, 3) if ab > 0 else None
            obp_denom = ab + bb + hbp + sf
            obp = round((h + bb + hbp) / obp_denom, 3) if obp_denom > 0 else None
            slg = round(tb / ab, 3) if ab > 0 else None
            ops = round((obp or 0) + (slg or 0), 3) if (obp and slg) else None

            try:
                await conn.execute("""
                    INSERT INTO mlb.batting_game_stats
                        (game_id, player_id, team_side,
                         plate_appearances, at_bats, runs, hits, doubles, triples, home_runs, runs_batted_in,
                         base_on_balls, intentional_walks, strikeouts, stolen_bases, caught_stealing,
                         hit_by_pitch, sacrifice_flies, sacrifice_bunts, left_on_base, total_bases,
                         ground_outs, air_outs, fly_outs, line_outs, pop_outs,
                         ground_into_double_play, ground_into_triple_play, catchers_interference, pickoffs,
                         avg, obp, slg, ops)
                    VALUES ($1, $2, $3,
                            $4, $5, $6, $7, $8, $9, $10, $11,
                            $12, $13, $14, $15, $16, $17, $18, $19, $20, $21,
                            $22, $23, $24, $25, $26, $27, $28, $29, $30,
                            $31, $32, $33, $34)
                    ON CONFLICT (game_id, player_id) DO NOTHING
                """, our_gid, our_pid, side,
                   pa, ab, runs, h, dbl, tri, hr, rbi, bb, ibb, so, sb, cs, hbp, sf, sh, lob, tb,
                   go, ao, fo, lo, po, gidp, gitp, ci, pick,
                   avg, obp, slg, ops)
                rows_saved += 1
            except Exception as e:
                logger.warning(f"  Insert error for game {our_gid} player {full_name}: {e}")

    return rows_saved


async def main():
    parser = argparse.ArgumentParser(description="Ingest MLB boxscore data")
    parser.add_argument("--from", dest="from_year", type=int, help="Start year")
    parser.add_argument("--fill-missing", action="store_true", help="Only process games without boxscore data")
    parser.add_argument("--limit", type=int, default=0, help="Max games to process (for testing)")
    args = parser.parse_args()

    conn = await asyncpg.connect(DB)
    await create_table_if_not_exists(conn)

    games = await get_games_to_process(conn, args.from_year, args.fill_missing)

    if args.limit > 0:
        games = games[:args.limit]

    logger.info(f"Processing {len(games)} games...")
    total_rows = 0
    errors = 0
    start = time.time()

    for i, game in enumerate(games):
        nrows = await process_game(conn, game)
        total_rows += nrows
        if nrows == 0:
            errors += 1

        if (i + 1) % 25 == 0:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed
            logger.info(
                f"  [{i+1}/{len(games)}] {game['date']} {game['ha']}@{game['aa']} "
                f"-> {nrows} rows (total={total_rows}, errors={errors}, {rate:.1f} games/s)"
            )

    elapsed = time.time() - start
    logger.info(
        f"\nDone: {len(games)} games processed, {total_rows} batting rows saved, "
        f"{errors} errors in {elapsed:.0f}s ({len(games)/elapsed:.1f} games/s)"
    )

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
