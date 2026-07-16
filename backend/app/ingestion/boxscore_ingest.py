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


def fetch_game_weather(mlb_game_id: int) -> Optional[dict]:
    """Fetch weather data from MLB Stats API live feed."""
    import urllib.request
    url = f"https://statsapi.mlb.com/api/v1.1/game/{mlb_game_id}/feed/live"
    try:
        req = urllib.request.urlopen(url, timeout=15)
        data = json.loads(req.read())
        return data.get("gameData", {}).get("weather", {})
    except Exception:
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


async def process_pitchers(conn, game: dict) -> int:
    """Fetch and store boxscore pitcher stats for one game."""
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
            stats = pdata.get("stats", {}).get("pitching", {})
            if not stats or stats.get("inningsPitched", "0") in ("0", "0.0", ""):
                continue

            person = pdata.get("person", {})
            mlb_player_id = person.get("id")
            full_name = person.get("fullName", "")

            our_pid = None
            if mlb_player_id:
                r = await conn.fetchrow(
                    "SELECT id FROM mlb.players WHERE mlb_id = $1", mlb_player_id
                )
                if r:
                    our_pid = r["id"]

            ip_str = stats.get("inningsPitched", "0")
            ip = 0.0
            if ip_str:
                parts = ip_str.split(".")
                if len(parts) == 2:
                    ip = float(parts[0]) + int(parts[1]) / 3
                else:
                    ip = float(parts[0])

            er = stats.get("earnedRuns", 0)
            h = stats.get("hits", 0)
            hr = stats.get("homeRuns", 0)
            bb = stats.get("baseOnBalls", 0)
            ibb = stats.get("intentionalWalks", 0)
            so = stats.get("strikeOuts", 0)
            hbp = stats.get("hitByPitch", 0)
            wp = stats.get("wildPitches", 0)
            balk = stats.get("balks", 0)
            runs_allowed = stats.get("runs", 0)
            pitches_thrown = stats.get("numberOfPitches", 0)
            strikes = stats.get("strikes", 0)
            go = stats.get("groundOuts", 0)
            ao = stats.get("airOuts", 0)
            fo = stats.get("flyOuts", 0)
            gidp = stats.get("groundIntoDoublePlay", 0)
            inherited_runners = stats.get("inheritedRunners", 0)
            inherited_scored = stats.get("inheritedRunnersScored", 0)
            is_starter = stats.get("gamesStarted", 0) > 0
            decision = stats.get("note", "")

            team_abbr = team_data.get("team", {}).get("abbreviation", "")

            try:
                await conn.execute("""
                    INSERT INTO mlb.pitcher_game_stats
                        (game_id, mlb_game_id, pitcher_mlb_id, team_abbr, pitcher_name,
                         ip, er, h, home_runs, base_on_balls, intentional_walks,
                         strikeouts, hit_by_pitch, wild_pitches, balks,
                         runs_allowed, pitches_thrown, strikes,
                         ground_outs, air_outs, fly_outs,
                         ground_into_double_play,
                         is_starter, decision)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                            $11, $12, $13, $14, $15, $16, $17,
                            $18, $19, $20, $21, $22, $23,
                            $24)
                    ON CONFLICT (mlb_game_id, pitcher_mlb_id) WHERE pitcher_mlb_id IS NOT NULL
                    DO UPDATE SET
                        game_id = EXCLUDED.game_id,
                        ip = EXCLUDED.ip,
                        er = EXCLUDED.er,
                        h = EXCLUDED.h,
                        base_on_balls = EXCLUDED.base_on_balls,
                        strikeouts = EXCLUDED.strikeouts,
                        runs_allowed = EXCLUDED.runs_allowed,
                        is_starter = EXCLUDED.is_starter
                """,
                    our_gid, mlb_gid, mlb_player_id, team_abbr, full_name,
                    ip, er, h, hr, bb, ibb,
                    so, hbp, wp, balk,
                    runs_allowed, pitches_thrown, strikes,
                    go, ao, fo, gidp,
                    is_starter, decision)
                rows_saved += 1
            except Exception as e:
                logger.warning(f"  Pitcher insert error for game {our_gid} player {full_name}: {e}")

    return rows_saved


async def refresh_boxscores_for_recent_games(conn) -> dict:
    """Load boxscore data for today's and yesterday's FINAL games.
    process_game() handles both batting AND pitching from the boxscore.
    """
    rows = await conn.fetch("""
        SELECT id, mlb_game_id
        FROM mlb.games
        WHERE status = 'FINAL'
          AND date >= CURRENT_DATE - INTERVAL '3 days'
          AND date <= CURRENT_DATE + INTERVAL '1 day'
          AND (
              NOT EXISTS (SELECT 1 FROM mlb.batting_game_stats bgs WHERE bgs.game_id = mlb.games.id)
              OR NOT EXISTS (SELECT 1 FROM mlb.pitcher_game_stats pgs WHERE pgs.game_id = mlb.games.id)
              OR NOT EXISTS (SELECT 1 FROM mlb.pitcher_game_stats pgs WHERE pgs.game_id = mlb.games.id AND pgs.is_starter = true)
          )
        ORDER BY date DESC
        LIMIT 50
    """)
    games = [dict(r) for r in rows]

    total_batting = 0
    total_pitching = 0
    for game in games:
        try:
            batting_result = await process_game(conn, game)
            total_batting += batting_result
            total_pitching += await process_pitchers(conn, game)
        except Exception as e:
            logger.warning(f"  Error processing game {game['id']}: {e}")

    # Step 2: Update weather for all recent games (even if boxscores already loaded)
    logger.info("  Updating weather for recent games...")
    weather_games = await conn.fetch("""
        SELECT id, mlb_game_id
        FROM mlb.games
        WHERE date >= CURRENT_DATE - INTERVAL '2 days'
          AND date <= CURRENT_DATE + INTERVAL '1 day'
        ORDER BY date DESC
        LIMIT 100
    """)
    weather_updated = 0
    for wg in weather_games:
        try:
            wth = fetch_game_weather(wg["mlb_game_id"])
            if wth:
                temp_str = wth.get("temp", "")
                condition = wth.get("condition")
                wind_str = wth.get("wind", "")

                # Parse temperature from string
                try:
                    temperature = int(temp_str) if temp_str else None
                except (ValueError, TypeError):
                    temperature = None

                # Parse wind speed and direction from string like "5 mph, Out To CF"
                wind_speed = None
                wind_direction = None
                if wind_str:
                    try:
                        wind_speed = int("".join(c for c in wind_str.split(",")[0] if c.isdigit() or c in ".-"))
                    except (ValueError, IndexError):
                        pass

                    if "," in wind_str:
                        wpart = wind_str.split(",", 1)[1].strip().lower()
                        if "out" in wpart:
                            wind_direction = "out"
                        elif "in" in wpart:
                            wind_direction = "in"
                        if not wind_direction:
                            if wpart.startswith("l") and "r" in wpart:
                                wind_direction = "l_to_r"
                            elif wpart.startswith("r") and "l" in wpart:
                                wind_direction = "r_to_l"

                await conn.execute("""
                    UPDATE mlb.games
                    SET temperature = $1, weather_condition = $2, wind_speed = $3, wind_direction = $4
                    WHERE id = $5
                """, temperature, condition, wind_speed, wind_direction, wg["id"])
                weather_updated += 1
        except Exception as e:
            logger.warning(f"  Weather error for game {wg['id']}: {e}")
    logger.info(f"  Weather updated for {weather_updated} games")

    return {
        "batting_rows": total_batting,
        "pitching_rows": total_pitching,
        "games_processed": len(games),
        "weather_updated": weather_updated,
    }


# ---- Backfill: batch loading (standalone use) ----

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


async def update_prediction_results(pg_conn) -> int:
    """
    Update mlb.game_predictions with actual scores and results for FINAL games
    that have predictions but haven't had actual results populated yet.

    Returns the number of predictions updated.
    """
    rows = await pg_conn.fetch(
        """
        SELECT gp.game_id,
               gp.predicted_home_runs, gp.predicted_away_runs,
               gp.predicted_margin,     gp.predicted_total,
               gp.run_line_pick, gp.ou_pick, gp.ml_pick,
               gp.rl_conf, gp.ml_conf, gp.ou_conf,
               g.home_score, g.away_score,
               blc.closing_spread,
               blc.closing_spread_home_odds, blc.closing_spread_away_odds,
               blc.closing_ou,
               blc.closing_over_odds, blc.closing_under_odds,
               blc.closing_home_ml, blc.closing_away_ml
        FROM mlb.game_predictions gp
        JOIN mlb.games g ON gp.game_id = g.id
        LEFT JOIN mlb.betting_lines_consolidated blc ON gp.game_id = blc.game_id
        WHERE g.status = 'FINAL'
          AND g.home_score IS NOT NULL
          AND g.away_score IS NOT NULL
          AND gp.actual_home_runs IS NULL
        ORDER BY gp.game_id
        """
    )

    if not rows:
        logger.info("  No predictions need result updates")
        return 0

    def _profit_per_100(odds: float) -> float:
        if odds > 0:
            return odds / 100.0
        return 100.0 / abs(odds)

    updated = 0
    for r in rows:
        gid = r["game_id"]
        # Normalize types: asyncpg returns Decimal for int/float columns
        home_score = float(r["home_score"])
        away_score = float(r["away_score"])
        actual_total = home_score + away_score
        actual_margin = home_score - away_score
        spr = float(r["closing_spread"]) if r["closing_spread"] is not None else None
        vegas_ou = float(r["closing_ou"]) if r["closing_ou"] is not None else None
        predicted_margin = float(r["predicted_margin"]) if r["predicted_margin"] is not None else None

        # --- Run line result ---
        rl_result = None
        if spr is not None and predicted_margin is not None:
            pred_home_covers = (predicted_margin + spr) > 0
            act_home_covers = (actual_margin + spr) > 0
            rl_diff = actual_margin + spr
            if abs(rl_diff) < 0.005:
                rl_result = "Push"
            else:
                rl_result = "Win" if pred_home_covers == act_home_covers else "Loss"

        # --- OU result ---
        ou_result = None
        if vegas_ou is not None and r["ou_pick"] is not None:
            pick_lower = r["ou_pick"].lower()
            ou_picked_over = pick_lower.startswith("over")
            ou_picked_under = pick_lower.startswith("under")

            if abs(actual_total - float(vegas_ou)) < 0.5:
                ou_result = "Push"
            elif ou_picked_over:
                ou_result = "Win" if actual_total > float(vegas_ou) else "Loss"
            elif ou_picked_under:
                ou_result = "Win" if actual_total < float(vegas_ou) else "Loss"

        # --- Moneyline result ---
        ml_result = None
        ml_pick = r["ml_pick"]
        if ml_pick and actual_margin is not None:
            if actual_margin == 0:
                ml_result = "Push"
            elif ml_pick in ("home", "Home"):
                ml_result = "Win" if actual_margin > 0 else "Loss"
            elif ml_pick in ("away", "Away"):
                ml_result = "Win" if actual_margin < 0 else "Loss"

        # --- Profit calculations ---
        def _calc_profit(result: str | None, odds: float | None) -> float | None:
            if result is None or odds is None:
                return None
            if result != "Win":
                return -100.0
            return round(100.0 * _profit_per_100(odds), 2)

        # RL odds: use the side we picked
        rl_odds = None
        if spr is not None and predicted_margin is not None:
            pred_covers = (predicted_margin + spr) > 0
            if pred_covers:
                rl_odds = float(r["closing_spread_home_odds"]) if r["closing_spread_home_odds"] is not None else None
            else:
                rl_odds = float(r["closing_spread_away_odds"]) if r["closing_spread_away_odds"] is not None else None

        # OU odds
        ou_odds = None
        if vegas_ou is not None and r["ou_pick"] is not None:
            pick_lower = r["ou_pick"].lower()
            if pick_lower.startswith("over"):
                ou_odds = float(r["closing_over_odds"]) if r["closing_over_odds"] is not None else None
            elif pick_lower.startswith("under"):
                ou_odds = float(r["closing_under_odds"]) if r["closing_under_odds"] is not None else None

        # ML odds
        ml_odds = None
        if ml_pick:
            if ml_pick in ("home", "Home"):
                ml_odds = float(r["closing_home_ml"]) if r["closing_home_ml"] is not None else None
            elif ml_pick in ("away", "Away"):
                ml_odds = float(r["closing_away_ml"]) if r["closing_away_ml"] is not None else None

        rl_profit = _calc_profit(rl_result, rl_odds)
        ou_profit = _calc_profit(ou_result, ou_odds)
        ml_profit = _calc_profit(ml_result, ml_odds)

        try:
            await pg_conn.execute(
                """
                UPDATE mlb.game_predictions
                SET actual_home_runs = $1,
                    actual_away_runs = $2,
                    actual_total     = $3,
                    actual_margin    = $4,
                    run_line_result  = $5,
                    ou_result        = $6,
                    ml_result        = $7,
                    ats_profit       = $8,
                    ou_profit        = $9,
                    ml_profit        = $10
                WHERE game_id = $11 AND actual_home_runs IS NULL
                """,
                home_score, away_score, actual_total, actual_margin,
                rl_result, ou_result, ml_result,
                rl_profit, ou_profit, ml_profit,
                gid,
            )
            updated += 1
        except Exception as e:
            logger.error(f"  Failed to update prediction for game {gid}: {e}")

    logger.info(f"  Updated {updated} predictions with actual results")
    return updated


if __name__ == "__main__":
    asyncio.run(main())
