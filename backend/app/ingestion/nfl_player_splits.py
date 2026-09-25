#!/usr/bin/env python3
"""Build nfl.player_splits_v2 — player performance by situation split.

Mirrors nba.player_splits (home/away/vs-conference) + the same situational
dimensions as nfl.team_splits, so player and team angles line up.

Sources (all canonical, dev):
  - nfl.stats_player_week  (spine: one row per player-game; 150 cols)
  - nfl.schedules_norm     (context: home/away, weather, rest, primetime, spread)
  - nfl.nflverse_teams     (opponent conference)

Writes a NEW table (leaves the legacy nfl.player_splits untouched).
Career rows have season = NULL; per-season rows set season.
Full-replace build.
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

FACT_SQL = r"""
CREATE TABLE nfl._pg (
  game_id text, season int, player_id text, player_name text, position text, team text,
  is_home boolean, opponent text, opp_conf text, opp_win_pct double precision,
  div_game int, roof_n text, surf_n text, temp int, wind int, weekday text, gametime text,
  rest int, team_spread double precision,
  completions int, attempts int, passing_yards int, passing_tds int, passing_interceptions int,
  passing_epa double precision, passing_cpoe double precision, passing_air_yards double precision,
  sacks_suffered int, sack_yards_lost int,
  carries int, rushing_yards int, rushing_tds int, rushing_epa double precision,
  rushing_fumbles_lost int,
  targets int, receptions int, receiving_yards int, receiving_tds int, receiving_epa double precision,
  receiving_air_yards double precision, receiving_yards_after_catch double precision,
  receiving_first_downs int, receiving_fumbles_lost int, target_share double precision,
  air_yards_share double precision, wopr double precision,
  def_sacks double precision, def_interceptions int, def_pass_defended int,
  def_tackles_solo int, def_tackle_assists int, def_tackles_for_loss int, def_qb_hits int,
  def_fumbles_forced int,
  fumbles_lost_total int, special_teams_tds int, fantasy_points double precision,
  fantasy_points_ppr double precision
);
INSERT INTO nfl._pg
SELECT p.game_id, p.season, p.player_id, p.player_display_name, p.position, p.team,
  (p.team = s.home_n),
  p.opponent_team,
  opp.team_conf, ts.win_pct, s.div_game,
  CASE WHEN lower(btrim(coalesce(s.roof,''))) IN ('dome','closed') THEN 'dome'
       WHEN lower(btrim(coalesce(s.roof,''))) IN ('outdoors','open') THEN 'outdoor'
       ELSE 'unknown' END,
  CASE WHEN lower(btrim(coalesce(s.surface,''))) LIKE 'grass%' THEN 'grass'
       WHEN lower(btrim(coalesce(s.surface,''))) <> '' THEN 'turf'
       ELSE 'unknown' END,
  s.temp, s.wind, s.weekday, s.gametime,
  CASE WHEN p.team=s.home_n THEN s.home_rest ELSE s.away_rest END,
  CASE WHEN p.team=s.home_n THEN -s.spread_line ELSE s.spread_line END,
  p.completions, p.attempts, p.passing_yards, p.passing_tds, p.passing_interceptions,
  p.passing_epa, p.passing_cpoe, p.passing_air_yards, p.sacks_suffered, p.sack_yards_lost,
  p.carries, p.rushing_yards, p.rushing_tds, p.rushing_epa, p.rushing_fumbles_lost,
  p.targets, p.receptions, p.receiving_yards, p.receiving_tds, p.receiving_epa,
  p.receiving_air_yards, p.receiving_yards_after_catch, p.receiving_first_downs,
  p.receiving_fumbles_lost, p.target_share, p.air_yards_share, p.wopr,
  p.def_sacks, p.def_interceptions, p.def_pass_defended, p.def_tackles_solo,
  p.def_tackle_assists, p.def_tackles_for_loss, p.def_qb_hits, p.def_fumbles_forced,
  p.fumbles_lost_total, p.special_teams_tds, p.fantasy_points, p.fantasy_points_ppr
FROM nfl.stats_player_week p
JOIN nfl.schedules_norm s ON s.game_id = p.game_id
LEFT JOIN nfl.nflverse_teams opp ON opp.team_abbr = p.opponent_team
LEFT JOIN nfl.team_splits ts
       ON ts.team = p.opponent_team AND ts.season = p.season AND ts.split_type='all'
WHERE p.season_type='REG' AND p.player_id IS NOT NULL;
"""

SPLITS = [
    ("all",          "Overall",            "TRUE"),
    ("home",         "Home",               "t.is_home"),
    ("away",         "Away",               "NOT t.is_home"),
    ("division",     "Division",           "t.div_game=1"),
    ("non_division", "Non-Division",       "t.div_game=0"),
    ("vs_afc",       "vs AFC",             "t.opp_conf='AFC'"),
    ("vs_nfc",       "vs NFC",             "t.opp_conf='NFC'"),
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
    # position-agnostic opponent-strength angles
    ("vs_winning",   "vs Winning Opp",     "t.opp_win_pct >= 0.5"),
    ("vs_losing",    "vs Losing Opp",      "t.opp_win_pct < 0.5"),
]

METRICS = [
    "completions", "attempts", "passing_yards", "passing_tds", "passing_interceptions",
    "passing_epa", "passing_cpoe", "passing_air_yards", "sacks_suffered", "sack_yards_lost",
    "carries", "rushing_yards", "rushing_tds", "rushing_epa", "rushing_fumbles_lost",
    "targets", "receptions", "receiving_yards", "receiving_tds", "receiving_epa",
    "receiving_air_yards", "receiving_yards_after_catch", "receiving_first_downs",
    "receiving_fumbles_lost",
    "def_sacks", "def_interceptions", "def_pass_defended", "def_tackles_solo",
    "def_tackle_assists", "def_tackles_for_loss", "def_qb_hits", "def_fumbles_forced",
    "fumbles_lost_total", "special_teams_tds", "fantasy_points", "fantasy_points_ppr",
]
_SUM = ",\n  ".join(f"sum(t.{m}) AS {m}" for m in METRICS)
_COLS = ", ".join(METRICS)

AGG = r"""
SELECT t.player_id,
  max(t.player_name) AS player_name,
  max(t.position) AS position,
  mode() WITHIN GROUP (ORDER BY t.team) AS team,
  t.season, '{st}'::text, '{lb}'::text,
  count(*) AS games,
  {sums}
FROM nfl._pg t
WHERE {pred}
GROUP BY GROUPING SETS ((t.player_id, t.season), (t.player_id))
"""

DDL = f"""
DROP TABLE IF EXISTS nfl.player_splits_v2;
CREATE TABLE nfl.player_splits_v2 (
  id bigserial PRIMARY KEY,
  player_id text, player_name text, position text, team text,
  season int, split_type text NOT NULL, split_label text,
  games int,
  {', '.join(m + (' double precision' if m.endswith('epa') or m in ('passing_cpoe','passing_air_yards','receiving_air_yards','receiving_yards_after_catch','def_sacks','fantasy_points','fantasy_points_ppr') or m.startswith('rushing_epa') or m.startswith('receiving_epa') else ' int') for m in METRICS)},
  created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE INDEX ix_psv2_player ON nfl.player_splits_v2 (player_id);
CREATE INDEX ix_psv2_player_split ON nfl.player_splits_v2 (player_id, split_type);
CREATE INDEX ix_psv2_season ON nfl.player_splits_v2 (season);
"""


async def main():
    conn = await asyncpg.connect(_dsn())
    t0 = time.time()
    try:
        print("building player-game fact...")
        await conn.execute("DROP TABLE IF EXISTS nfl._pg")
        await conn.execute(FACT_SQL)
        print(f"  fact rows: {await conn.fetchval('SELECT count(*) FROM nfl._pg'):,}")
        await conn.execute(DDL)
        assert list(SPLITS)[0] is not None
        for st, lb, pred in SPLITS:
            sql = f"INSERT INTO nfl.player_splits_v2 (player_id, player_name, position, team, season, split_type, split_label, games, {_COLS}) {AGG.format(st=st, lb=lb, pred=pred, sums=_SUM)}"
            await conn.execute(sql)
        await conn.execute("DROP TABLE IF EXISTS nfl._pg")
        tot = await conn.fetchval("SELECT count(*) FROM nfl.player_splits_v2")
        print(f"nfl.player_splits_v2 rows: {tot:,}  ({time.time()-t0:.1f}s)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
