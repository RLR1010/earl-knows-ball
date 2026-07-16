"""
Compute nba.player_season_stats from nba.player_game_stats for a given season.

Usage:
    python -m scripts.compute_nba_player_season_stats 35   # season_id 35 (2025)
    python -m scripts.compute_nba_player_season_stats 36   # season_id 36 (2026)

For players who played on multiple teams in the same season, only the
most-recent team (by game date) is kept to satisfy the unique constraint
on (player_id, season_id).
"""

import sys
import logging
from collections import OrderedDict

import psycopg2
from psycopg2.extras import RealDictCursor

DB_URL = "postgresql://earl:earl2025@localhost:5432/earl_knows_football"

logger = logging.getLogger("compute-nba-pss")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

INSERT_COLS = [
    "player_id", "season_id", "team_id",
    "games_played", "games_started", "minutes_played",
    "points", "points_per_game",
    "field_goals_made", "field_goals_attempted", "field_goal_pct",
    "three_points_made", "three_points_attempted", "three_point_pct",
    "free_throws_made", "free_throws_attempted", "free_throw_pct",
    "rebounds", "offensive_rebounds", "defensive_rebounds", "rebounds_per_game",
    "assists", "assists_per_game", "turnovers", "assists_turnover_ratio",
    "steals", "blocks", "personal_fouls", "plus_minus",
    "efficiency", "true_shooting_pct", "usage_pct", "fantasy_points",
]

PARSE_MINUTES = """
    CASE
        WHEN pgs.minutes ~ '^[0-9]+:[0-9]+$'
        THEN (SPLIT_PART(pgs.minutes, ':', 1)::numeric * 60 + SPLIT_PART(pgs.minutes, ':', 2)::numeric) / 60.0
        WHEN pgs.minutes ~ '^[0-9]+$' THEN pgs.minutes::numeric
        ELSE 0
    END
"""

RAW_AGG_SQL = """
SELECT
    pgs.player_id,
    pgs.team_id,
    g.season_id,
    MAX(g.date) as last_team_game,
    COUNT(*)                                                                   AS games_played,
    SUM(CASE WHEN pgs.is_starter THEN 1 ELSE 0 END)                           AS games_started,
    SUM({parse_minutes})                                                       AS minutes_played,
    SUM(pgs.points)                                                            AS points,
    SUM(pgs.field_goals_made)                                                  AS field_goals_made,
    SUM(pgs.field_goals_attempted)                                             AS field_goals_attempted,
    SUM(pgs.three_pointers_made)                                               AS three_points_made,
    SUM(pgs.three_pointers_attempted)                                          AS three_points_attempted,
    SUM(pgs.free_throws_made)                                                  AS free_throws_made,
    SUM(pgs.free_throws_attempted)                                             AS free_throws_attempted,
    SUM(pgs.rebounds_total)                                                    AS rebounds,
    SUM(pgs.rebounds_offensive)                                                AS offensive_rebounds,
    SUM(pgs.rebounds_defensive)                                                AS defensive_rebounds,
    SUM(pgs.assists)                                                           AS assists,
    SUM(pgs.turnovers)                                                         AS turnovers,
    SUM(pgs.steals)                                                            AS steals,
    SUM(pgs.blocks)                                                            AS blocks,
    SUM(pgs.fouls_personal)                                                    AS personal_fouls,
    SUM(pgs.plus_minus)                                                        AS plus_minus
FROM nba.player_game_stats pgs
JOIN nba.games g ON pgs.game_id = g.id
WHERE g.season_id = %s
GROUP BY pgs.player_id, pgs.team_id, g.season_id
ORDER BY pgs.player_id, last_team_game DESC
""".format(parse_minutes=PARSE_MINUTES)


def _compute_result_row(player_id, team_id, season_id, last_team_game,
                        games_played, games_started, minutes_played,
                        points, fgm, fga, tpm, tpa, ftm, fta,
                        reb, oreb, dreb, ast, tov, stl, blk, pf, pm):
    """Build a dict of player_season_stats values for one row."""
    points = points or 0
    fgm = fgm or 0
    fga = fga or 0
    tpm = tpm or 0
    tpa = tpa or 0
    ftm = ftm or 0
    fta = fta or 0
    reb = reb or 0
    oreb = oreb or 0
    dreb = dreb or 0
    ast = ast or 0
    tov = tov or 0
    stl = stl or 0
    blk = blk or 0
    pf = pf or 0
    pm = pm or 0
    minutes_played = minutes_played or 0
    games_played = games_played or 0
    games_started = games_started or 0
    ppg = round(points / max(games_played, 1), 1)
    fgp = round(fgm / max(fga, 1), 3)
    tpp = round(tpm / max(tpa, 1), 3)
    ftp = round(ftm / max(fta, 1), 3)
    rpg = round(reb / max(games_played, 1), 1)
    apg = round(ast / max(games_played, 1), 1)
    atr = round(ast / max(tov, 1), 2)
    eff = (points + reb + ast + stl + blk
           - (fga - fgm) - (fta - ftm) - tov)
    tsa = fga + 0.44 * fta
    tsp = round(points / max(2 * tsa, 1), 3)
    fp = round(points
               + 0.5 * fgm + 1.5 * tpm + 1.0 * ftm
               + 1.2 * reb + 1.5 * ast + 2.0 * stl + 2.0 * blk
               - 1.0 * tov - 0.5 * pf
               - 0.5 * (fga - fgm) - 0.5 * (fta - ftm), 1)

    return {
        "player_id": player_id,
        "season_id": season_id,
        "team_id": team_id,
        "games_played": games_played,
        "games_started": games_started,
        "minutes_played": round(minutes_played, 1),
        "points": points,
        "points_per_game": ppg,
        "field_goals_made": fgm,
        "field_goals_attempted": fga,
        "field_goal_pct": fgp,
        "three_points_made": tpm,
        "three_points_attempted": tpa,
        "three_point_pct": tpp,
        "free_throws_made": ftm,
        "free_throws_attempted": fta,
        "free_throw_pct": ftp,
        "rebounds": reb,
        "offensive_rebounds": oreb,
        "defensive_rebounds": dreb,
        "rebounds_per_game": rpg,
        "assists": ast,
        "assists_per_game": apg,
        "turnovers": tov,
        "assists_turnover_ratio": atr,
        "steals": stl,
        "blocks": blk,
        "personal_fouls": pf,
        "plus_minus": pm,
        "efficiency": eff,
        "true_shooting_pct": tsp,
        "usage_pct": None,
        "fantasy_points": fp,
    }


def compute_season_stats(season_id: int) -> int:
    """Compute and insert season stats for a given season_id.

    For players with multiple teams, keeps only the most-recent team.
    Returns the number of rows inserted.
    """
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Step 1: Delete existing stats for this season
    cur.execute("DELETE FROM nba.player_season_stats WHERE season_id = %s;", (season_id,))
    logger.info("Deleted %d existing rows for season %d", cur.rowcount, season_id)

    # Step 2: Aggregate raw per-player-per-team
    cur.execute(RAW_AGG_SQL, (season_id,))
    rows = cur.fetchall()
    logger.info("Found %d raw aggregates for season %d", len(rows), season_id)

    if not rows:
        conn.close()
        return 0

    # Step 3: Deduplicate to one row per player (keep latest team)
    deduped = OrderedDict()
    for r in rows:
        pid = r["player_id"]
        if pid not in deduped:
            deduped[pid] = _compute_result_row(
                r["player_id"], r["team_id"], r["season_id"], r["last_team_game"],
                r["games_played"], r["games_started"], r["minutes_played"],
                r["points"], r["field_goals_made"], r["field_goals_attempted"],
                r["three_points_made"], r["three_points_attempted"],
                r["free_throws_made"], r["free_throws_attempted"],
                r["rebounds"], r["offensive_rebounds"], r["defensive_rebounds"],
                r["assists"], r["turnovers"], r["steals"], r["blocks"],
                r["personal_fouls"], r["plus_minus"],
            )

    logger.info("Deduplicated to %d players (kept latest team for multi-team players)", len(deduped))

    # Step 4: Bulk insert
    cols_str = ",".join(INSERT_COLS)
    placeholders = ",".join(["%s"] * len(INSERT_COLS))
    insert_sql = f"INSERT INTO nba.player_season_stats ({cols_str}) VALUES ({placeholders})"

    inserted = 0
    for row in deduped.values():
        try:
            vals = tuple(row[c] for c in INSERT_COLS)
            cur.execute(insert_sql, vals)
            inserted += 1
        except Exception as e:
            logger.warning("  Failed for player_id=%s: %s", row["player_id"], e)

    conn.commit()
    conn.close()

    logger.info("Inserted %d rows into player_season_stats for season %d", inserted, season_id)
    return inserted


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    season_id = int(sys.argv[1])
    compute_season_stats(season_id)
