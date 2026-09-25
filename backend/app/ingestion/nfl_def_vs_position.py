#!/usr/bin/env python3
"""Build nfl.def_vs_position — fantasy/stat production ALLOWED by a defence,
broken down by the offensive position group that produced it.

Classic "defence vs position" (DvP): for each defensive team we sum what
opposing players of each position group (QB/RB/WR/TE) did against them.

Sources (canonical, dev):
  - nfl.stats_player_week  (offensive production, per player-game)
  - nfl.schedules_norm     (home/away not needed here; defensive team = opponent)

Rows: defence_team × position_group × {season, career}. Full replace.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import asyncpg

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
from app.ingestion.nflverse_hub import _dsn  # noqa: E402

POS_GROUP = ("CASE WHEN p.position='QB' THEN 'QB' "
             "WHEN p.position IN ('RB','FB','HB') THEN 'RB' "
             "WHEN p.position='WR' THEN 'WR' "
             "WHEN p.position='TE' THEN 'TE' ELSE 'OTHER' END")

METRICS = [
    "passing_yards", "passing_tds", "passing_interceptions", "passing_epa", "passing_cpoe",
    "carries", "rushing_yards", "rushing_tds", "rushing_epa", "rushing_fumbles_lost",
    "targets", "receptions", "receiving_yards", "receiving_tds", "receiving_epa",
    "receiving_air_yards", "receiving_yards_after_catch", "receiving_first_downs",
    "receiving_fumbles_lost", "fumbles_lost_total", "fantasy_points", "fantasy_points_ppr",
]

FACT_SQL = f"""
DROP TABLE IF EXISTS nfl._dvp;
CREATE TABLE nfl._dvp AS
SELECT
  p.opponent_team AS defense_team,
  {POS_GROUP} AS position_group,
  p.season, p.game_id,
  coalesce(p.passing_yards,0) AS passing_yards,
  coalesce(p.passing_tds,0) AS passing_tds,
  coalesce(p.passing_interceptions,0) AS passing_interceptions,
  p.passing_epa, p.passing_cpoe,
  coalesce(p.carries,0) AS carries,
  coalesce(p.rushing_yards,0) AS rushing_yards,
  coalesce(p.rushing_tds,0) AS rushing_tds,
  p.rushing_epa,
  coalesce(p.rushing_fumbles_lost,0) AS rushing_fumbles_lost,
  coalesce(p.targets,0) AS targets,
  coalesce(p.receptions,0) AS receptions,
  coalesce(p.receiving_yards,0) AS receiving_yards,
  coalesce(p.receiving_tds,0) AS receiving_tds,
  p.receiving_epa, p.receiving_air_yards, p.receiving_yards_after_catch,
  coalesce(p.receiving_first_downs,0) AS receiving_first_downs,
  coalesce(p.receiving_fumbles_lost,0) AS receiving_fumbles_lost,
  coalesce(p.fumbles_lost_total,0) AS fumbles_lost_total,
  coalesce(p.fantasy_points,0) AS fantasy_points,
  coalesce(p.fantasy_points_ppr,0) AS fantasy_points_ppr
FROM nfl.stats_player_week p
WHERE p.season_type='REG' AND p.opponent_team IS NOT NULL
  AND {POS_GROUP} <> 'OTHER';
"""

_SUM = ",\n  ".join(f"sum(t.{m}) AS {m}" for m in METRICS)
_COLS = ", ".join(METRICS)

AGG = f"""
SELECT t.defense_team, t.position_group, t.season,
  {_SUM},
  count(DISTINCT t.game_id) AS games
FROM nfl._dvp t
GROUP BY GROUPING SETS ((t.defense_team, t.position_group, t.season),
                        (t.defense_team, t.position_group))
"""

DDL = f"""
DROP TABLE IF EXISTS nfl.def_vs_position;
CREATE TABLE nfl.def_vs_position (
  id bigserial PRIMARY KEY,
  defense_team text NOT NULL, position_group text NOT NULL, season int,
  {', '.join(m + ' double precision' for m in METRICS)},
  games int,
  pass_yds_pg numeric, rush_yds_pg numeric, rec_yds_pg numeric,
  fp_pg numeric, fp_ppg numeric,
  fp_rank int, fp_rank_ppr int,
  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE INDEX ix_dvp_def_pos ON nfl.def_vs_position (defense_team, position_group);
CREATE INDEX ix_dvp_season ON nfl.def_vs_position (season);
"""


async def main():
    conn = await asyncpg.connect(_dsn())
    t0 = time.time()
    try:
        print("building dvp fact...")
        await conn.execute(FACT_SQL)
        print(f"  fact rows: {await conn.fetchval('SELECT count(*) FROM nfl._dvp'):,}")
        await conn.execute(DDL)
        await conn.execute(
            f"INSERT INTO nfl.def_vs_position (defense_team, position_group, season, {_COLS}, games) {AGG}")
        await conn.execute("""
            UPDATE nfl.def_vs_position SET
              pass_yds_pg = round(passing_yards::numeric/NULLIF(games,0),1),
              rush_yds_pg = round(rushing_yards::numeric/NULLIF(games,0),1),
              rec_yds_pg  = round(receiving_yards::numeric/NULLIF(games,0),1),
              fp_pg       = round(fantasy_points::numeric/NULLIF(games,0),2),
              fp_ppg      = round(fantasy_points_ppr::numeric/NULLIF(games,0),2)""")
        await conn.execute("DROP TABLE IF EXISTS nfl._dvp")
        # per-season ranks: 1 = most generous defence for that position group
        await conn.execute("""
            UPDATE nfl.def_vs_position d SET fp_rank = r.rk, fp_rank_ppr = r.rkp
            FROM (
              SELECT id,
                rank() OVER (PARTITION BY season, position_group ORDER BY fp_pg DESC NULLS LAST) AS rk,
                rank() OVER (PARTITION BY season, position_group ORDER BY fp_ppg DESC NULLS LAST) AS rkp
              FROM nfl.def_vs_position WHERE season IS NOT NULL
            ) r WHERE r.id = d.id""")
        tot = await conn.fetchval("SELECT count(*) FROM nfl.def_vs_position")
        print(f"nfl.def_vs_position rows: {tot:,}  ({time.time()-t0:.1f}s)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
