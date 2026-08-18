"""NBA team-season stats audit: build our side of the comparison.

Aggregates per-team-per-season stats from nba.games (REG, FINAL) for the
trainable years, so they can be diffed against basketball-reference season
summary tables from /leagues/NBA_<season-end>.html.

Usage:
  python app/scripts/nba_season_stats_audit.py            # full range
  python app/scripts/nba_season_stats_audit.py 2019 2020  # specific years
"""
import csv
import sys

import psycopg2

from app.db_urls import PSYCOPG2_DATABASE_URL


COLUMNS = [
    "year", "team", "games", "wins", "losses",
    "ppg", "oppg", "fg_pct", "fg3_pg", "fg3a_pg", "ft_pg", "reb_pg", "ast_pg",
]

Q = """\
WITH t AS (
    SELECT s.year AS yr, g.home_team_id AS tid,
           g.home_score AS sc, g.away_score AS opp,
           g.home_field_goals_made AS fgm, g.home_field_goals_attempted AS fga,
           g.home_three_points_made AS tpm, g.home_three_points_attempted AS tpa,
           g.home_free_throws_made AS ftm, g.home_rebounds AS reb,
           g.home_assists AS ast
    FROM nba.games g JOIN nba.seasons s ON s.id = g.season_id
    WHERE g.game_type = 'REG' AND g.status = 'FINAL'
    UNION ALL
    SELECT s.year, g.away_team_id, g.away_score, g.home_score,
           g.away_field_goals_made, g.away_field_goals_attempted,
           g.away_three_points_made, g.away_three_points_attempted,
           g.away_free_throws_made, g.away_rebounds, g.away_assists
    FROM nba.games g JOIN nba.seasons s ON s.id = g.season_id
    WHERE g.game_type = 'REG' AND g.status = 'FINAL'
)
SELECT t.yr AS year, te.abbreviation AS team, count(*) AS games,
       sum(CASE WHEN t.sc > t.opp THEN 1 ELSE 0 END) AS wins,
       sum(CASE WHEN t.sc < t.opp THEN 1 ELSE 0 END) AS losses,
       round(sum(t.sc)::numeric / count(*), 1) AS ppg,
       round(sum(t.opp)::numeric / count(*), 1) AS oppg,
       round(100.0 * sum(t.fgm) / NULLIF(sum(t.fga), 0), 1) AS fg_pct,
       round(sum(t.tpm)::numeric / count(*), 1) AS fg3_pg,
       round(sum(t.tpa)::numeric / count(*), 1) AS fg3a_pg,
       round(sum(t.ftm)::numeric / count(*), 1) AS ft_pg,
       round(sum(t.reb)::numeric / count(*), 1) AS reb_pg,
       round(sum(t.ast)::numeric / count(*), 1) AS ast_pg
FROM t
JOIN nba.teams te ON te.id = t.tid
WHERE t.yr BETWEEN %(lo)s AND %(hi)s
GROUP BY t.yr, te.abbreviation, te.id
ORDER BY t.yr, te.abbreviation
"""


def main():
    lo, hi = 2006, 2025
    if len(sys.argv) >= 2:
        lo = int(sys.argv[1])
    if len(sys.argv) >= 3:
        hi = int(sys.argv[2])
    conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
    cur = conn.cursor()
    cur.execute(Q, {"lo": lo, "hi": hi})
    w = csv.writer(sys.stdout)
    w.writerow(COLUMNS)
    for r in cur.fetchall():
        w.writerow(list(r))
    conn.close()


if __name__ == "__main__":
    main()
