"""Earl Injury Adjustments -- MLB + NBA.

Mirrors the NFL model (`compute_injury_adjustments.py`): value = POSITION_MAX *
usage-share-within-position-group, summed over unavailable players, capped.

Sources differ per sport:
  * MLB: `mlb.injuries` is a CURRENT snapshot only (no week history) -> the dock
    is applied to the latest power-ratings week. Production = season PA (batters,
    from player_batting_rolling_stats) / IP (pitchers, from pitching_stats, split
    SP vs RP by games_started).
  * NBA: `nba.active_players` has per-game PLAYED / DNP_CD / INACTIVE status ->
    the dock is computed for EVERY week. Production = minutes through that week
    (nba.player_game_stats), so a player missing a week does not lose his value.

Writes {schema}.injury_adjustments (same columns as nfl.injury_adjustments).
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("injury_adj")

# points docked if a full-time starter at this position is unavailable
MLB_POS_MAX = {
    "SP": 0.30, "P": 0.25, "RP": 0.14,
    "C": 0.13, "1B": 0.13, "2B": 0.13, "3B": 0.13, "SS": 0.17,
    "LF": 0.13, "CF": 0.13, "RF": 0.13, "OF": 0.13, "DH": 0.10,
}
MLB_POS_GROUP = {
    "SP": "SP", "P": "SP", "RP": "RP",
    "C": "C", "1B": "1B", "2B": "2B", "3B": "3B", "SS": "SS",
    "LF": "OF", "CF": "OF", "RF": "OF", "OF": "OF", "DH": "DH",
}
NBA_POS_MAX = {"PG": 1.8, "SG": 1.8, "SF": 1.8, "PF": 1.8, "C": 1.8, "G": 1.5, "F": 1.5}
NBA_POS_GROUP = {"PG": "PG", "SG": "SG", "SF": "SF", "PF": "PF", "C": "C", "G": "G", "F": "F"}

SPORTS = {
    "mlb": dict(
        schema="mlb", source="injuries", cap=0.8, default_pos_max=0.10, share_pow=1.0,
        pos_max=MLB_POS_MAX, pos_group=MLB_POS_GROUP, model_version="inj-mlb-v0",
    ),
    "nba": dict(
        schema="nba", source="inactive", cap=4.0, default_pos_max=1.3, share_pow=1.6,
        pos_max=NBA_POS_MAX, pos_group=NBA_POS_GROUP, model_version="inj-nba-v0",
    ),
}


def _saturate(raw: float, cap: float) -> float:
    """Smooth diminishing returns instead of a hard cap (can never exceed cap)."""
    import math

    if raw <= 0:
        return 0.0
    return round(cap * (1.0 - math.exp(-raw / cap)), 2)

DDL = """
CREATE TABLE IF NOT EXISTS {s}.injury_adjustments (
    season      integer NOT NULL,
    week        integer NOT NULL,
    team_id     integer NOT NULL,
    team_abbr   varchar(8),
    adj         double precision NOT NULL DEFAULT 0,
    n_players   integer NOT NULL DEFAULT 0,
    players     jsonb,
    model_version varchar(32),
    generated_at timestamptz DEFAULT now(),
    PRIMARY KEY (season, week, team_id)
)
"""


def _shares(metric: dict[int, float], pmap: dict[int, tuple], group_of, team_of) -> dict[int, float]:
    """player_id -> value share within his (team, position-group), 0..1."""
    gmax: dict[tuple, float] = {}
    for pid, m in metric.items():
        g = group_of(pmap.get(pid, (None,))[0])
        if not g:
            continue
        k = (team_of(pid), g)
        if m > gmax.get(k, 0.0):
            gmax[k] = m
    out = {}
    for pid, m in metric.items():
        g = group_of(pmap.get(pid, (None,))[0])
        if not g:
            continue
        mx = gmax.get((team_of(pid), g), 0.0)
        out[pid] = min(1.0, m / mx) if mx > 0 else 0.0
    return out


def _group_of_factory(cfg):
    pg = cfg["pos_group"]
    return lambda pos: pg.get((pos or "").upper().strip())


async def _write(db, s, rows, model_version, season):
    # full replace for the season: rows for teams that no longer qualify must not linger
    await db.execute(
        text(f"DELETE FROM {s}.injury_adjustments WHERE season = :y"), {"y": season}
    )
    if not rows:
        await db.commit()
        return 0
    await db.execute(
        text(
            f"""
            INSERT INTO {s}.injury_adjustments
              (season, week, team_id, team_abbr, adj, n_players, players, model_version)
            VALUES (:season,:week,:team_id,:team_abbr,:adj,:n_players,
                    CAST(:players AS jsonb),:model_version)
            ON CONFLICT (season, week, team_id) DO UPDATE SET
              team_abbr=EXCLUDED.team_abbr, adj=EXCLUDED.adj, n_players=EXCLUDED.n_players,
              players=EXCLUDED.players, model_version=EXCLUDED.model_version, generated_at=now()
            """
        ),
        rows,
    )
    await db.commit()
    return len(rows)


async def _weeks(db, s, season):
    rows = (
        await db.execute(
            text(f"SELECT week, as_of_date FROM {s}.power_ratings WHERE season=:y GROUP BY 1,2 ORDER BY 1"),
            {"y": season},
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


def _week_for_date(weeks, d):
    d = _as_date(d)
    for w, asof in weeks:
        if asof is not None and d <= _as_date(asof):
            return w
    return weeks[-1][0] if weeks else None


def _as_date(x):
    """Coerce a datetime/date to a date (no-op for date)."""
    return x.date() if hasattr(x, "date") and not isinstance(x, type(None)) and hasattr(x, "hour") else x


async def compute_mlb(db):
    cfg = SPORTS["mlb"]
    s = "mlb"
    season = await db.scalar(text(f"SELECT max(season) FROM {s}.power_ratings"))
    weeks = await _weeks(db, s, season)
    week = weeks[-1][0]
    abbr = {r[0]: r[1] for r in (await db.execute(text("SELECT id, abbreviation FROM mlb.teams"))).all()}
    pmap = {r[0]: (r[1],) for r in (await db.execute(text("SELECT id, position FROM mlb.players"))).all()}
    names = {r[0]: r[1] for r in (await db.execute(text("SELECT id, name FROM mlb.players"))).all()}
    group_of = _group_of_factory(cfg)

    # batting production = latest ytd_pa in the season
    bat = {
        r[0]: float(r[1] or 0)
        for r in (
            await db.execute(
                text(
                    """
                    SELECT DISTINCT ON (player_id) player_id, ytd_pa
                    FROM mlb.player_batting_rolling_stats
                    WHERE season_id = (SELECT id FROM mlb.seasons WHERE year=:y)
                    ORDER BY player_id, game_date DESC, game_n DESC
                    """
                ),
                {"y": season},
            )
        ).all()
    }
    # pitching production = season IP; SP if mostly starts
    pit, pit_pos = {}, {}
    for r in (
        await db.execute(
            text(
                """
                SELECT p.player_id, p.team_id, p.innings_pitched, p.games_started, p.games_played
                FROM mlb.pitching_stats p
                WHERE p.season_id = (SELECT id FROM mlb.seasons WHERE year=:y)
                """
            ),
            {"y": season},
        )
    ).all():
        pit[r[0]] = float(r[2] or 0)
        pit_pos[r[0]] = "SP" if (r[3] or 0) >= 0.5 * max(1, (r[4] or 0)) else "RP"

    team_of = {}
    for pid in bat:
        team_of[pid] = None
    bat_team = {
        r[0]: r[1]
        for r in (
            await db.execute(
                text(
                    """
                    SELECT DISTINCT ON (player_id) player_id, team_id
                    FROM mlb.player_batting_rolling_stats
                    WHERE season_id=(SELECT id FROM mlb.seasons WHERE year=:y)
                    ORDER BY player_id, game_date DESC, game_n DESC
                    """
                ),
                {"y": season},
            )
        ).all()
    }
    pit_team = {}
    for r in (await db.execute(text("SELECT player_id, team_id FROM mlb.pitching_stats WHERE season_id=(SELECT id FROM mlb.seasons WHERE year=:y)"), {"y": season})).all():
        pit_team[r[0]] = r[1]
    for pid in bat:
        team_of[pid] = bat_team.get(pid)
    for pid in pit:
        team_of[pid] = pit_team.get(pid)
    # pitching players carry the SP/RP pseudo-position
    pmap2 = dict(pmap)
    for pid, p in pit_pos.items():
        pmap2[pid] = (p,)
    metric = {**bat, **pit}

    inj = (
        await db.execute(
            text(
                """
                SELECT player_id, team_id, status FROM mlb.injuries
                WHERE is_active = true
                """
            )
        )
    ).all()
    shares = _shares(metric, pmap2, group_of, lambda p: team_of.get(p))
    by_team: dict[int, list] = {}
    for pid, tid, status in inj:
        share = shares.get(pid, 0.0)
        if share <= 0.05:
            continue
        pos = (pmap2.get(pid, (None,))[0] or "")
        posmax = cfg["pos_max"].get(pos.upper(), cfg["default_pos_max"])
        val = round(posmax * (share ** cfg["share_pow"]), 3)
        if val <= 0:
            continue
        g = group_of(pos)
        by_team.setdefault(tid, []).append(
            {"player_id": pid, "name": names.get(pid), "position": pos, "group": g,
             "report_status": status, "share": round(share, 2), "value": round(val, 2)}
        )
    rows = []
    for tid, pl in by_team.items():
        pl.sort(key=lambda x: -x["value"])
        raw = sum(p["value"] for p in pl)
        rows.append({
            "season": season, "week": week, "team_id": tid,
            "team_abbr": abbr.get(tid), "adj": _saturate(raw, cfg["cap"]),
            "n_players": len(pl), "players": json.dumps(pl[:12]),
            "model_version": cfg["model_version"],
        })
    n = await _write(db, s, rows, cfg["model_version"], season)
    logger.info("mlb %s wk%d: wrote %d team adjustments", season, week, n)
    return n


async def compute_nba(db):
    cfg = SPORTS["nba"]
    s = "nba"
    season = await db.scalar(text(f"SELECT max(season) FROM {s}.power_ratings"))
    weeks = await _weeks(db, s, season)
    abbr = {r[0]: r[1] for r in (await db.execute(text("SELECT id, abbreviation FROM nba.teams"))).all()}
    pmap = {r[0]: (r[1],) for r in (await db.execute(text("SELECT id, position FROM nba.players"))).all()}
    names = {r[0]: r[1] for r in (await db.execute(text("SELECT id, name FROM nba.players"))).all()}
    group_of = _group_of_factory(cfg)
    sid = await db.scalar(text("SELECT id FROM nba.seasons WHERE year=:y"), {"y": season})

    # per-game: date + player minutes (PLAYED rows only)
    mins = (
        await db.execute(
            text(
                """
                SELECT pgs.player_id, pgs.team_id, g.date, pgs.minutes
                FROM nba.player_game_stats pgs
                JOIN nba.games g ON g.id = pgs.game_id
                WHERE g.season_id = :sid AND g.game_type = 'REG'
                """
            ),
            {"sid": sid},
        )
    ).all()
    inact = (
        await db.execute(
            text(
                """
                SELECT ap.player_id, ap.team_id, g.date
                FROM nba.active_players ap
                JOIN nba.games g ON g.id = ap.game_id
                WHERE g.season_id = :sid AND g.game_type = 'REG' AND ap.status = 'INACTIVE'
                """
            ),
            {"sid": sid},
        )
    ).all()

    rows = []
    # dedupe inactives to one row per (week, player)
    seen = set()
    inact_u = []
    for pid, tid, d in inact:
        wk = _week_for_date(weeks, d)
        key = (wk, pid)
        if key in seen:
            continue
        seen.add(key)
        inact_u.append((pid, tid, wk))

    for week, asof in weeks:
        # season-to-date minutes through this week
        metric: dict[int, float] = {}
        team_of: dict[int, int] = {}
        for pid, tid, d, m in mins:
            if d is not None and asof is not None and _as_date(d) <= _as_date(asof):
                metric[pid] = metric.get(pid, 0.0) + float(m or 0)
                team_of[pid] = tid
        shares = _shares(metric, pmap, group_of, lambda p: team_of.get(p))

        by_team: dict[int, list] = {}
        for pid, tid, wk in inact_u:
            if wk != week:
                continue
            share = shares.get(pid, 0.0)
            if share <= 0.08:
                continue
            pos = (pmap.get(pid, (None,))[0] or "")
            posmax = cfg["pos_max"].get(pos.upper(), cfg["default_pos_max"])
            val = round(posmax * (share ** cfg["share_pow"]), 3)
            if val <= 0:
                continue
            by_team.setdefault(tid, []).append(
                {"player_id": pid, "name": names.get(pid), "position": pos,
                 "report_status": "Inactive", "share": round(share, 2), "value": round(val, 2)}
            )
        for tid, pl in by_team.items():
            pl.sort(key=lambda x: -x["value"])
            raw = sum(p["value"] for p in pl)
            rows.append({
                "season": season, "week": week, "team_id": tid,
                "team_abbr": abbr.get(tid),
                "adj": _saturate(raw, cfg["cap"]),
                "n_players": len(pl), "players": json.dumps(pl[:12]),
                "model_version": cfg["model_version"],
            })
    n = await _write(db, s, rows, cfg["model_version"], season)
    logger.info("nba %s: wrote %d team-week adjustments", season, n)
    return n


async def main(sport: str):
    cfg = SPORTS[sport]
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            await db.execute(text(DDL.format(s=sport)))
            await db.commit()
            if cfg["source"] == "injuries":
                await compute_mlb(db)
            else:
                await compute_nba(db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import sys

    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "mlb"))
