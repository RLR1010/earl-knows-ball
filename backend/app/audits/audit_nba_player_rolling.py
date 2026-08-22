"""Audit nba.player_rolling_stats against basketball-reference season stats.

For a sample of (player_id, season_id) pairs spanning all seasons we have:
  * find the LAST REGULAR-SEASON game row for the player in that season
    (last nba.games.game_type='REG' game; playoffs/playin excluded since they're
     chronologically after and share the season_id),
  * compute full-season totals from that row's cum_* (entering-the-game) PLUS the
    game's own boxscore: G, season points/reb/ast/min, PPG/RPG/APG/MPG, FG%/3P%/FT%,
  * print for manual comparison against basketball-reference.

game_date below uses the US-EASTERN date to align with bball-ref's calendar.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from app.core.config import settings  # noqa: E402

# (player_id, season_id, label) — sample spanning S16..S35, marquee + varied.
SAMPLES = [
    # (player_id, season_id, label)  — season_id is the season's START year (S16=2006-07)
    (2, 16, "LeBron 2006-07"), (2, 18, "LeBron 2008-09"), (2, 20, "LeBron 2010-11"),
    (2, 22, "LeBron 2012-13"), (2, 25, "LeBron 2015-16"), (2, 33, "LeBron 2023-24"),
    (1, 16, "Kobe 2006-07"), (1, 18, "Kobe 2008-09"), (1, 20, "Kobe 2010-11"),
    (1, 22, "Kobe 2012-13"),
    (30, 16, "Duncan 2006-07"), (30, 20, "Duncan 2010-11"), (30, 25, "Duncan 2015-16"),
    (46, 25, "CP3 2015-16"), (46, 35, "CP3 2024-25"),
    (678, 19, "Curry 2008-09"), (678, 22, "Curry 2011-12"), (678, 25, "Curry 2014-15"),
    (678, 28, "Curry 2017-18"), (678, 31, "Curry 2020-21"),
    (967, 25, "Giannis 2014-15"), (967, 28, "Giannis 2017-18"), (967, 30, "Giannis 2019-20"),
    (967, 33, "Giannis 2022-23"),
    (1284, 30, "Tatum 2019-20"), (1284, 33, "Tatum 2022-23"),
    (1406, 32, "SGA 2021-22"), (1406, 35, "SGA 2024-25"),
    (1126, 28, "Jokic 2017-18"), (1126, 30, "Jokic 2019-20"),
    (1126, 33, "Jokic 2022-23"), (1126, 35, "Jokic 2024-25"),
]

_AUDIT_SQL = """
WITH last_reg AS (
    SELECT pgs.player_id, g.season_id, pgs.game_id,
           (g.date AT TIME ZONE 'America/New_York')::date AS et_date,
           g.home_team_id, g.away_team_id
    FROM nba.player_game_stats pgs
    JOIN nba.games g ON g.id = pgs.game_id
    WHERE g.game_type = 'REG' AND g.status = 'FINAL'
      AND pgs.player_id = :player_id AND g.season_id = :season_id
    ORDER BY (g.date AT TIME ZONE 'America/New_York')::date DESC, g.id DESC
    LIMIT 1
),
prs AS (
    SELECT prs2.*, lr.et_date
    FROM nba.player_rolling_stats prs2
    JOIN last_reg lr ON lr.game_id = prs2.game_id AND lr.player_id = prs2.player_id
    WHERE prs2.player_id = :player_id AND prs2.season_id = :season_id
)
SELECT
    prs.player_id, prs.game_id, prs.season_id, prs.is_starter, prs.et_date,
    COALESCE(prs.minutes,0) AS game_minutes,
    COALESCE(prs.points,0) AS game_pts,
    COALESCE(prs.rebounds_total,0) AS game_reb,
    COALESCE(prs.assists,0) AS game_ast,
    COALESCE(prs.cum_games,0) AS cum_games,
    COALESCE(prs.cum_points,0) AS cum_pts,
    COALESCE(prs.cum_rebounds,0) AS cum_reb,
    COALESCE(prs.cum_assists,0) AS cum_ast,
    COALESCE(prs.cum_minutes,0) AS cum_min,
    COALESCE(prs.cum_fg_pct,0) AS cum_fg_pct,
    COALESCE(prs.cum_tp_pct,0) AS cum_tp_pct,
    COALESCE(prs.cum_ft_pct,0) AS cum_ft_pct
FROM prs
WHERE prs.player_id IS NOT NULL
"""

_FG_SQL = """
SELECT
  COALESCE(sum(pgs.field_goals_made),0)::int AS season_fgm,
  COALESCE(sum(pgs.field_goals_attempted),0)::int AS season_fga,
  COALESCE(sum(pgs.three_pointers_made),0)::int AS season_tpm,
  COALESCE(sum(pgs.three_pointers_attempted),0)::int AS season_tpa,
  COALESCE(sum(pgs.free_throws_made),0)::int AS season_ftm,
  COALESCE(sum(pgs.free_throws_attempted),0)::int AS season_fta,
  count(*)::int AS season_games,
  COALESCE(sum(pgs.points),0)::int AS season_pts,
  COALESCE(sum(pgs.rebounds_total),0)::int AS season_reb,
  COALESCE(sum(pgs.assists),0)::int AS season_ast,
  COALESCE(sum(pgs.turnovers),0)::int AS season_tov
FROM nba.player_game_stats pgs
JOIN nba.games g ON g.id=pgs.game_id
WHERE g.game_type='REG' AND g.status='FINAL'
  AND pgs.player_id=:player_id AND g.season_id=:season_id
"""

# minutes are stored as TEXT 'MM:SS' in player_game_stats; decode like the builder.
def _min_to_dec(v):
    if not v or str(v).strip() in ("", "0:00", "None", "nan"):
        return 0.0
    s = str(v).strip()
    if ":" in s:
        try:
            m, sec = s.split(":", 1)
            return round(int(m) + int(sec) / 60.0, 2)
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0

_MINUTES_SQL = """
SELECT pgs.minutes FROM nba.player_game_stats pgs
JOIN nba.games g ON g.id=pgs.game_id
WHERE g.game_type='REG' AND g.status='FINAL'
  AND pgs.player_id=:player_id AND g.season_id=:season_id
"""


def audit(engine, player_id, season_id, label=""):
    with engine.connect() as conn:
        row = conn.execute(
            text(_AUDIT_SQL), {"player_id": player_id, "season_id": season_id}
        ).mappings().first()
        if not row:
            print(f"  !! player {player_id} S{season_id}: no last-REG row found")
            return None
        # season totals via summing ALL REG games up to & incl the last REG game
        # (most robust: count every REG boxscore row for the player)
        tot = conn.execute(
            text(_FG_SQL), {"player_id": player_id, "season_id": season_id}
        ).mappings().first()
        mins = conn.execute(
            text(_MINUTES_SQL), {"player_id": player_id, "season_id": season_id}
        ).scalars().all()

    season_min = sum(_min_to_dec(m) for m in mins)
    G = tot["season_games"]
    ppg = tot["season_pts"] / G if G else 0.0
    rpg = tot["season_reb"] / G if G else 0.0
    apg = tot["season_ast"] / G if G else 0.0
    mpg = season_min / G if G else 0.0
    fgp = tot["season_fgm"] / tot["season_fga"] if tot["season_fga"] else 0.0
    tp3 = tot["season_tpm"] / tot["season_tpa"] if tot["season_tpa"] else 0.0
    ftp = tot["season_ftm"] / tot["season_fta"] if tot["season_fta"] else 0.0

    res = {
        "player_id": player_id,
        "season_id": season_id,
        "label": label,
        "last_reg_game": row["game_id"],
        "last_reg_et_date": str(row.get("et_date")),
        "G": G,
        "PTS": tot["season_pts"],
        "PPG": round(ppg, 1),
        "RPG": round(rpg, 1),
        "APG": round(apg, 1),
        "MPG": round(mpg, 1),
        "FGM": tot["season_fgm"], "FGA": tot["season_fga"], "FGpct": round(fgp * 100, 1),
        "3PM": tot["season_tpm"], "3PA": tot["season_tpa"], "3Ppct": round(tp3 * 100, 1),
        "FTM": tot["season_ftm"], "FTA": tot["season_fta"], "FTpct": round(ftp * 100, 1),
        "MIN": round(season_min, 0),
        "cum_games_at_last_reg": row["cum_games"],
    }
    return res


if __name__ == "__main__":
    engine = create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"))
    for pid, sid, slug in SAMPLES:
        r = audit(engine, pid, sid, slug)
        if r:
            print(f"{r['label'] or (pid,sid)}: G={r['G']} PPG={r['PPG']} RPG={r['RPG']} "
                  f"APG={r['APG']} MPG={r['MPG']} FG%={r['FGpct']} 3P%={r['3Ppct']} FT%={r['FTpct']} "
                  f"({r['FGM']}/{r['FGA']}FG {r['3PM']}/{r['3PA']}3P {r['FTM']}/{r['FTA']}FT) min={r['MIN']} "
                  f"lastreg=g{r['last_reg_game']} {r['last_reg_et_date']}")
