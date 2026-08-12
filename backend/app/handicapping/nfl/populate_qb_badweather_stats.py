"""Populate nfl.qb_badweather_stats -- QB passer rating in cold/precipitation games.

For each QB x game target, compute the QB's NFL passer rating in PRIOR (leak-free)
starts that were:
  - cold: game-time temperature < 40F
  - precip: weather_condition matches rain|snow|drizzle|thunder|shower

Mirrors nfl.team_badweather_stats structure (feeds_into_game_id) so the data
loader joins it identically for the resolved home/away starter. Uses raw per-game
passing from nfl.qb_cumulative_stats joined to nfl.games for weather/date.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import text

from app.database import SessionLocal

logger = logging.getLogger(__name__)

PRECIP_RE = r"rain|snow|drizzle|thunder|shower"
COLD_TEMP = 40.0

_TABLE = "nfl.qb_badweather_stats"


def _precip(cond):
    return bool(cond) and bool(re.search(PRECIP_RE, cond.lower()))


def passer_rating(att, comp, yds, td, intc):
    """Standard NFL passer rating (0-158.3)."""
    if not att or att <= 0:
        return None
    a = max(0.0, min(((comp / att) - 0.30) * 5.0, 2.375))
    b = max(0.0, min(((yds / att) - 3.0) * 0.25, 2.375))
    c = max(0.0, min((td / att) * 20.0, 2.375))
    d = max(0.0, min(2.375 - ((intc / att) * 25.0), 2.375))
    return round(((a + b + c + d) / 6.0) * 100.0, 1)


def _rows_for(conn):
    sql = text(
        """
        SELECT q.player_id, q.game_id, q.season, q.game_type, q.week,
               q.game_date, q.team_abbr, q.starter_flag,
               q.pass_attempts, q.pass_completions, q.pass_yards, q.pass_tds, q.pass_int,
               g.temperature, g.weather_condition, g.roof_type
        FROM nfl.qb_cumulative_stats q
        JOIN nfl.games g ON g.id = q.game_id
        WHERE g.status = 'FINAL'
          AND g.game_type = 'REG'
    """
    )
    return conn.execute(sql).mappings().all()


def _build_row(r, cold, warm, precip, dry):
    def _rate(games):
        if not games:
            return None, None
        # aggregate raw passing counts across games, then one rating
        att = sum(g["att"] for g in games)
        comp = sum(g["comp"] for g in games)
        yds = sum(g["yds"] for g in games)
        td = sum(g["td"] for g in games)
        intc = sum(g["intc"] for g in games)
        return len(games), passer_rating(att, comp, yds, td, intc)

    c_starts, c_rate = _rate(cold)
    w_starts, w_rate = _rate(warm)
    p_starts, p_rate = _rate(precip)
    d_starts, d_rate = _rate(dry)
    return {
        "player_id": r["player_id"],
        "game_id": r["game_id"],
        "season": r["season"],
        "game_type": (r.get("game_type") or "REG"),
        "week": r["week"],
        "game_date": r["game_date"],
        "team_abbr": r["team_abbr"],
        "feeds_into_game_id": r["game_id"],
        "cold_starts": c_starts,
        "cold_passer_rating": c_rate,
        "warm_starts": w_starts,
        "warm_passer_rating": w_rate,
        "precip_starts": p_starts,
        "precip_passer_rating": p_rate,
        "dry_starts": d_starts,
        "dry_passer_rating": d_rate,
    }


def run(conn=None, min_starts: int = 1) -> dict:
    """Build and insert qb_badweather_stats for QBs with >= min_starts prior games."""
    own_conn = conn is None
    if own_conn:
        conn = SessionLocal()
    try:
        rows = _rows_for(conn)

        # Per QB: list of starts, sorted by date, tagged cold/precip + your own passing
        qb_games: dict[int, list[dict]] = {}
        for r in rows:
            # Dome games are always warm + dry (climate-controlled, no weather).
            is_dome = (r.get("roof_type") == "dome")
            if is_dome:
                cold, warm, precip = False, True, False
            else:
                cold = (r["temperature"] is not None and r["temperature"] < COLD_TEMP)
                warm = (r["temperature"] is not None and r["temperature"] >= COLD_TEMP)
                precip = _precip(r["weather_condition"])
            qb_games.setdefault(r["player_id"], []).append(
                {
                    "date": r["game_date"],
                    "att": r["pass_attempts"] or 0,
                    "comp": r["pass_completions"] or 0,
                    "yds": r["pass_yards"] or 0,
                    "td": r["pass_tds"] or 0,
                    "intc": r["pass_int"] or 0,
                    "cold": cold,
                    "warm": warm,
                    "precip": precip,
                    "starter": bool(r["starter_flag"]),
                }
            )
        for pid in qb_games:
            qb_games[pid].sort(key=lambda g: g["date"] or __import__("datetime").date.min)

        insert_rows = []
        for r in rows:
            pid = r["player_id"]
            target_date = r["game_date"]
            prior = [
                g for g in qb_games.get(pid, [])
                if g["starter"] and g["date"] is not None and target_date is not None
                and g["date"] < target_date
            ]
            cold = [g for g in prior if g["cold"]]
            warm = [g for g in prior if g["warm"]]
            precip = [g for g in prior if g["precip"]]
            dry = [g for g in prior if not g["precip"]]
            insert_rows.append(_build_row(r, cold, warm, precip, dry))

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

        return {"qb_rows": len(insert_rows), "games": len(set(r["game_id"] for r in rows))}
    finally:
        if own_conn:
            conn.close()
