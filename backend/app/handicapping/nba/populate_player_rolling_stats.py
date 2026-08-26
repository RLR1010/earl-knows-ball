"""Build nba.player_rolling_stats — per-player rolling/cumulative stats, one row per
(player_id, game_id), mirroring mlb.player_batting_rolling_stats.

Source: nba.player_game_stats (raw boxscore) joined with nba.games for season_id and
the US-EASTERN game_date ((g.date AT TIME ZONE 'America/New_York')::date per TOOLS.md).

Semantics (INCLUSIVE, no look-ahead):
  * Each row is FOR game G. cum_*/ppg_5/... values INCLUDE game G's own stats
    (i.e. cum_* at row G = season total THROUGH G; ppg_5 at row G = avg of the
    last 5 games ending at G). This matches mlb.player_batting_rolling_stats and
    every other rolling/team table in the system. Leak-safety is the DATA
    LOADER's job: it reads the PRIOR row (prev_game_id[_season], or
    `game_id < g.id LIMIT 1`) to get "through the last completed game" without
    peeking at G. The builder must NOT shift stats to represent "form entering
    G" (that was the OLD exclusive bug -- one game staler than every other
    table; FIXED 2026-08-25).
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
      AND g.game_type != 'PRE'  -- rolling/cumulative stats never include preseason
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
    "WHERE g.status IN ('FINAL', 'POST', 'PLAYIN')\n      AND g.game_type != 'PRE'",
    "WHERE g.status = 'FINAL' AND pgs.points IS NOT NULL\n      AND g.game_type != 'PRE'",  # exclude DNP placeholder rows (NULL points)
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
    # 🔴 FIX 2026-08-24: the cumsum MUST keep the (player, season) grouping so the
    # running total resets each season, for the leak-safe "season-to-date" semantics.
    # The old code re-grouped by player_id ALONE (`.groupby(df["player_id"])`), which
    # summed the shifted season-openers across the player's ENTIRE career -> career
    # cumulatives, defeating the table's purpose (look up season stats at a moment in
    # time) and inflating pick-card "active/starters vs team" sums. We pass the actual
    # df column Series (index-aligned with the shifted series) so the season boundary
    # stays in the cumsum.
    gpk = [df["player_id"], df["season_id"]]
    # 🔴 FIX 2026-08-25 (INCLUSIVE): cumulative stats INCLUDE the current row's
    # own game. Rows must NOT represent "form entering this game" (that was the
    # OLD exclusive bug: .shift(1) made each row's cum = through the PREVIOUS
    # game, one game staler than MLB/NFL/team tables). Remove the shift so
    # cum_* at game G = season total THROUGH game G. Leak-safety is the data
    # loader's job (reads prior row via game_id < g.id / prev_game_id pointer).
    for stat, col in [("cum_points", "points"), ("cum_rebounds", "rebounds_total"),
                      ("cum_assists", "assists"), ("cum_minutes", "minutes")]:
        df[stat] = df[col].fillna(0).groupby(gpk, sort=False).cumsum()
    # games played THROUGH this game (unshifted cumsum of 1 within season): row G = N
    df["ones"] = 1
    df["cum_games"] = df["ones"].fillna(0).groupby(gpk, sort=False).cumsum().astype(int)

    def _avg_pct(stat_col):
        # cumulative FG/TP/FT pct through this game = cum made / cum attempted (inclusive, season-scoped)
        made = df[stat_col + "m"].fillna(0).groupby(gpk, sort=False).cumsum()
        att = df[stat_col + "a"].fillna(0).groupby(gpk, sort=False).cumsum()
        return (made / att.replace(0, float("nan"))).fillna(0.0)

    df["cum_fg_pct"] = _avg_pct("fg")
    df["cum_tp_pct"] = _avg_pct("tp")
    df["cum_ft_pct"] = _avg_pct("ft")
    df["cum_ppg"] = (df["cum_points"] / df["cum_games"]).round(3)
    df["cum_rpg"] = (df["cum_rebounds"] / df["cum_games"]).round(3)
    df["cum_apg"] = (df["cum_assists"] / df["cum_games"]).round(3)
    df["cum_mpg"] = (df["cum_minutes"] / df["cum_games"]).round(2)

    # ── rolling windows, INCLUSIVE of this game (last N games THROUGH current row) ──

    # 🔴 FIX 2026-08-25 (INCLUSIVE): rolling windows INCLUDE the current row's own
    # game (ppg_5 at row G = avg of games G-4..G). The OLD code shifted the stat
    # values (.shift(1)) so windows excluded the current game ("last N prior"),
    # one game staler than MLB/NFL/team tables.
    #
    # We use the per-(player, season) GroupBy.transform: `gs_roll["col"]...` gives
    # the current row's value at each position (INCLUSIVE, no shift), is aligned
    # back to the original row index, and the window never bleeds across players OR
    # across season boundaries. Season-scoping matches MLB's populate_batting_rolling
    # (`PARTITION BY p.player_id, p.season_id` for w5/w15/w30), so a player's ppg_5
    # at the start of season N reflects ONLY season N's games, never last season's.
    # The OLD code grouped by player_id ONLY, so the first ~4 games of every season
    # blended last season's games into ppg_5/fg_pct_5/etc. — cross-season noise that
    # leaked into the model (esp. mid-season call-ups + every season-opener).
    # Leak-safety stays in the loader.
    gs_roll = df.groupby(["player_id", "season_id"], sort=False)
    for w in WINDOWS:
        df[f"ppg_{w}"] = gs_roll["points"].transform(
            lambda s: s.rolling(w, min_periods=1).mean()
        ).round(2)

    df["rpg_5"] = gs_roll["rebounds_total"].transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["apg_5"] = gs_roll["assists"].transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["mpg_5"] = gs_roll["minutes"].transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["spg_5"] = gs_roll["steals"].transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["bpg_5"] = gs_roll["blocks"].transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["tpg_5"] = gs_roll["turnovers"].transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)
    df["plus_minus_5"] = gs_roll["plus_minus"].transform(lambda s: s.rolling(5, min_periods=1).mean()).round(2)

    # FG/TP/FT % over last 5 = sum(made)/sum(attempt), per player+season, INCLUSIVE of this game
    def _pct_5(made_col, att_col):
        made = gs_roll[made_col].transform(lambda s: s.rolling(5, min_periods=1).sum())
        att = gs_roll[att_col].transform(lambda s: s.rolling(5, min_periods=1).sum())
        return (made / att.replace(0, float("nan"))).fillna(0.0).round(3)

    df["fg_pct_5"] = _pct_5("fgm", "fga")
    df["tp_pct_5"] = _pct_5("tpm", "tpa")
    df["ft_pct_5"] = _pct_5("ftm", "fta")
    df["gp_5"] = gs_roll["ones"].transform(
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
