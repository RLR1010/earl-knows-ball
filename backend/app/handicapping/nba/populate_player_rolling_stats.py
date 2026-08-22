"""Build nba.player_rolling_stats — per-player rolling/cumulative stats, one row per
(player_id, game_id), mirroring mlb.player_batting_rolling_stats.

Source: nba.player_game_stats (raw boxscore) joined with nba.games for season_id and
the US-EASTERN game_date ((g.date AT TIME ZONE 'America/New_York')::date per TOOLS.md).

Semantics (no look-ahead):
  * Each row is FOR game G. cum_*/ppg_5/... values are computed from the player's
    games STRICTLY BEFORE G (excluding G itself). Reading row-for-G gives you the
    player's form "entering" game G.
  * prev_game_id[_season] = the player's prior game id (cross-season / within-season).

This is idempotent: it REPLACES the table (TRUNCATE + full rebuild) because it's
derived purely from player_game_stats. Full rebuild is fast (~528k rows).
"""
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import settings  # noqa: E402

LOG = __import__("logging").getLogger("earl.nba_player_rolling_stats")

SOURCE_SQL = """
    SELECT
        pgs.player_id,
        pgs.game_id,
        pgs.team_id,
        g.season_id,
        (g.date AT TIME ZONE 'America/New_York')::date AS game_date,
        pgs.is_starter,
        pgs.position,
        pgs.minutes                          AS minutes_txt,
        pgs.field_goals_made                 AS fgm,
        pgs.field_goals_attempted            AS fga,
        pgs.field_goal_pct                   AS fg_pct,
        pgs.three_pointers_made              AS tpm,
        pgs.three_pointers_attempted         AS tpa,
        pgs.three_pointer_pct                AS tp_pct,
        pgs.free_throws_made                 AS ftm,
        pgs.free_throws_attempted            AS fta,
        pgs.free_throw_pct                   AS ft_pct,
        pgs.rebounds_offensive               AS rebounds_offensive,
        pgs.rebounds_defensive               AS rebounds_defensive,
        pgs.rebounds_total                   AS rebounds_total,
        pgs.assists,
        pgs.steals,
        pgs.blocks,
        pgs.turnovers,
        pgs.fouls_personal,
        pgs.points,
        pgs.plus_minus,
        pgs.fantasy_points
    FROM nba.player_game_stats pgs
    JOIN nba.games g ON g.id = pgs.game_id
    WHERE g.status IN ('FINAL', 'POST', 'PLAYIN')
""".strip()

# NBA status enum is SCHEDULED/IN_PROGRESS/FINAL/POSTPONED/CANCELLED (no POST/PLAYIN;
# playoffs+play-in are all FINAL there). Override the WHERE to a plain FINAL filter.

WINDOWS = [5, 10, 15, 30]
_STAT_G = {  # stat -> game-level aggregates per window (rolling avg over prior games)
    "ppg": "points",
    "rpg": "rebounds_total",
    "apg": "assists",
    "spg": "steals",
    "bpg": "blocks",
    "tpg": "turnovers",
    "mpg": "minutes",
    "plus_minus": "plus_minus",
}

COLUMNS = [
    "player_id", "game_id", "team_id", "season_id", "game_date", "is_starter", "position",
    "minutes_txt", "minutes", "points", "rebounds_offensive", "rebounds_defensive",
    "rebounds_total", "assists", "steals", "blocks", "turnovers", "fouls_personal",
    "plus_minus", "fantasy_points",
    "fgm", "fga", "fg_pct", "tpm", "tpa", "tp_pct", "ftm", "fta", "ft_pct",
    "prev_game_id", "prev_game_date", "prev_game_id_season", "prev_game_date_season",
    "cum_games", "cum_points", "cum_rebounds", "cum_assists", "cum_minutes",
    "cum_ppg", "cum_rpg", "cum_apg", "cum_mpg",
    "cum_fg_pct", "cum_tp_pct", "cum_ft_pct",
    "ppg_5", "ppg_10", "ppg_15", "ppg_30",
    "rpg_5", "apg_5", "mpg_5", "spg_5", "bpg_5", "tpg_5", "plus_minus_5",
    "fg_pct_5", "tp_pct_5", "ft_pct_5", "gp_5",
]


SOURCE_SQL = SOURCE_SQL.replace(
    "WHERE g.status IN ('FINAL', 'POST', 'PLAYIN')",
    "WHERE g.status = 'FINAL' AND pgs.points IS NOT NULL",  # exclude DNP placeholder rows (NULL points)
)


def _min_to_dec(minutes_txt):
    """'34:12' -> 34.2 (decimal minutes). None/'0:00'/empty -> 0.0."""
    if not minutes_txt or str(minutes_txt).strip() in ("", "0:00", "None", "nan"):
        return 0.0
    s = str(minutes_txt)
    if ":" in s and " " not in s.split(":", 1)[1]:
        m, sec = s.split(":", 1)
        try:
            return round(int(m) + int(sec) / 60.0, 2)
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def build(engine, full=True):
    """Full rebuild of nba.player_rolling_stats."""
    df = pd.read_sql(SOURCE_SQL + (" ORDER BY pgs.player_id, game_date, pgs.game_id" if False else ""), engine)
    if df.empty:
        raise RuntimeError("no player_game_stats rows")

    # decimal minutes
    df["minutes"] = df["minutes_txt"].map(_min_to_dec).fillna(0.0)

    # sort (player, season, et game_date, game_id) for strict chronological order
    df.sort_values(["player_id", "game_date", "game_id"], inplace=True)

    g = df.groupby("player_id", sort=False)

    # ── prior-game pointers ──
    df["prev_game_id"] = g["game_id"].shift(1)
    df["prev_game_date"] = g["game_date"].shift(1)
    gs = df.groupby(["player_id", "season_id"], sort=False)
    df["prev_game_id_season"] = gs["game_id"].shift(1)
    df["prev_game_date_season"] = gs["game_date"].shift(1)

    # ── season-to-date cumulative, ENTERING this game (shift by 1 within season) ──
    for stat, col in [("cum_points", "points"), ("cum_rebounds", "rebounds_total"),
                      ("cum_assists", "assists"), ("cum_minutes", "minutes")]:
        df[stat] = gs[col].shift(1).fillna(0).groupby(df["player_id"], sort=False).cumsum()
    # games played entering game (shifted cumsum of 1 within season)
    df["ones"] = 1
    df["cum_games"] = gs["ones"].shift(1).fillna(0).groupby(df["player_id"], sort=False).cumsum().astype(int)

    def _avg_entering(stat_col):
        # cumulative FG/TP/FT pct entering game = cum made / cum attempted (shifted)
        made = gs[stat_col + "m"].shift(1).fillna(0).groupby(df["player_id"], sort=False).cumsum()
        att = gs[stat_col + "a"].shift(1).fillna(0).groupby(df["player_id"], sort=False).cumsum()
        return (made / att.replace(0, float("nan"))).fillna(0.0)

    df["cum_fg_pct"] = _avg_entering("fg")
    df["cum_tp_pct"] = _avg_entering("tp")
    df["cum_ft_pct"] = _avg_entering("ft")
    df["cum_ppg"] = (df["cum_points"] / df["cum_games"].replace(0, 1)).round(3)
    df["cum_rpg"] = (df["cum_rebounds"] / df["cum_games"].replace(0, 1)).round(3)
    df["cum_apg"] = (df["cum_assists"] / df["cum_games"].replace(0, 1)).round(3)
    df["cum_mpg"] = (df["cum_minutes"] / df["cum_games"].replace(0, 1)).round(2)

    # ── rolling windows, ENTERING this game (last N prior games) ──

    # ⚠️ CORRECT per-player rolling: shift within player, then ROLL within player.
    # A bare "g[col].shift(1).rolling(w).mean()" would roll across ALL players
    # (shift() returns a plain Series, losing the grouped context) -> mixing players.
    # We must re-group the shifted series by player and use .transform() so the
    # window stays per-player AND aligns back to the original row index.
    _id = df["player_id"]
    for w in WINDOWS:
        shifted = g["points"].shift(1)
        df[f"ppg_{w}"] = shifted.groupby(_id).transform(
            lambda s: s.rolling(w, min_periods=1).mean()
        ).round(2)

    df["rpg_5"] = g["rebounds_total"].shift(1).groupby(_id).transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["apg_5"] = g["assists"].shift(1).groupby(_id).transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["mpg_5"] = g["minutes"].shift(1).groupby(_id).transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["spg_5"] = g["steals"].shift(1).groupby(_id).transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["bpg_5"] = g["blocks"].shift(1).groupby(_id).transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["tpg_5"] = g["turnovers"].shift(1).groupby(_id).transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["plus_minus_5"] = g["plus_minus"].shift(1).groupby(_id).transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)

    # FG/TP/FT % over last 5 = sum(made)/sum(attempt), per player, entering game
    def _pct_5(made_col, att_col):
        made = g[made_col].shift(1).groupby(_id).transform(lambda s: s.rolling(5, min_periods=1).sum())
        att = g[att_col].shift(1).groupby(_id).transform(lambda s: s.rolling(5, min_periods=1).sum())
        return (made / att.replace(0, float("nan"))).fillna(0.0).round(3)

    df["fg_pct_5"] = _pct_5("fgm", "fga")
    df["tp_pct_5"] = _pct_5("tpm", "tpa")
    df["ft_pct_5"] = _pct_5("ftm", "fta")
    df["gp_5"] = g["ones"].shift(1).groupby(_id).transform(
        lambda s: s.rolling(5, min_periods=1).sum()
    ).fillna(0).astype(int)

    df.drop(columns=["ones"], inplace=True)

    # normalize types + nulls for DB
    out = df[COLUMNS].copy()
    for c in ["is_starter", "position", "minutes_txt"]:
        out[c] = out[c].where(pd.notna(out[c]), None)
    out["game_date"] = out["game_date"].astype(object).where(pd.notna(out["game_date"]), None)

    # replace NaN with None (JSON/DB-safe). Integers already na -> float NaN; map to None.
    out = out.astype(object).where(pd.notna(out), None)

    # ── write (replace) ──
    with engine.begin() as conn:
        conn.exec_driver_sql("TRUNCATE TABLE nba.player_rolling_stats")
    rows = out.to_dict("records")
    # FAST bulk insert via psycopg2.extras.execute_values (executemany is ~10x slower
    # for ~528k rows). Overwrite key player_id,game_id is the PK -> ON CONFLICT noop-safe.
    import psycopg2.extras
    from sqlalchemy import text as _txt
    cols = ", ".join(COLUMNS)
    vals = ", ".join(["%s"] * len(COLUMNS))
    template = "({values})".format(values=", ".join(["%({0})s".format(c) for c in COLUMNS]))

    def _chunks(lst, n=20000):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    # Use the raw DBAPI connection for execute_values
    with engine.connect() as conn:
        raw = conn.connection.driver_connection
        with raw.cursor() as cur:
            cur.execute("TRUNCATE TABLE nba.player_rolling_stats")
            for chunk in _chunks(rows):
                psycopg2.extras.execute_values(
                    cur,
                    f"INSERT INTO nba.player_rolling_stats ({cols}) VALUES %s",
                    chunk,
                    template=template,
                    page_size=5000,
                )
        raw.commit()
    LOG.info("player_rolling_stats built: %s rows", len(rows))
    return len(rows)


def main():
    engine = create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"))
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
    n = build(engine, full=True)
    print(f"DONE: nba.player_rolling_stats = {n} rows")


if __name__ == "__main__":
    main()
