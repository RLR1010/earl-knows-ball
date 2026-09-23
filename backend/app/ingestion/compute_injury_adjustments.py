#!/usr/bin/env python3
"""Translate weekly NFL injury reports into a points adjustment per team.

Output: nfl.injury_adjustments (season, week, team_id, team_abbr, adj, n_players,
players jsonb) — `adj` is the number of rating points a team is docked for the
players unavailable for that week's game.

Model (v0, deliberately transparent):
    value(player) = POSITION_MAX[position] * usage_share * status_weight
    adj(team, week) = min(CAP, sum(value over that team's unavailable players))

  * usage_share = the player's production up to `week` divided by his team's
    leader in the same position group (so each team's top QB / WR / edge ~ 1.0).
    Production metric is position-group specific (pass attempts, targets,
    carries+targets, or defensive splash plays). Falls back to the previous
    season when a player has no production yet (early weeks / role proxy).
  * status_weight from the game designation (Out/Doubtful/Questionable) with a
    practice-status fallback (DNP / Limited) when no game designation is posted.

No look-ahead: only games/reports with week <= W are used. Idempotent per season.

Usage:
    python -m app.ingestion.compute_injury_adjustments            # 2022..current
    python -m app.ingestion.compute_injury_adjustments 2026
"""
from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL

# Points docked if a full-time starter at this position is unavailable.
POSITION_MAX = {
    "QB": 7.0,
    "WR": 2.0,
    "RB": 1.5,
    "FB": 0.6,
    "TE": 1.2,
    "T": 1.5, "OT": 1.5, "G": 1.5, "OG": 1.5, "C": 1.5, "OL": 1.5,
    "DE": 1.2, "EDGE": 1.3, "DT": 1.1, "NT": 1.0, "DL": 1.1,
    "LB": 1.0, "ILB": 1.0, "OLB": 1.0, "MLB": 1.0,
    "CB": 1.3, "DB": 1.1, "S": 0.9, "FS": 0.9, "SS": 0.9,
    "K": 0.3, "P": 0.3, "LS": 0.15,
}
DEFAULT_POS_MAX = 0.6

# position -> coarse group used for the "team leader" normalisation
POSITION_GROUP = {
    "QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE",
    "T": "OL", "OT": "OL", "G": "OL", "OG": "OL", "C": "OL", "OL": "OL",
    "DE": "DL", "EDGE": "DL", "DT": "DL", "NT": "DL", "DL": "DL",
    "LB": "LB", "ILB": "LB", "OLB": "LB", "MLB": "LB",
    "CB": "DB", "DB": "DB", "S": "DB", "FS": "DB", "SS": "DB",
    "K": "ST", "P": "ST", "LS": "ST",
}

REPORT_WEIGHT = {"out": 1.0, "doubtful": 0.6, "questionable": 0.3}
CAP = 12.0

DDL = """
CREATE TABLE IF NOT EXISTS nfl.injury_adjustments (
    season      integer NOT NULL,
    week        integer NOT NULL,
    team_id     integer NOT NULL,
    team_abbr   varchar(8),
    adj         double precision NOT NULL DEFAULT 0,
    n_players   integer NOT NULL DEFAULT 0,
    players     jsonb,
    model_version varchar(32) DEFAULT 'inj-v0',
    generated_at timestamptz DEFAULT now(),
    PRIMARY KEY (season, week, team_id)
)
"""

_AGG_SQL = """
    SELECT player_id, max(team_id) AS team_id,
           sum(coalesce(pass_attempts,0))       AS pa,
           sum(coalesce(rush_attempts,0))       AS ra,
           sum(coalesce(targets,0))             AS tg,
           sum(coalesce(tackles_combined,0))    AS tk,
           sum(coalesce(sacks_unassisted,0))    AS sk,
           sum(coalesce(interceptions,0))       AS ic,
           sum(coalesce(passes_defended,0))     AS pd,
           sum(coalesce(field_goals_attempted,0)) AS fg,
           sum(coalesce(punts,0))               AS pu
    FROM nfl.player_weekly_stats
    WHERE season_id = :s AND game_type = 'REG' AND week <= :w
    GROUP BY player_id
"""


def _metric(group: str, a: dict) -> float:
    if group == "QB":
        return a["pa"]
    if group == "RB":
        return a["ra"] + a["tg"]
    if group in ("WR", "TE"):
        return a["tg"]
    if group in ("DL", "LB", "DB"):
        return a["tk"] + 2 * a["sk"] + 3 * a["ic"] + a["pd"]
    if group == "ST":
        return a["fg"] * 3 + a["pu"]
    return 0.0


def _report_weight(report_status: str | None, practice_status: str | None) -> float:
    w = REPORT_WEIGHT.get((report_status or "").strip().lower(), 0.0)
    p = (practice_status or "").strip().lower()
    pract = 0.0
    if "did not" in p or p == "dnp":
        pract = 0.5
    elif "limited" in p:
        pract = 0.15
    return max(w, pract)


async def _agg(db, season_id: int, week: int) -> dict[int, dict]:
    rows = (await db.execute(text(_AGG_SQL), {"s": season_id, "w": week})).all()
    return {
        r[0]: {
            "team_id": r[1], "pa": float(r[2]), "ra": float(r[3]), "tg": float(r[4]),
            "tk": float(r[5]), "sk": float(r[6]), "ic": float(r[7]), "pd": float(r[8]),
            "fg": float(r[9]), "pu": float(r[10]),
        }
        for r in rows
    }


def _shares(agg: dict[int, dict], pmap: dict[int, tuple]) -> dict[int, float]:
    """player_id -> usage share within his team's position group (0..1)."""
    group_max: dict[tuple[int, str], float] = {}
    metric: dict[int, float] = {}
    for pid, a in agg.items():
        pos = pmap.get(pid, (None, None))[0] or ""
        g = POSITION_GROUP.get(pos)
        if not g:
            continue
        m = _metric(g, a)
        metric[pid] = m
        key = (a["team_id"], g)
        if m > group_max.get(key, 0.0):
            group_max[key] = m
    out: dict[int, float] = {}
    for pid, m in metric.items():
        pos = pmap.get(pid, (None, None))[0] or ""
        g = POSITION_GROUP[pos]
        mx = group_max.get((agg[pid]["team_id"], g), 0.0)
        out[pid] = min(1.0, m / mx) if mx > 0 else 0.0
    return out


async def compute(years: list[int] | None = None) -> int:
    years = years or list(range(2022, 2027))
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    written = 0
    try:
        async with Session() as db:
            await db.execute(text(DDL))
            await db.commit()

            seasons = {
                r[1]: r[0]
                for r in (await db.execute(text("SELECT id, year FROM nfl.seasons"))).all()
            }
            team_ids = {
                r[1].upper(): r[0]
                for r in (await db.execute(text("SELECT id, abbreviation FROM nfl.teams"))).all()
            }
            pmap = {
                r[0]: (r[1], r[2])
                for r in (await db.execute(text("SELECT id, position, name FROM nfl.players"))).all()
            }

            for year in years:
                sid = seasons.get(year)
                if sid is None:
                    print(f"{year}: SKIP (no season)")
                    continue
                prev_sid = seasons.get(year - 1)
                prev_shares = (
                    _shares(await _agg(db, prev_sid, 18), pmap) if prev_sid else {}
                )
                rep_weeks = (
                    await db.execute(
                        text(
                            "SELECT DISTINCT week FROM nfl.injuries WHERE season_id=:s "
                            "ORDER BY week"
                        ),
                        {"s": sid},
                    )
                ).all()
                for (w,) in rep_weeks:
                    shares = _shares(await _agg(db, sid, w), pmap)
                    inj = (
                        await db.execute(
                            text(
                                """
                                SELECT player_id, team_abbr, position, report_status,
                                       practice_status
                                FROM nfl.injuries
                                WHERE season_id=:s AND week=:w
                                """
                            ),
                            {"s": sid, "w": w},
                        )
                    ).all()

                    by_team: dict[tuple[int, str], list[dict]] = {}
                    for r in inj:
                        pid, abbr, pos = r[0], (r[1] or "").upper(), (r[2] or "")
                        wt = _report_weight(r[3], r[4])
                        if wt <= 0 or not abbr or abbr not in team_ids:
                            continue
                        share = shares.get(pid, 0.0)
                        if share <= 0.05:
                            share = prev_shares.get(pid, 0.0)
                        if share <= 0.05:
                            continue
                        posmax = POSITION_MAX.get(pos, DEFAULT_POS_MAX)
                        val = round(posmax * min(1.0, share) * wt, 1)
                        if val <= 0:
                            continue
                        tid = team_ids[abbr]
                        by_team.setdefault((tid, abbr), []).append(
                            {
                                "player_id": pid,
                                "name": pmap.get(pid, (None, None))[1],
                                "position": pos,
                                "report_status": (r[3] or "").strip() or None,
                                "practice_status": (r[4] or "").strip() or None,
                                "weight": round(wt, 2),
                                "value": val,
                            }
                        )

                    for (tid, abbr), players in by_team.items():
                        players.sort(key=lambda x: x["value"], reverse=True)
                        adj = round(min(CAP, sum(p["value"] for p in players)), 2)
                        await db.execute(
                            text(
                                """
                                INSERT INTO nfl.injury_adjustments
                                  (season, week, team_id, team_abbr, adj, n_players,
                                   players, model_version)
                                VALUES (:season, :week, :team_id, :team_abbr, :adj,
                                        :n_players, CAST(:players AS jsonb), 'inj-v0')
                                ON CONFLICT (season, week, team_id) DO UPDATE SET
                                  team_abbr=EXCLUDED.team_abbr, adj=EXCLUDED.adj,
                                  n_players=EXCLUDED.n_players, players=EXCLUDED.players,
                                  model_version=EXCLUDED.model_version,
                                  generated_at=now()
                                """
                            ),
                            {
                                "season": year, "week": w, "team_id": tid,
                                "team_abbr": abbr, "adj": adj,
                                "n_players": len(players), "players": json.dumps(players),
                            },
                        )
                        written += 1
                    await db.commit()
                print(f"{year}: {len(rep_weeks)} weeks processed")
    finally:
        await engine.dispose()
    print(f"Done: {written} team-week adjustments")
    return written


def main(argv: list[str]) -> None:
    years = [int(a) for a in argv[1:]] or None
    asyncio.run(compute(years))


if __name__ == "__main__":
    main(sys.argv)
