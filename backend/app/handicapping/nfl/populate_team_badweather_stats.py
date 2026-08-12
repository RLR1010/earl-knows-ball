"""Populate nfl.team_badweather_stats -- situational cold/precipitation stats.

For each team x game target, compute the team's PPG / YPG / win% in PRIOR
(leak-free) games that were:
  - cold: game-time temperature < 40F
  - precip: weather_condition matches rain|snow|drizzle|thunder|shower

Mirrors the structure of nfl.team_rolling_stats (game_id + team_abbr + season +
week + is_home + feeds_into_game_id) so data_loader joins it identically. Only
PRIOR games (date < target date) are used -> no lookahead leakage.

Source per-game team stats: nfl.cumulative_game_stats (off_pts, off_total_yds)
joined to nfl.games for weather/date; wins derived from games final score.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger(__name__)

# Consistent with nfl.player_splits precipitation classification.
PRECIP_RE = r"rain|snow|drizzle|thunder|shower"
COLD_TEMP = 40.0  # < 40F

_TABLE = "nfl.team_badweather_stats"


def _precip(cond):
    return bool(cond) and bool(re.search(PRECIP_RE, cond.lower()))


def _rows_for(conn):
    """Load per-game team offensive stats (diffed from cumulative + weather/win/is_home)."""
    sql = text(
        """
        WITH per_game AS (
            SELECT
                c.game_id, c.team_abbr, c.season, c.season_type AS game_type, c.week,
                g.date AS game_date, g.temperature, g.weather_condition,
                g.home_score, g.away_score,
                (ht.abbreviation = c.team_abbr) AS is_home,
                c.off_pts - COALESCE(LAG(c.off_pts) OVER w, 0) AS off_pts_pg,
                c.off_total_yds - COALESCE(LAG(c.off_total_yds) OVER w, 0) AS off_yds_pg
            FROM nfl.cumulative_game_stats c
            JOIN nfl.games g ON g.id = c.game_id
            LEFT JOIN nfl.teams ht ON ht.id = g.home_team_id
            WHERE g.status = 'FINAL' AND g.game_type = 'REG'
            WINDOW w AS (PARTITION BY c.team_abbr, c.season ORDER BY c.week)
        )
        SELECT * FROM per_game WHERE off_pts_pg IS NOT NULL
        """
    )
    return conn.execute(sql).mappings().all()


def _win(r):
    """Team won if its side scored more (is_home from join)."""
    hs = r.get("home_score")
    aw = r.get("away_score")
    if hs is None or aw is None:
        return None
    return (hs > aw) if r["is_home"] else (aw > hs)


def _build_row(r, cold, precip):
    def _agg(games):
        if not games:
            return None, None, None
        pts = sum(g["pts"] for g in games)
        yds = sum(g["yds"] for g in games)
        w = sum(1 for g in games if g["win"])
        n = len(games)
        return round(pts / n, 1), round(yds / n, 1), round(w / n, 3)

    c_ppg, c_ypg, c_wp = _agg(cold)
    p_ppg, p_ypg, p_wp = _agg(precip)
    return {
        "game_id": r["game_id"],
        "team_abbr": r["team_abbr"],
        "season": r["season"],
        "game_type": (r.get("game_type") or "REG"),
        "week": r["week"],
        "game_date": r["game_date"],
        "is_home": bool(r["is_home"]),
        "feeds_into_game_id": r["game_id"],
        "cold_games": len(cold) or None,
        "cold_ppg": c_ppg,
        "cold_ypg": c_ypg,
        "cold_win_pct": c_wp,
        "precip_games": len(precip) or None,
        "precip_ppg": p_ppg,
        "precip_ypg": p_ypg,
        "precip_win_pct": p_wp,
    }


def run(conn=None) -> dict:
    """Build and insert team_badweather_stats. Returns row counts."""
    own_conn = conn is None
    if own_conn:
        conn = SessionLocal()
    try:
        rows = _rows_for(conn)

        # Index per team: list of game dicts
        team_games: dict[str, list[dict]] = {}
        for r in rows:
            team_games.setdefault(r["team_abbr"], []).append(
                {
                    "date": r["game_date"],
                    "pts": r["off_pts_pg"] or 0,
                    "yds": r["off_yds_pg"] or 0,
                    "cold": (r["temperature"] is not None and r["temperature"] < COLD_TEMP),
                    "precip": _precip(r["weather_condition"]),
                    "win": _win(r),
                }
            )
        for t in team_games:
            team_games[t].sort(key=lambda g: g["date"] or __import__("datetime").date.min)

        # Per target row, aggregate the team's PRIOR cold/precip games.
        insert_rows = []
        for r in rows:
            t = r["team_abbr"]
            target_date = r["game_date"]
            prior = [
                g for g in team_games.get(t, [])
                if g["date"] is not None and target_date is not None and g["date"] < target_date
            ]
            cold = [g for g in prior if g["cold"]]
            precip = [g for g in prior if g["precip"]]
            insert_rows.append(_build_row(r, cold, precip))

        if insert_rows:
            game_ids = sorted({r["game_id"] for r in rows})
            for i in range(0, len(game_ids), 900):
                chunk = game_ids[i : i + 900]
                ph = ", ".join([":g%d" % x for x in range(len(chunk))])
                conn.execute(
                    text(f"DELETE FROM {_TABLE} WHERE game_id IN ({ph})"),
                    {f"g{x}": gid for x, gid in enumerate(chunk)},
                )
            for i in range(0, len(insert_rows), 1500):
                chunk = insert_rows[i : i + 1500]
                cols = list(chunk[0].keys())
                col_sql = ", ".join(cols)
                ph = ", ".join([":" + c for c in cols])
                conn.execute(text(f"INSERT INTO {_TABLE} ({col_sql}) VALUES ({ph})"), chunk)
            conn.commit()

        return {"team_rows": len(insert_rows), "games": len(set(r["game_id"] for r in rows))}
    finally:
        if own_conn:
            conn.close()
