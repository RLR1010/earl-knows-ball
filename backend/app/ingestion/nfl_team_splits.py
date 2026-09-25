#!/usr/bin/env python3
"""Build nfl.team_splits — team performance by situation split.

Parity with nba.team_splits (home/away/vs-conference) plus NFL-relevant splits:
division, primetime, dome/outdoor, grass/turf, cold/mild/warm, windy/calm,
rest buckets, favourite/underdog.

Sources (all canonical, dev):
  - nfl.stats_team_week  (spine: one row per team-game; offence + defence stats)
  - nfl.schedules_norm   (view: schedules with franchise abbreviations normalised)
  - nfl.pbp              (success rate, explosive plays, 3rd down, red-zone)
  - nfl.nflverse_teams   (opponent conference)

Abbreviation note: nflverse `schedules` carries AS-OF-SEASON abbreviations
(OAK/SD/STL) while `stats_team_week` uses CURRENT ones (LV/LAC/LA). The
schedules_norm view maps the historical ones so joins don't drop early games.

Full-replace build. Career rows have season = NULL; per-season rows set season.
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

# franchise abbreviation aliases -> current nflverse abbreviation
ABBR_MAP = {"OAK": "LV", "SD": "LAC", "STL": "LA"}
_ABBR_CASE = lambda col: ("CASE btrim(" + col + ") "
                          + " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in ABBR_MAP.items())
                          + f" ELSE btrim({col}) END")

VIEW_SQL = f"""
CREATE OR REPLACE VIEW nfl.schedules_norm AS
SELECT s.*,
  {_ABBR_CASE('s.home_team')} AS home_n,
  {_ABBR_CASE('s.away_team')} AS away_n
FROM nfl.schedules s;
"""

FACT_SQL = r"""
CREATE TABLE nfl._tg (
  game_id text, season int, team text, is_home boolean,
  win int, loss int, tie int, pf int, pa int,
  pass_yds int, rush_yds int, total_yds int,
  plays int, pass_epa double precision, rush_epa double precision,
  turnovers int, takeaways int, sacks_allowed int, sacks_made int,
  success_att int, success int, explosive int,
  third_att int, third_conv int, rz_att int, rz_td int,
  team_spread double precision, total_line double precision,
  div_game int, roof_n text, surf_n text, temp int, wind int,
  weekday text, gametime text, rest int
);
INSERT INTO nfl._tg
SELECT st.game_id, st.season, st.team,
  (st.team = s.home_n) AS is_home,
  CASE WHEN (CASE WHEN st.team=s.home_n THEN s.home_score ELSE s.away_score END)
          > (CASE WHEN st.team=s.home_n THEN s.away_score ELSE s.home_score END) THEN 1 ELSE 0 END,
  CASE WHEN (CASE WHEN st.team=s.home_n THEN s.home_score ELSE s.away_score END)
          < (CASE WHEN st.team=s.home_n THEN s.away_score ELSE s.home_score END) THEN 1 ELSE 0 END,
  CASE WHEN s.home_score = s.away_score THEN 1 ELSE 0 END,
  CASE WHEN st.team=s.home_n THEN s.home_score ELSE s.away_score END,
  CASE WHEN st.team=s.home_n THEN s.away_score ELSE s.home_score END,
  st.passing_yards, st.rushing_yards,
  coalesce(st.passing_yards,0)+coalesce(st.rushing_yards,0),
  coalesce(st.attempts,0)+coalesce(st.carries,0),
  st.passing_epa, st.rushing_epa,
  coalesce(st.passing_interceptions,0)+coalesce(st.sack_fumbles_lost,0)
     +coalesce(st.rushing_fumbles_lost,0)+coalesce(st.receiving_fumbles_lost,0),
  coalesce(st.def_interceptions,0)+coalesce(st.fumble_recovery_opp,0),
  st.sacks_suffered, st.def_sacks,
  p.success_att, p.success, p.explosive, p.third_att, p.third_conv, p.rz_att, p.rz_td,
  CASE WHEN st.team=s.home_n THEN -s.spread_line ELSE s.spread_line END,
  s.total_line, s.div_game,
  CASE WHEN lower(btrim(coalesce(s.roof,''))) IN ('dome','closed') THEN 'dome'
       WHEN lower(btrim(coalesce(s.roof,''))) IN ('outdoors','open') THEN 'outdoor'
       ELSE 'unknown' END,
  CASE WHEN lower(btrim(coalesce(s.surface,''))) LIKE 'grass%' THEN 'grass'
       WHEN lower(btrim(coalesce(s.surface,''))) <> '' THEN 'turf'
       ELSE 'unknown' END,
  s.temp, s.wind, s.weekday, s.gametime,
  CASE WHEN st.team=s.home_n THEN s.home_rest ELSE s.away_rest END
FROM nfl.stats_team_week st
JOIN nfl.schedules_norm s ON s.game_id = st.game_id
LEFT JOIN (
  SELECT game_id, posteam AS team,
    count(*) FILTER (WHERE "pass"=1 OR "rush"=1) AS success_att,
    sum(CASE WHEN success=1 THEN 1 ELSE 0 END) AS success,
    sum(CASE WHEN ("pass"=1 AND yards_gained >= 20) OR ("rush"=1 AND yards_gained >= 10)
             THEN 1 ELSE 0 END) AS explosive,
    count(*) FILTER (WHERE down=3 AND ("pass"=1 OR "rush"=1)) AS third_att,
    sum(CASE WHEN down=3 AND ("pass"=1 OR "rush"=1) AND first_down=1 THEN 1 ELSE 0 END) AS third_conv,
    count(*) FILTER (WHERE yardline_100<=20 AND ("pass"=1 OR "rush"=1)) AS rz_att,
    sum(CASE WHEN yardline_100<=20 AND touchdown=1 THEN 1 ELSE 0 END) AS rz_td
  FROM nfl.pbp WHERE season_type='REG' GROUP BY 1,2
) p ON p.game_id = st.game_id AND p.team = st.team
WHERE st.season_type='REG' AND btrim(coalesce(st.team,'')) <> '';
"""

# split_type -> (label, extra predicate)
SPLITS = [
    ("all",          "Overall",            "TRUE"),
    ("home",         "Home",               "t.is_home"),
    ("away",         "Away",               "NOT t.is_home"),
    ("division",     "Division",           "t.div_game=1"),
    ("non_division", "Non-Division",       "t.div_game=0"),
    ("vs_afc",       "vs AFC",             "opp.team_conf='AFC'"),
    ("vs_nfc",       "vs NFC",             "opp.team_conf='NFC'"),
    ("primetime",    "Primetime",          "(t.weekday IN ('Monday','Thursday','Saturday') OR t.gametime >= '19:00')"),
    ("non_primetime","Non-Primetime",      "NOT (t.weekday IN ('Monday','Thursday','Saturday') OR t.gametime >= '19:00')"),
    ("dome",         "Dome/Closed",        "t.roof_n='dome'"),
    ("outdoor",      "Outdoor",            "t.roof_n='outdoor'"),
    ("grass",        "Grass",              "t.surf_n='grass'"),
    ("turf",         "Turf",               "t.surf_n='turf'"),
    ("cold",         "Cold (<=40F)",       "t.roof_n='outdoor' AND t.temp<=40"),
    ("mild",         "Mild (41-70F)",      "t.roof_n='outdoor' AND t.temp>40 AND t.temp<=70"),
    ("warm",         "Warm (>70F)",        "t.roof_n='outdoor' AND t.temp>70"),
    ("windy",        "Windy (>=12mph)",    "t.roof_n='outdoor' AND t.wind>=12"),
    ("calm",         "Calm (<12mph)",      "t.roof_n='outdoor' AND t.wind<12"),
    ("rest_short",   "Short Rest (<=6d)",  "t.rest<=6"),
    ("rest_normal",  "Normal Rest (7-9d)", "t.rest>=7 AND t.rest<=9"),
    ("rest_long",    "Long Rest (>=10d)",  "t.rest>=10"),
    ("favorite",     "Favourite",          "t.team_spread<0"),
    ("underdog",     "Underdog",           "t.team_spread>0"),
]

AGG = r"""
SELECT t.team, t.season, '{st}'::text, '{lb}'::text,
  count(*) AS games, sum(t.win) AS wins, sum(t.loss) AS losses, sum(t.tie) AS ties,
  round(sum(t.pf)::numeric/GREATEST(count(*),1),1),
  round(sum(t.pa)::numeric/GREATEST(count(*),1),1),
  round((sum(t.pf)-sum(t.pa))::numeric/GREATEST(count(*),1),1),
  sum(t.plays), sum(t.total_yds), sum(t.pass_yds), sum(t.rush_yds),
  round(sum(t.total_yds)::numeric/NULLIF(sum(t.plays),0),2),
  sum(t.pass_epa), sum(t.rush_epa),
  round((sum(t.pass_epa)+sum(t.rush_epa))::numeric/NULLIF(sum(t.plays),0),4),
  round(sum(t.success)::numeric/NULLIF(sum(t.success_att),0),4),
  round(sum(t.explosive)::numeric/GREATEST(count(*),1),2),
  sum(t.turnovers), sum(t.takeaways), sum(t.sacks_allowed), sum(t.sacks_made),
  round(sum(t.third_conv)::numeric/NULLIF(sum(t.third_att),0),4),
  round(sum(t.rz_td)::numeric/NULLIF(sum(t.rz_att),0),4),
  sum(CASE WHEN t.pf-t.pa+t.team_spread > 0 THEN 1 ELSE 0 END),
  sum(CASE WHEN t.pf-t.pa+t.team_spread < 0 THEN 1 ELSE 0 END),
  sum(CASE WHEN t.pf-t.pa+t.team_spread = 0 THEN 1 ELSE 0 END),
  sum(CASE WHEN t.pf+t.pa > t.total_line THEN 1 ELSE 0 END),
  sum(CASE WHEN t.pf+t.pa < t.total_line THEN 1 ELSE 0 END),
  sum(CASE WHEN t.pf+t.pa = t.total_line THEN 1 ELSE 0 END),
  now(), now()
FROM nfl._tg t
LEFT JOIN nfl.schedules_norm s2 ON s2.game_id = t.game_id
LEFT JOIN nfl.nflverse_teams opp ON opp.team_abbr =
     (CASE WHEN t.team = s2.home_n THEN s2.away_n ELSE s2.home_n END)
WHERE {pred}
GROUP BY GROUPING SETS ((t.team, t.season), (t.team))
"""

DDL = """
DROP TABLE IF EXISTS nfl.team_splits;
CREATE TABLE nfl.team_splits (
  id bigserial PRIMARY KEY,
  team text NOT NULL, season int, split_type text NOT NULL, split_label text,
  games int, wins int, losses int, ties int, win_pct numeric,
  points_for numeric, points_against numeric, point_diff numeric,
  plays bigint, total_yards bigint, pass_yards bigint, rush_yards bigint,
  yards_per_play numeric,
  pass_epa numeric, rush_epa numeric, epa_per_play numeric,
  success_rate numeric, explosive_per_game numeric,
  turnovers int, takeaways int, sacks_allowed int, sacks_made int,
  third_down_pct numeric, red_zone_td_pct numeric,
  ats_wins int, ats_losses int, ats_pushes int, ats_pct numeric,
  ou_overs int, ou_unders int, ou_pushes int, ou_over_pct numeric,
  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE INDEX ix_nfl_team_splits_team ON nfl.team_splits (team);
CREATE INDEX ix_nfl_team_splits_team_split ON nfl.team_splits (team, split_type);
"""


async def main():
    conn = await asyncpg.connect(_dsn())
    t0 = time.time()
    try:
        print("creating schedules_norm view...")
        await conn.execute(VIEW_SQL)
        print("building fact table...")
        await conn.execute("DROP TABLE IF EXISTS nfl._tg")
        await conn.execute(FACT_SQL)
        print(f"  fact rows: {await conn.fetchval('SELECT count(*) FROM nfl._tg'):,}")
        await conn.execute(DDL)
        for st, lb, pred in SPLITS:
            sql = f"""INSERT INTO nfl.team_splits
              (team, season, split_type, split_label, games, wins, losses, ties,
               points_for, points_against, point_diff, plays, total_yards, pass_yards,
               rush_yards, yards_per_play, pass_epa, rush_epa, epa_per_play, success_rate,
               explosive_per_game, turnovers, takeaways, sacks_allowed, sacks_made,
               third_down_pct, red_zone_td_pct, ats_wins, ats_losses, ats_pushes,
               ou_overs, ou_unders, ou_pushes, created_at, updated_at)
              {AGG.format(st=st, lb=lb, pred=pred)}"""
            await conn.execute(sql)
        await conn.execute("""
            UPDATE nfl.team_splits SET
              win_pct = round(wins::numeric/NULLIF(games,0),3),
              ats_pct = round(ats_wins::numeric/NULLIF(ats_wins+ats_losses,0),3),
              ou_over_pct = round(ou_overs::numeric/NULLIF(ou_overs+ou_unders,0),3)""")
        await conn.execute("DROP TABLE IF EXISTS nfl._tg")
        tot = await conn.fetchval("SELECT count(*) FROM nfl.team_splits")
        print(f"nfl.team_splits rows: {tot:,}  ({time.time()-t0:.1f}s)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
