#!/usr/bin/env python3
"""Verify canonical nflverse history faithfulness (accuracy harness).

Checks:
  1. Inventory: per-dataset rows, season coverage.
  2. PBP integrity: points reconstructed from PBP == schedules final scores.
  3. Referential integrity: game_id / player_id joins across datasets.
  4. Era-availability matrix: earliest season each key PBP metric is populated.
  5. Cross-check 2016+ canonical vs pre-existing derived tables (game_stats).

Usage: PYTHONPATH=$PWD python3 app/scripts/verify_nflverse_history.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import asyncpg

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ingestion.nflverse_hub import _dsn  # noqa: E402

TABLES = ["pbp", "stats_team_week", "stats_player_week", "schedules", "injuries_full",
          "snap_counts", "participation", "ngs_passing", "ngs_receiving", "ngs_rushing",
          "ftn_charting", "pfr_advstats_def", "pfr_advstats_pass", "pfr_advstats_rush",
          "pfr_advstats_rec", "roster_weekly", "nflverse_players", "nflverse_teams",
          "draft_picks_ref", "officials", "combine_ref", "contracts_ref"]


async def q(conn, sql, *args):
    return await conn.fetch(sql, *args)


async def main():
    conn = await asyncpg.connect(_dsn())
    try:
        print("=" * 78)
        print("1. INVENTORY")
        print("=" * 78)
        for t in TABLES:
            try:
                n = await conn.fetchval(f"SELECT count(*) FROM nfl.{t}")
                print(f"  nfl.{t:22s} rows={n:>10,}")
            except Exception as e:  # noqa: BLE001
                print(f"  nfl.{t:22s} MISSING ({str(e).splitlines()[0][:50]})")

        print("\n" + "=" * 78)
        print("2. PBP SCORE RECONSTRUCTION vs SCHEDULES FINAL (truth check)")
        print("=" * 78)
        # reconstruct points: touchdowns (6) + XP/2pt (via extra_point/2pt) + FG(3) + safeties
        sql = """
        WITH ps AS (
            SELECT game_id,
                sum(CASE WHEN td_team = posteam THEN 6 ELSE 0 END
                    + CASE WHEN touchdown=1 AND td_team=posteam
                           AND (extra_point_result='good'
                                OR two_point_conv_result IN ('success','good')) THEN
                          CASE WHEN two_point_attempt=1 THEN 2 ELSE 1 END ELSE 0 END) AS p_pass
            FROM nfl.pbp GROUP BY game_id
        )
        SELECT count(*) AS games FROM nfl.schedules
        """
        # simpler robust method: score via 'total_home_score'/'total_away_score' max per game
        rows = await q(conn, """
            SELECT count(*) n,
              count(*) FILTER (WHERE hs IS DISTINCT FROM g.home_score OR aws IS DISTINCT FROM g.away_score) bad
            FROM (
              SELECT game_id, max(total_home_score) hs, max(total_away_score) aws
              FROM nfl.pbp GROUP BY game_id
            ) x
            JOIN nfl.schedules g ON g.game_id = x.game_id
            WHERE g.home_score IS NOT NULL
        """)
        print(f"  games with PBP-derived final vs schedules: {rows[0]['n']:,}  "
              f"mismatches: {rows[0]['bad']:,}")

        print("\n" + "=" * 78)
        print("3. REFERENTIAL INTEGRITY")
        print("=" * 78)
        checks = [
            ("pbp.game_id", "SELECT count(*) FROM nfl.pbp p "
                            "LEFT JOIN nfl.schedules s ON s.game_id=p.game_id WHERE s.game_id IS NULL"),
            ("stats_team_week.game_id", "SELECT count(*) FROM nfl.stats_team_week t "
                                        "LEFT JOIN nfl.schedules s ON s.game_id=t.game_id WHERE s.game_id IS NULL"),
            ("stats_player_week.game_id", "SELECT count(*) FROM nfl.stats_player_week t "
                                          "LEFT JOIN nfl.schedules s ON s.game_id=t.game_id WHERE s.game_id IS NULL"),
            ("snap_counts.game_id", "SELECT count(*) FROM nfl.snap_counts t "
                                    "LEFT JOIN nfl.schedules s ON s.game_id=t.game_id WHERE s.game_id IS NULL"),
        ]
        for label, sql in checks:
            try:
                bad = await conn.fetchval(sql)
                print(f"  {label:28s} orphans={bad:,}")
            except Exception as e:  # noqa: BLE001
                print(f"  {label:28s} ERR {str(e).splitlines()[0][:50]}")

        print("\n" + "=" * 78)
        print("4. ERA AVAILABILITY — earliest season metric is populated (PBP)")
        print("=" * 78)
        metrics = ["epa", "wpa", "success", "cp", "cpoe", "air_yards",
                   "yards_after_catch", "xyac_epa", "passer_player_id",
                   "receiver_player_id", "rusher_player_id", "pass_oe",
                   "qb_dropback", "qb_scramble", "no_huddle", "shotgun",
                   "play_action", "motion"]
        for m in metrics:
            try:
                row = await conn.fetchrow(
                    f"SELECT min(season) FILTER (WHERE {m} IS NOT NULL) mn, "
                    f"count(*) FILTER (WHERE {m} IS NOT NULL) nz FROM nfl.pbp")
                print(f"  {m:22s} first_populated_season={row['mn']}  non_null={row['nz']:,}")
            except Exception as e:  # noqa: BLE001
                print(f"  {m:22s} n/a ({str(e).splitlines()[0][:40]})")

        print("\n" + "=" * 78)
        print("5. CROSS-CHECK 2016+ canonical vs existing game_stats")
        print("=" * 78)
        try:
            rows = await q(conn, """
                SELECT count(*) n,
                  count(*) FILTER (WHERE abs(gs.passing_epa - t.passing_epa) > 1e-6) epa_bad,
                  count(*) FILTER (WHERE gs.pass_yards <> t.passing_yards) py_bad
                FROM nfl.game_stats gs
                JOIN nfl.stats_team_week t
                  ON t.season=gs.season AND t.team=gs.team_abbr
                 AND t.season_type=CASE WHEN gs.season_type='POST' THEN 'POST' ELSE 'REG' END
                 AND t.week = CASE
                       WHEN gs.season_type='POST' AND gs.season<=2020 THEN gs.week-1
                       ELSE gs.week END
                WHERE gs.season >= 2016
            """)
            print(f"  joined={rows[0]['n']:,}  epa_mismatch={rows[0]['epa_bad']:,}  "
                  f"yards_mismatch={rows[0]['py_bad']:,}")
            print("  (POST 2016-2020 in nflverse use weeks 18-21; legacy game_stats used 19-22 —")
            print("   the CASE above aligns them. A nonzero epa_mismatch here is a real discrepancy.)")
        except Exception as e:  # noqa: BLE001
            print(f"  ERR {e!r}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
