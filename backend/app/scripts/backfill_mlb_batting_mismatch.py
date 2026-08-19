"""Backfill MLB batting boxscores that don't sum to the final game score.

Root cause (fixed in boxscore_ingest.py): the batting ingest skipped any player
with atBats==0, which wrongly dropped pinch-runners (PA=0, AB=0, R=1) and
walk/HBP-only batters. That made team batting boxscores come up short vs the
real final score (e.g. Houston game 824159: boxscore summed to 9 but scored 10),
which in turn made the rf / venue_rf_r10 features (and everything derived from
boxscore runs) wrong.

This script:
  1. Finds every FINAL game where either team's batting boxscore != its real
     score in mlb.games.
  2. Deletes that game's batting rows (clean rebuild).
  3. Re-ingests via process_game() with the fixed skip logic.
  4. Re-checks; reports remaining mismatches.

Usage:
    venv/bin/python app/scripts/backfill_mlb_batting_mismatch.py [--limit N]
"""
import asyncio
import argparse
import asyncpg

from app.ingestion.boxscore_ingest import DB, fetch_boxscore, process_game, create_table_if_not_exists


async def find_mismatched_games(conn, limit: int = 0) -> list[dict]:
    """Return games where either team-side boxscore != actual score."""
    rows = await conn.fetch("""
        WITH sides AS (
            SELECT bgs.game_id, bgs.team_side, SUM(bgs.runs) AS box_runs
            FROM mlb.batting_game_stats bgs
            GROUP BY bgs.game_id, bgs.team_side
        )
        SELECT g.id, g.mlb_game_id, g.date::date AS date,
               ht.abbreviation AS ha, at.abbreviation AS aa
        FROM mlb.games g
        JOIN sides s ON s.game_id = g.id
        JOIN mlb.teams ht ON ht.id = g.home_team_id
        JOIN mlb.teams at ON at.id = g.away_team_id
        WHERE g.status = 'FINAL'
          AND g.mlb_game_id IS NOT NULL
          AND (
               (s.team_side='home' AND s.box_runs <> g.home_score)
            OR (s.team_side='away' AND s.box_runs <> g.away_score)
          )
        GROUP BY g.id, g.mlb_game_id, g.date, ht.abbreviation, at.abbreviation
        ORDER BY g.date
    """)
    if limit > 0:
        rows = rows[:limit]
    return [dict(r) for r in rows]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="max games to process")
    args = parser.parse_args()

    conn = await asyncpg.connect(DB)
    await create_table_if_not_exists(conn)

    games = await find_mismatched_games(conn, args.limit)
    print(f"Found {len(games)} games with boxscore mismatches")

    fixed = 0
    errors = 0
    for i, game in enumerate(games):
        # Clean rebuild for this game
        await conn.execute("DELETE FROM mlb.batting_game_stats WHERE game_id = $1", game["id"])
        nrows = await process_game(conn, game)
        if nrows:
            fixed += 1
        else:
            errors += 1
        if (i + 1) % 25 == 0:
            print(f"  [{i+1}/{len(games)}] {game['date']} {game['ha']}@{game['aa']} -> {nrows} rows (fixed={fixed}, err={errors})")

    print(f"\nRe-processed {len(games)} games: {fixed} got rows, {errors} errored")

    # Re-verify
    remaining = await conn.fetch("""
        WITH sides AS (
            SELECT bgs.game_id, bgs.team_side, SUM(bgs.runs) AS box_runs
            FROM mlb.batting_game_stats bgs
            GROUP BY bgs.game_id, bgs.team_side
        )
        SELECT COUNT(*) AS n
        FROM mlb.games g JOIN sides s ON s.game_id = g.id
        WHERE g.status='FINAL' AND (
             (s.team_side='home' AND s.box_runs <> g.home_score)
          OR (s.team_side='away' AND s.box_runs <> g.away_score)
        )
    """)
    print(f"Remaining mismatched sides after backfill: {remaining[0]['n']}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
