"""
Rebuild nba.player_season_stats from nba.player_game_stats (ground truth).

WHY: the old player_season_stats was built by scraping Basketball-Reference season
totals, which systematically produced truncated/phantom rows (Jeff Green 2019 ->
games_played=10 instead of 48; 865 rows with games<10 in 2016+; ~49% games_played
mismatches). player_game_stats is the complete, verified per-game source for 2016+
(100% of FINAL games boxed; player points sum to game totals 100%).

THIS: derive season totals per (player_id, season_id, team_id) from FINAL REG games.
Derives FG%/3P%/FT%/TS% from SUMMED counts (never AVG of per-game rates — accuracy rule).
minutes is varchar mixing bare-integer and "MM:SS" -> parsed to decimal minutes.
plus_minus summed directly (numeric).

USAGE:
  cd backend
  set -a && . ./.env && set +a
  PYTHONPATH=$PWD ../venv/bin/python app/scripts/rebuild_player_season_stats.py   # dry-run
  PYTHONPATH=$PWD ../venv/bin/python app/scripts/rebuild_player_season_stats.py --apply

Backup (already done 2026-08-28): nba.player_season_stats_backup_20260828
"""
import argparse
import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.database import admin_async_session

logger = logging.getLogger("rebuild-player-season-stats")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

AGG_SQL = """
SELECT pgs.player_id,
       g.season_id            AS season_id,
       NULL::int              AS team_id,
       COUNT(*) FILTER (WHERE parsed_min.minutes_nbr > 0)     AS games_played,
       COUNT(*) FILTER (WHERE pgs.is_starter AND parsed_min.minutes_nbr > 0) AS games_started,
       SUM(parsed_min.minutes_nbr)::numeric                   AS minutes_played,
       SUM(pgs.points)                           AS points,
       SUM(pgs.field_goals_made)                 AS fgm,
       SUM(pgs.field_goals_attempted)            AS fga,
       SUM(pgs.three_pointers_made)              AS tpm,
       SUM(pgs.three_pointers_attempted)         AS tpa,
       SUM(pgs.free_throws_made)                 AS ftm,
       SUM(pgs.free_throws_attempted)            AS fta,
       SUM(pgs.rebounds_total)                   AS rebounds,
       SUM(pgs.rebounds_offensive)               AS oreb,
       SUM(pgs.rebounds_defensive)               AS dreb,
       SUM(pgs.assists)                          AS assists,
       SUM(pgs.turnovers)                        AS turnovers,
       SUM(pgs.steals)                           AS steals,
       SUM(pgs.blocks)                           AS blocks,
       SUM(pgs.fouls_personal)                   AS pf,
       SUM(pgs.plus_minus)                       AS plus_minus
FROM nba.player_game_stats pgs
JOIN nba.games g ON g.id = pgs.game_id
JOIN nba.seasons s ON s.id = g.season_id
CROSS JOIN LATERAL (
    -- minutes is varchar mixing bare-int minutes and 'MM:SS'; '-'/''/non-numeric -> 0
    SELECT CASE
        WHEN pgs.minutes ~ '^[0-9]+:[0-5][0-9]$'
            THEN (SPLIT_PART(pgs.minutes,':',1)::int) + (SPLIT_PART(pgs.minutes,':',2)::int)/60.0
        WHEN pgs.minutes ~ '^[0-9]+(\\.[0-9]+)?$'
            THEN COALESCE(pgs.minutes,'0')::numeric
        ELSE 0.0 END AS minutes_nbr
) parsed_min
WHERE g.status = 'FINAL'
  AND g.game_type = 'REG'
  AND s.year BETWEEN 2016 AND 2025
GROUP BY pgs.player_id, g.season_id
ORDER BY g.season_id, pgs.player_id
"""


def derived(row):
    """Compute derived percentages/ratios from summed counts."""
    r = dict(row)
    # NULL-safe: any summed column may be NULL if a player only has rows with NULLs
    for k in ("points","fgm","fga","tpm","tpa","ftm","fta","rebounds",
              "oreb","dreb","assists","turnovers","steals","blocks","pf",
              "plus_minus","minutes_played"):
        if r.get(k) is None:
            r[k] = 0
    fga = r["fga"] or 0
    tpa = r["tpa"] or 0
    fta = r["fta"] or 0
    gp = r["games_played"] or 0
    r["field_goal_pct"] = (r["fgm"] / fga) if fga else None
    r["three_point_pct"] = (r["tpm"] / tpa) if tpa else None
    r["free_throw_pct"] = (r["ftm"] / fta) if fta else None
    r["points_per_game"] = (r["points"] / gp) if gp else None
    r["rebounds_per_game"] = (r["rebounds"] / gp) if gp else None
    r["assists_per_game"] = (r["assists"] / gp) if gp else None
    r["assists_turnover_ratio"] = (r["assists"] / r["turnovers"]) if r["turnovers"] else None
    ts_num = 2 * (r["points"] or 0)
    ts_den = 2 * (fga + 0.44 * fta)
    r["true_shooting_pct"] = (ts_num / ts_den) if ts_den else None
    minutes = r["minutes_played"] or 0
    # usage_pct (AST%+FGA+0.44FTA approximated / team possessions) — estimate using
    # player usage of league-standard 5-man team possession denominator approx.
    r["usage_pct"] = None  # not derivable precisely without team possessions; leave null
    # efficiency = PTS + REB + AST + STL + BLK - (FGA - FGM) - (FTA - FTM) - TOV
    r["efficiency"] = max(0.0, (
        (r["points"] or 0) + (r["rebounds"] or 0) + (r["assists"] or 0)
        + (r["steals"] or 0) + (r["blocks"] or 0)
        - ((fga - (r["fgm"] or 0))) - ((fta - (r["ftm"] or 0)))
        - (r["turnovers"] or 0)
    ))
    return r


async def build(dry: bool) -> int:
    async with admin_async_session() as db:
        rows = (await db.execute(text(AGG_SQL))).all()
        logger.info(f"aggregated {len(rows)} (player, season, team) rows from player_game_stats")
        if dry:
            return len(rows)
        # delete existing 2016+ rows (targeted) then insert derived
        await db.execute(text(
            "DELETE FROM nba.player_season_stats ps USING nba.seasons s "
            "WHERE ps.season_id = s.id AND s.year BETWEEN 2016 AND 2025"
        ))
        inserted = 0
        for row in rows:
            r = derived(row._mapping)
            await db.execute(text("""
                INSERT INTO nba.player_season_stats (
                  player_id, season_id, team_id, games_played, games_started,
                  minutes_played, points, points_per_game, field_goals_made,
                  field_goals_attempted, field_goal_pct, three_points_made,
                  three_points_attempted, three_point_pct, free_throws_made,
                  free_throws_attempted, free_throw_pct, rebounds, offensive_rebounds,
                  defensive_rebounds, rebounds_per_game, assists, assists_per_game,
                  turnovers, assists_turnover_ratio, steals, blocks, personal_fouls,
                  plus_minus, efficiency, true_shooting_pct, usage_pct
                ) VALUES (
                  :player_id, :season_id, :team_id, :games_played, :games_started,
                  :minutes_played, :points, :points_per_game, :fgm, :fga, :field_goal_pct,
                  :tpm, :tpa, :three_point_pct, :ftm, :fta, :free_throw_pct, :rebounds,
                  :oreb, :dreb, :rebounds_per_game, :assists, :assists_per_game, :turnovers,
                  :assists_turnover_ratio, :steals, :blocks, :pf, :plus_minus, :efficiency,
                  :true_shooting_pct, :usage_pct
                )
            """), r)
            inserted += 1
            if inserted % 500 == 0:
                await db.commit()
        await db.commit()
        return inserted


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="actually rebuild (default dry-run)")
    args = p.parse_args()
    n = await build(not args.apply)
    if args.apply:
        logger.info(f"REBUILT player_season_stats from player_game_stats: {n} rows (2016+)")
    else:
        logger.info(f"DRY-RUN: would rebuild {n} rows (2016+). Re-run with --apply")


if __name__ == "__main__":
    asyncio.run(main())
