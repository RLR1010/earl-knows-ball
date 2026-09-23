#!/usr/bin/env python3
"""Preview offseason roster-turnover value per team (no writes).

v1 (broken): valued each joiner/leaver by his OWN prior production -> every QB
move was worth ~7 points, so signing a backup QB looked like a massive upgrade
and losing a starter looked like a catastrophe. (Rich: "Fields adding value to
KC is a joke; he is a backup they hope never sees the field.")

v2 (this file): value the team's POSITIONAL UNIT, not the individual move.
  * quality(player) = POSITION_MAX[position] * usage_share (prior-season production),
    rookies -> draft-capital curve on draft_number.
  * Within each unit (QB/RB/WR/TE/DL/LB/DB/ST), players are ranked by quality and
    only the top of the depth chart counts, with decaying weights (DEPTH_W).
    A QB unit counts ONLY the projected starter -> signing a backup behind an
    entrenched starter adds ~nothing; a genuine upgrade to the starter counts fully.
  * Incumbents (already on the team last season) win quality ties, so a newcomer
    only displaces the starter if he is genuinely better.
  * Offseason delta = unit value (this season's roster) - unit value (last season's roster).

Usage:
    python -m app.scripts.preview_roster_changes 2025 2026
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.database import async_session
from app.ingestion.compute_injury_adjustments import POSITION_GROUP, POSITION_MAX, _agg, _shares

# How much of a player's quality counts at each depth rank within a unit.
# QB = 1 -> only the projected starter matters.
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


async def preview(from_year: int, to_year: int) -> None:
    async with async_session() as db:
        pmap = {
            r[0]: (r[1], r[2])
            for r in (await db.execute(text("SELECT id, position, team_id FROM nfl.players"))).all()
        }
        g2p = {
            r[0]: r[1]
            for r in (
                await db.execute(
                    text("SELECT nflverse_id, id FROM nfl.players WHERE nflverse_id IS NOT NULL")
                )
            ).all()
        }
        nm = {r[0]: r[1] for r in (await db.execute(text("SELECT id, name FROM nfl.players"))).all()}

        sid = await db.scalar(text("SELECT id FROM nfl.seasons WHERE year = :y"), {"y": from_year})
        agg = await _agg(db, sid, 99)
        sh = _shares(agg, pmap)
        pos_of = {pid: (p or None) for pid, (p, _t) in pmap.items()}

        # --- TALENT axis: blend of PRODUCTION and CONTRACT VALUE -----------------
        # Production: QB -> ANY/A; others -> POSITION_MAX * usage-share.
        # Contract:  apy_cap_pct (share of the cap) from nfl.contracts -> market
        #            consensus on both talent and role.
        # Both are converted to within-position percentiles and blended 55/45,
        # so quality spans [0,1] per position and no longer saturates at POSITION_MAX.
        import statistics

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
            pid: POSITION_MAX.get(pos_of.get(pid) or "", 0.6) * share
            for pid, share in sh.items()
        }
        for r in qrows:
            denom = (r[1] or 0) + (r[5] or 0)
            if denom:
                prod[r[0]] = ((r[2] or 0) + 20 * (r[3] or 0) - 45 * (r[4] or 0) - (r[6] or 0)) / denom

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
            "SELECT gsis_id, team_abbr, position, draft_number "
            "FROM nfl.rosters WHERE season = :y"
        )
        r_from = {
            r[0]: (r[1], r[2], r[3])
            for r in (await db.execute(_roster_sql, {"y": from_year})).all()
        }
        r_to = {
            r[0]: (r[1], r[2], r[3])
            for r in (await db.execute(_roster_sql, {"y": to_year})).all()
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
                lst.sort(key=lambda x: (-x[0], x[1]))   # best first; incumbents win ties
                for i, (q, _inc) in enumerate(lst[: len(w)]):
                    total += q * w[i]
            return total

        # incumbents = players already on the team in from_year (for tie-breaks)
        inc_to = {g for g, (tm, _, _) in r_from.items() if tm}

        changes = []
        for t in teams:
            before = unit_total(r_from, t, set())          # all were incumbents last year
            after = unit_total(r_to, t, inc_to)
            changes.append((t, after - before))
        changes.sort(key=lambda x: -x[1])

        print(f"=== OFFSET (unit-based, v2) NET VALUE {from_year}->{to_year} (best 8 / worst 8) ===")
        for t, d in changes[:8] + changes[-8:]:
            print(f"  {t:3} {d:+5.1f}")

        # per-team detail for a few headline cases
        def detail(team: str) -> None:
            before = unit_total(r_from, team, set())
            after = unit_total(r_to, team, inc_to)
            print(f"\n  [{team}] before {before:5.1f} -> after {after:5.1f}  ({after - before:+.1f})")
            for unit in UNIT_ORDER:
                b = {g: (p, d) for g, (tm, p, d) in r_from.items() if tm == team and POSITION_GROUP.get(p or "") == unit}
                a = {g: (p, d) for g, (tm, p, d) in r_to.items() if tm == team and POSITION_GROUP.get(p or "") == unit}
                if not a and not b:
                    continue
                w = DEPTH_W[unit]
                ab = sorted(((quality(g, p, d), nm.get(g2p.get(g)) or g) for g, (p, d) in a.items()), key=lambda x: -x[0])[: len(w)]
                print(f"    {unit:3} " + ", ".join(f"{n.split()[-1]} {q:.1f}" for q, n in ab))

        print("\n=== headline cases ===")
        for t in ("KC", "LV", "NYJ", "ATL", "CAR", "IND"):
            detail(t)


if __name__ == "__main__":
    a = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    b = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    asyncio.run(preview(a, b))
