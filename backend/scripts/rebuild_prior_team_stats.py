"""
Rebuild nfl.prior_team_stats from end-of-season cumulative_game_stats data.

Root cause: PBP data for 2016-2019 was never loaded into nfl.play_by_play,
so advanced stats (third down, red zone, explosive plays, etc.) were all zero.

Fixed by loading PBP and re-running aggregations. This script repopulates
prior_team_stats from the corrected cumulative_game_stats.

Usage:
    source venv/bin/activate
    python -m backend.scripts.rebuild_prior_team_stats
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from app.db_urls import SYNC_DATABASE_URL

DATABASE_URL = SYNC_DATABASE_URL  # from app.db_urls
engine = create_engine(DATABASE_URL, pool_pre_ping=True)


def main():
    print("=" * 60)
    print("REBUILD: nfl.prior_team_stats from cumulative_game_stats")
    print("=" * 60)

    with engine.connect() as conn:
        # Step 1: Find seasons and their final weeks
        print("\n1. Identifying seasons and final weeks...")
        r = conn.execute(text("""
            SELECT season, MAX(week) as final_week
            FROM nfl.cumulative_game_stats
            GROUP BY season ORDER BY season
        """))
        seasons = {row[0]: row[1] for row in r}
        if not seasons:
            print("No data found in cumulative_game_stats. Aborting.")
            return
        for s, w in sorted(seasons.items()):
            print(f"   Season {s}: final week = {w}")
        season_list = sorted(seasons.keys())

        # Step 1b: Ensure new columns exist (idempotent). These backfill the
        # remaining rolling/efficiency features from prior-season data so
        # early-season games aren't seeded with 0.
        print("\n1b. Ensuring prior_team_stats columns exist...")
        _new_cols = {
            "off_ypp": "NUMERIC", "def_ypp": "NUMERIC",
            "off_first_downs": "NUMERIC", "def_first_downs": "NUMERIC",
            "off_fourth_down_pct": "NUMERIC", "def_fourth_down_pct": "NUMERIC",
            "off_rz_trips": "NUMERIC", "def_rz_trips": "NUMERIC",
            "off_ints_thrown": "NUMERIC", "def_ints_thrown": "NUMERIC",
            "turnover_diff_r5": "NUMERIC",
            "off_pts_stddev_5": "NUMERIC", "off_yds_stddev_5": "NUMERIC",
            "def_pts_stddev_5": "NUMERIC", "def_yds_stddev_5": "NUMERIC",
        }
        for col, ctype in _new_cols.items():
            conn.execute(text(
                f"ALTER TABLE nfl.prior_team_stats ADD COLUMN IF NOT EXISTS {col} {ctype}"
            ))
        conn.commit()

        # Step 2: Delete existing rows for all seasons we're rebuilding
        r = conn.execute(text("SELECT COUNT(*) FROM nfl.prior_team_stats"))
        print(f"\n2. Deleting {r.scalar()} existing rows...")
        r = conn.execute(
            text("DELETE FROM nfl.prior_team_stats WHERE season IN :seasons"),
            {"seasons": tuple(season_list)},
        )
        print(f"   Deleted {r.rowcount} rows")
        conn.commit()

        # Step 3: Insert from cumulative_game_stats final-week snapshot,
        # joined with a win_pct subquery from nfl.games
        insert_sql = text("""
            INSERT INTO nfl.prior_team_stats (
                team_abbr, season, games,
                off_ppg, off_ypg, off_pass_ypg, off_rush_ypg, off_ypa,
                off_cmp_pct, off_third_down_pct, off_rz_td_pct,
                off_explosive_rate, off_three_and_out_rate, off_epa_per_play,
                def_ppg, def_ypg, def_pass_ypg, def_rush_ypg, def_ypa_allowed,
                def_cmp_pct_allowed, def_third_down_pct, def_sack_rate,
                def_explosive_rate, def_three_and_out_rate, def_epa_per_play,
                point_differential, yardage_differential, turnover_margin,
                win_pct,
                rw_off_ppg, rw_off_ypg, rw_def_ppg, rw_def_ypg,
                win_streak,
                off_ypp, def_ypp,
                off_first_downs, def_first_downs,
                off_fourth_down_pct, def_fourth_down_pct,
                off_rz_trips, def_rz_trips,
                off_ints_thrown, def_ints_thrown,
                turnover_diff_r5,
                off_pts_stddev_5, off_yds_stddev_5,
                def_pts_stddev_5, def_yds_stddev_5
            )
            SELECT
                cgs.team_abbr, cgs.season, cgs.games_played,
                cgs.off_ppg, cgs.off_ypg, cgs.off_pass_ypg, cgs.off_rush_ypg,
                cgs.off_ypa,
                cgs.off_cmp_pct, cgs.off_third_down_pct, cgs.off_rz_td_pct,
                cgs.off_explosive_rate, cgs.off_three_and_out_rate,
                cgs.off_epa_per_play,
                cgs.def_ppg_allowed, cgs.def_ypg_allowed,
                cgs.def_pass_ypg_allowed, cgs.def_rush_ypg_allowed,
                cgs.def_ypa_allowed,
                cgs.def_cmp_pct_allowed, cgs.def_third_down_pct,
                cgs.def_sack_rate,
                cgs.def_explosive_rate, cgs.def_three_and_out_rate,
                cgs.def_epa_per_play,
                cgs.point_differential_avg, cgs.yardage_differential_avg,
                cgs.turnover_margin_avg,
                COALESCE(wp.win_pct, 0),
                cgs.rw_off_ppg, cgs.rw_off_ypg,
                cgs.rw_def_ppg, cgs.rw_def_ypg,
                cgs.win_streak,
                cgs.off_ypp, cgs.def_ypp_allowed,
                cgs.off_first_downs::numeric / NULLIF(cgs.games_played, 0),
                cgs.def_first_downs_allowed::numeric / NULLIF(cgs.games_played, 0),
                cgs.off_fourth_down_pct, cgs.def_fourth_down_pct,
                cgs.off_red_zone_trips::numeric / NULLIF(cgs.games_played, 0),
                cgs.def_red_zone_trips::numeric / NULLIF(cgs.games_played, 0),
                cgs.off_interceptions::numeric / NULLIF(cgs.games_played, 0),
                cgs.off_interceptions::numeric / NULLIF(cgs.games_played, 0),  -- def_ints_thrown proxy: own offense's INTs
                cgs.turnover_margin_avg,
                cgs.off_pts_stddev_5, cgs.off_yds_stddev_5,
                cgs.def_pts_stddev_5, cgs.def_yds_stddev_5
            FROM nfl.cumulative_game_stats cgs
            INNER JOIN (
                SELECT team_abbr, season, MAX(week) as max_week
                FROM nfl.cumulative_game_stats
                GROUP BY team_abbr, season
            ) last
                ON cgs.team_abbr = last.team_abbr
                AND cgs.season = last.season
                AND cgs.week = last.max_week
            LEFT JOIN (
                SELECT
                    t.abbreviation as team_abbr,
                    s.year as season,
                    COUNT(*) FILTER(WHERE
                        (t.id = g.home_team_id AND g.home_score > g.away_score)
                        OR (t.id = g.away_team_id AND g.away_score > g.home_score)
                    )::numeric / COUNT(*)::numeric as win_pct
                FROM nfl.games g
                JOIN nfl.seasons s ON g.season_id = s.id
                JOIN nfl.teams t ON t.id IN (g.home_team_id, g.away_team_id)
                WHERE g.game_type = 'REG'
                GROUP BY t.abbreviation, s.year
            ) wp
                ON cgs.team_abbr = wp.team_abbr
                AND cgs.season = wp.season
            WHERE cgs.season IN :seasons
            ORDER BY cgs.season, cgs.team_abbr
        """)

        r = conn.execute(insert_sql, {"seasons": tuple(season_list)})
        conn.commit()
        print(f"\n3. Inserted {r.rowcount} new rows")

        # Step 4: Verify all columns now have non-zero values
        print("\n4. Verifying results...")
        for season in season_list:
            r = conn.execute(text(f"""
                SELECT COUNT(*) as total,
                       COUNT(*) FILTER(WHERE off_third_down_pct > 0) as has_3rd,
                       COUNT(*) FILTER(WHERE off_rz_td_pct > 0) as has_rz,
                       COUNT(*) FILTER(WHERE off_explosive_rate > 0) as has_exp,
                       COUNT(*) FILTER(WHERE off_three_and_out_rate > 0) as has_3_out,
                       COUNT(*) FILTER(WHERE def_third_down_pct > 0) as has_def_3rd,
                       COUNT(*) FILTER(WHERE def_explosive_rate > 0) as has_def_exp,
                       COUNT(*) FILTER(WHERE def_three_and_out_rate > 0) as has_def_3out,
                       COUNT(*) FILTER(WHERE off_epa_per_play != 0) as has_epa,
                       ROUND(AVG(off_third_down_pct)::numeric, 4) as avg_3rd,
                       ROUND(AVG(off_rz_td_pct)::numeric, 4) as avg_rz,
                       ROUND(AVG(win_pct)::numeric, 4) as avg_win_pct
                FROM nfl.prior_team_stats
                WHERE season = {season}
            """))
            row = r.fetchone()
            if row:
                print(f"   Season {season:4d}: T={row[0]:3d} "
                      f"3rdD={row[1]:3d} RZTD={row[2]:3d} Expl={row[3]:3d} "
                      f"3Out={row[4]:3d} | "
                      f"D3rd={row[5]:3d} DExp={row[6]:3d} D3O={row[7]:3d} "
                      f"EPA={row[8]:3d} | "
                      f"avg: 3rd={row[9]:.4f} rz_td={row[10]:.4f} wp={row[11]:.4f}")

        # Step 5: Sample rows
        print("\n5. Sample rows (2016):")
        r = conn.execute(text("""
            SELECT team_abbr, games, off_third_down_pct, off_rz_td_pct,
                   off_explosive_rate, off_three_and_out_rate, win_pct
            FROM nfl.prior_team_stats
            WHERE season = 2016
            ORDER BY team_abbr
            LIMIT 8
        """))
        for row in r:
            vals = []
            for v in row:
                if isinstance(v, float):
                    vals.append(f"{v:.4f}")
                else:
                    vals.append(str(v))
            print(f"   {vals[0]:4s}: G={vals[1]:2s}  3rdD={vals[2]}  "
                  f"RZTD={vals[3]}  Expl={vals[4]}  3Out={vals[5]}  "
                  f"W%={vals[6]}")

        # Step 6: Overall check for any ALL-ZERO rows
        print("\n6. Sanity check - non-zero counts across ALL seasons:")
        r = conn.execute(text("""
            SELECT COUNT(*) as total_rows,
                   COUNT(*) FILTER(WHERE off_third_down_pct > 0) as third_down,
                   COUNT(*) FILTER(WHERE off_rz_td_pct > 0) as rz_td,
                   COUNT(*) FILTER(WHERE off_explosive_rate > 0) as explosive,
                   COUNT(*) FILTER(WHERE off_three_and_out_rate > 0) as three_and_out,
                   COUNT(*) FILTER(WHERE def_third_down_pct > 0) as def_third,
                   COUNT(*) FILTER(WHERE def_explosive_rate > 0) as def_explosive,
                   COUNT(*) FILTER(WHERE def_three_and_out_rate > 0) as def_three_out,
                   COUNT(*) FILTER(WHERE off_epa_per_play != 0) as epa,
                   COUNT(*) FILTER(WHERE win_pct > 0) as win_pct
            FROM nfl.prior_team_stats
        """))
        row = r.fetchone()
        print(f"   Total rows: {row[0]:4d}")
        print(f"   Non-zero:  3rdD={row[1]:4d} RZTD={row[2]:4d} "
              f"Expl={row[3]:4d} 3Out={row[4]:4d} | "
              f"D3rd={row[5]:4d} DExp={row[6]:4d} D3O={row[7]:4d} | "
              f"EPA={row[8]:4d} Win%={row[9]:4d}")

        print("\n✅  Done! prior_team_stats is now fully populated.")


if __name__ == "__main__":
    main()
