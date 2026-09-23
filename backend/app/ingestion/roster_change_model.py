"""Offseason roster-turnover value model (NFL) -- shared by the preview script and
the persistent `nfl.roster_changes` builder.

This is the v2 model, extracted verbatim from `app/scripts/preview_roster_changes.py`
so it can be *persisted* and consumed by the power-ratings engine.

  quality(player) = POSITION_MAX[position] * usage_share (prior-season production),
                    rookies -> draft-capital curve on draft_number, blended with
                    contract value (apy_cap_pct) at 55/45 within-position percentiles.
  unit value      = top-of-depth-chart players only, decaying weights (DEPTH_W).
  offseason delta = unit value (this season's roster) - unit value (last season's roster).
"""
from __future__ import annotations

from sqlalchemy import text

from app.ingestion.compute_injury_adjustments import (
    POSITION_GROUP,
    POSITION_MAX,
    _agg,
    _shares,
)

DEPTH_W = {
    "QB": [1.0],
    "RB": [1.0, 0.30],
    "WR": [1.0, 0.60, 0.35],
    "TE": [1.0, 0.20],
    "OL": [1.0, 0.9, 0.8, 0.7, 0.6],
    "DL": [1.0, 0.70, 0.45, 0.25],
    "LB": [1.0, 0.55, 0.20],
    "DB": [1.0, 0.75, 0.55, 0.35, 0.20],
    "ST": [1.0, 0.25],
}
UNIT_ORDER = ["QB", "RB", "WR", "TE", "OL", "DL", "LB", "DB", "ST"]


def _rookie_quality(pos: str | None, draft_number: int | None) -> float:
    mx = POSITION_MAX.get(pos or "", 0.6)
    if not draft_number:
        return mx * 0.15
    factor = max(0.1, 1.0 - (draft_number - 1) / 180.0)
    return mx * 0.7 * factor


async def team_deltas(db, from_year: int, to_year: int) -> dict[str, float]:
    """Net unit-value change per team from `from_year` roster -> `to_year` roster."""
    pmap = {
        r[0]: (r[1], r[2])
        for r in (
            await db.execute(text("SELECT id, position, team_id FROM nfl.players"))
        ).all()
    }
    g2p = {
        r[0]: r[1]
        for r in (
            await db.execute(
                text("SELECT nflverse_id, id FROM nfl.players WHERE nflverse_id IS NOT NULL")
            )
        ).all()
    }

    sid = await db.scalar(text("SELECT id FROM nfl.seasons WHERE year = :y"), {"y": from_year})
    agg = await _agg(db, sid, 99)
    sh = _shares(agg, pmap)
    pos_of = {pid: (p or None) for pid, (p, _t) in pmap.items()}

    qrows = (
        await db.execute(
            text(
                """
                SELECT player_id, sum(pass_attempts) a, sum(pass_yards) y,
                       sum(pass_tds) td, sum(pass_int) i,
                       sum(pass_sacks) sk, sum(pass_sack_yards) sky
                FROM nfl.player_weekly_stats
                WHERE season_id = :sid
                GROUP BY 1 HAVING sum(pass_attempts) >= 150
                """
            ),
            {"sid": sid},
        )
    ).all()
    prod: dict[int, float] = {
        pid: POSITION_MAX.get(pos_of.get(pid) or "", 0.6) * share for pid, share in sh.items()
    }
    for r in qrows:
        denom = (r[1] or 0) + (r[5] or 0)
        if denom:
            prod[r[0]] = (
                (r[2] or 0) + 20 * (r[3] or 0) - 45 * (r[4] or 0) - (r[6] or 0)
            ) / denom

    ctr = {
        r[0]: float(r[1])
        for r in (
            await db.execute(
                text(
                    "SELECT player_id, apy_cap_pct FROM nfl.contracts "
                    "WHERE player_id IS NOT NULL AND apy_cap_pct IS NOT NULL"
                )
            )
        ).all()
    }

    def _pct(d: dict[int, float]) -> dict[int, float]:
        groups: dict[str, list[tuple[float, int]]] = {}
        for pid, v in d.items():
            p = pos_of.get(pid)
            if p:
                groups.setdefault(p, []).append((v, pid))
        out: dict[int, float] = {}
        for lst in groups.values():
            lst.sort()
            n = len(lst)
            for i, (_v, pid) in enumerate(lst):
                out[pid] = i / (n - 1) if n > 1 else 0.5
        return out

    pct_prod = _pct(prod)
    pct_ctr = _pct(ctr)

    def quality(g: str, pos: str | None, dn: int | None) -> float:
        pid = g2p.get(g)
        if pid is not None and pid in pct_prod:
            pp = pct_prod[pid]
            pc = pct_ctr.get(pid)
            talent = 0.55 * pp + 0.45 * pc if pc is not None else pp
            return POSITION_MAX.get(pos or pos_of.get(pid) or "", 0.6) * (0.40 + 0.60 * talent)
        return _rookie_quality(pos, dn)

    _roster_sql = text(
        "SELECT gsis_id, team_abbr, position, draft_number FROM nfl.rosters WHERE season = :y"
    )
    r_from = {
        r[0]: (r[1], r[2], r[3])
        for r in (await db.execute(_roster_sql, {"y": from_year})).all()
    }
    r_to = {
        r[0]: (r[1], r[2], r[3]) for r in (await db.execute(_roster_sql, {"y": to_year})).all()
    }
    teams = sorted({t for t, _, _ in r_to.values() if t})

    def unit_total(rmap: dict, team: str, incumbents: set) -> float:
        groups: dict[str, list[tuple[float, int]]] = {}
        for g, (tm, pos, dn) in rmap.items():
            if tm != team:
                continue
            unit = POSITION_GROUP.get(pos or "")
            if not unit or unit not in DEPTH_W:
                continue
            groups.setdefault(unit, []).append((quality(g, pos, dn), 0 if g in incumbents else 1))
        total = 0.0
        for unit, lst in groups.items():
            w = DEPTH_W[unit]
            lst.sort(key=lambda x: (-x[0], x[1]))
            for i, (q, _inc) in enumerate(lst[: len(w)]):
                total += q * w[i]
        return total

    inc_to = {g for g, (tm, _, _) in r_from.items() if tm}
    return {
        t: unit_total(r_to, t, inc_to) - unit_total(r_from, t, set()) for t in teams
    }
