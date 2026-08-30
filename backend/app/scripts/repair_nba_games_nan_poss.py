"""
Repair NaN possession columns in nba.games for seasons 26-35 (2016-17 .. 2025-26).

WHY (2026-08-30): 153 PRE/POST/PLAYIN games in these seasons had NaN in the
team-level box-score possession columns (field_goals_attempted,
free_throws_attempted, field_goals_made, offensive_rebounds,
defensive_rebounds, total_turnovers) in nba.games. The adjusted-rating solver
(adjusted_ratings.py) divides by possessions, so each NaN game poisoned the
cumulative sum for its ENTIRE season, leaving cum_adj_ortg/cum_adj_drtg/cum_sos
NaN for whole seasons (2017-18, 2021-22, 2024-25) and the postseason rows of
others (2016-17, 2019-20, 2020-21).

FIX: reconstruct each affected game's team-level possession columns by summing
the per-player rows already present in nba.player_game_stats (which has complete
FGA/FTA/FGM/ORB/DRB/TOV + minutes for these games). No imputation — real data.

Verified: all 153 games have complete pgs data (156 team-rows, 0 NULL stat fields).

Usage: cd backend && PYTHONPATH=. ../venv/bin/python app/scripts/repair_nba_games_nan_poss.py
       (add --dry to preview without writing)
"""

from __future__ import annotations

import logging
import sys

import pandas as pd
from sqlalchemy import Engine, create_engine, text

logger = logging.getLogger("repair_nba_games_nan_poss")

RELEVANT_SEASONS = list(range(26, 36))  # 26=2016-17 .. 35=2025-26

# game_id -> list of (team_id, is_home) mappings and sums
NAV = "nba.games"
PGS = "nba.player_game_stats"

POSS_COLS = {
    "fga": "field_goals_attempted",
    "fta": "free_throws_attempted",
    "fgm": "field_goals_made",
    "orb": "rebounds_offensive",
    "drb": "rebounds_defensive",
    "tov": "turnovers",
}
HOME_NAN_COLS = {
    "home_field_goals_attempted": "h_fga",
    "home_free_throws_attempted": "h_fta",
    "home_field_goals_made": "h_fgm",
    "home_offensive_rebounds": "h_orb",
    "home_defensive_rebounds": "h_drb",
    "home_total_turnovers": "h_tov",
}
AWAY_NAN_COLS = {k.replace("home_", "away_"): v.replace("h_", "a_") for k, v in HOME_NAN_COLS.items()}


def find_nan_games(engine: Engine) -> pd.DataFrame:
    import app.handicapping.nba.adjusted_ratings as ar

    df = ar._load_games(engine)
    df["poss"] = df.apply(
        lambda r: ar.symmetric_poss(
            r.h_fga, r.h_fta, r.h_fgm, r.h_orb, r.h_drb, r.h_tov,
            r.a_fga, r.a_fta, r.a_fgm, r.a_orb, r.a_drb, r.a_tov,
        ), axis=1
    ).replace(0, 1)
    nd = df[(df.season_id.isin(RELEVANT_SEASONS)) & (df.poss.isna())]
    return nd[["game_id", "season_id", "game_type"]].drop_duplicates()


def reconstruct(engine: Engine, game_ids) -> pd.DataFrame:
    """Return rows (game_id, team_id, side[h/a], fga,fta,fgm,orb,drb,tov) summed from pgs."""
    ids = [int(x) for x in game_ids]
    cols = ", ".join(POSS_COLS.values())
    placeholders = ",".join(":p%d" % i for i in range(len(ids)))
    params = {"p%d" % i: v for i, v in enumerate(ids)}
    sql = text(f"""
        SELECT pg.game_id, pg.team_id, {cols}
        FROM {PGS} pg
        WHERE pg.game_id IN ({placeholders})
    """)
    with engine.connect() as c:
        pgs = pd.read_sql(sql, c, params=params)
    # long-form wide sums per (game_id, team_id)
    sums = pgs.groupby(["game_id", "team_id"], as_index=False)[list(POSS_COLS.values())].sum(numeric_only=True)
    sums = sums.rename(columns={v: k for k, v in POSS_COLS.items()})
    return sums


def side_map(engine: Engine, game_ids) -> dict:
    ids = [int(x) for x in game_ids]
    placeholders = ",".join(":p%d" % i for i in range(len(ids)))
    params = {"p%d" % i: v for i, v in enumerate(ids)}
    sql = f"SELECT id, home_team_id, away_team_id FROM {NAV} WHERE id IN ({placeholders})"
    with engine.connect() as c:
        rows = c.execute(text(sql), params).fetchall()
    return {r[0]: {"home": r[1], "away": r[2]} for r in rows}


def main() -> None:
    import os

    dry = "--dry" in sys.argv
    uri = os.environ.get("DATABASE_URL", "")
    if not uri:
        for line in open(".env"):
            if line.startswith("DATABASE_URL="):
                uri = line.split("=", 1)[1].strip()
                break
    engine = create_engine(uri.replace("+asyncpg", "+psycopg2"))

    nan = find_nan_games(engine)
    print(f"games with NaN possession cols (seasons 26-35): {len(nan)}")
    if nan.empty:
        print("no repairs needed")
        return

    gids = list(nan.game_id.unique())
    sums = reconstruct(engine, gids)
    smap = side_map(engine, gids)

    upd_home, upd_away = [], []
    for game_id, team_id, h_fga, h_fta, h_fgm, h_orb, h_drb, h_tov in sums.itertuples(index=False):
        g = smap.get(game_id)
        if not g:
            continue
        if team_id == g["home"]:
            upd_home.append((game_id, h_fga, h_fta, h_fgm, h_orb, h_drb, h_tov))
        elif team_id == g["away"]:
            upd_away.append((game_id, h_fga, h_fta, h_fgm, h_orb, h_drb, h_tov))
        else:
            print(f"  WARN game {game_id} team {team_id} not home/away")

    print(f"  home-side repairs: {len(upd_home)}, away-side repairs: {len(upd_away)}")
    covered_games = set()
    for u in upd_home: covered_games.add(u[0])
    for u in upd_away: covered_games.add(u[0])
    print(f"  distinct games covered: {len(covered_games)} / {len(gids)}")

    if dry:
        print("dry run — not writing")
        return

    setcols = {
        "h": ["home_field_goals_attempted", "home_free_throws_attempted", "home_field_goals_made",
              "home_offensive_rebounds", "home_defensive_rebounds", "home_total_turnovers"],
        "a": ["away_field_goals_attempted", "away_free_throws_attempted", "away_field_goals_made",
              "away_offensive_rebounds", "away_defensive_rebounds", "away_total_turnovers"],
    }
    from sqlalchemy import bindparam
    from sqlalchemy.dialects.postgresql import ARRAY
    from sqlalchemy import Integer as Int, Numeric

    GIDS = ARRAY(Int)
    NUMS = ARRAY(Numeric)
    import itertools
    with engine.begin() as conn:
        for side, data in (("h", upd_home), ("a", upd_away)):
            if not data:
                continue
            g = [r[0] for r in data]
            v = list(zip(*[r[1:] for r in data]))
            setcols_l = setcols[side]
            conn.execute(
                text(f"""
                    UPDATE {NAV} AS r SET
                      {setcols_l[0]} = u.v0, {setcols_l[1]} = u.v1,
                      {setcols_l[2]} = u.v2, {setcols_l[3]} = u.v3,
                      {setcols_l[4]} = u.v4, {setcols_l[5]} = u.v5
                    FROM unnest(:g, :v0, :v1, :v2, :v3, :v4, :v5) AS u(g, v0, v1, v2, v3, v4, v5)
                    WHERE r.id = u.g
                """).bindparams(
                    bindparam("g", type_=GIDS),
                    bindparam("v0", type_=NUMS), bindparam("v1", type_=NUMS),
                    bindparam("v2", type_=NUMS), bindparam("v3", type_=NUMS),
                    bindparam("v4", type_=NUMS), bindparam("v5", type_=NUMS),
                ),
                {"g": g, "v0": v[0], "v1": v[1], "v2": v[2],
                 "v3": v[3], "v4": v[4], "v5": v[5]},
            )
    print("repair write complete")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
