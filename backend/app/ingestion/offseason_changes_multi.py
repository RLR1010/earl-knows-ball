"""Earl Offseason Roster-Change Adjustments -- MLB + NBA.

Mirrors the NFL offseason roster-turnover model (`app/scripts/preview_roster_changes.py`,
"v3 gentle"): for each team, the NET value of players GAINED minus LOST between the
prior season's roster and the current season's roster.  The result is stored as
`net_pts` and applied ONLY to a season's WEEK-1 prior rating (a team's roster is
frozen from that point on, so the adjustment fades as real games accrue -- exactly
the NFL behaviour).

The NFL model values players with production (ANY/A, usage share) blended with
CONTRACT value.  NBA/MLB have no contract feed here, so we value players purely by
PRIOR-SEASON PRODUCTION:

  NBA : V = (prior minutes / 1000) * quality(pts per 36)
  MLB : V = (prior PA / 600)       * quality(OPS)      [batters]
        V = (prior IP / 180)       * quality(ERA)      [pitchers]

`net_pts = clip(K * (sum(gained V) - sum(lost V)), -CAP, +CAP)`, per sport.

Writes {schema}.roster_changes (one row per season/team).  Additive new table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("offseason")

DDL = """
CREATE TABLE IF NOT EXISTS {s}.roster_changes (
    season          integer      NOT NULL,
    team_id         integer      NOT NULL,
    team_abbr       text,
    net_pts         double precision,
    detail          jsonb,
    model_version   text,
    updated_at      timestamptz  DEFAULT now(),
    PRIMARY KEY (season, team_id)
);
"""

# per-sport calibration (K = points-per-value-unit, CAP = max |net_pts|)
CONF = {
    "nba": {"k": 0.20, "cap": 1.2, "model": "off-nba-v0", "default_gain": 0.0},
    "mlb": {"k": 0.08, "cap": 0.5, "model": "off-mlb-v0", "default_gain": 0.0},
}


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


# ---------------------------------------------------------------- value axes
def _nba_value(minutes: float, points: float) -> float:
    if not minutes or minutes <= 0:
        return 0.0
    per36 = (points or 0) * 36.0 / minutes
    q = _clip(1.0 + (per36 - 16.0) / 20.0, 0.5, 2.0)
    return (minutes / 1000.0) * q


def _mlb_batter_value(pa: float, ops: float) -> float:
    if not pa or pa <= 0:
        return 0.0
    q = _clip(1.0 + ((ops or 0.72) - 0.720) / 0.300, 0.5, 2.0)
    return (pa / 600.0) * q


def _mlb_pitcher_value(ip: float, era: float) -> float:
    if not ip or ip <= 0:
        return 0.0
    q = _clip(1.0 + (4.00 - (era if era is not None else 4.0)) / 2.0, 0.5, 2.0)
    return (ip / 180.0) * q


# ---------------------------------------------------------------- helpers
async def _team_abbrs(db, s: str) -> dict[int, str]:
    rows = (await db.execute(text(f"SELECT id, abbreviation FROM {s}.teams"))).all()
    return {r[0]: r[1] for r in rows}


async def _write(db, s: str, rows: list[dict], model: str, season: int) -> None:
    await db.execute(text(DDL.format(s=s)))
    # full replace for the season so dropped teams cannot linger
    await db.execute(
        text(f"DELETE FROM {s}.roster_changes WHERE season = :y"), {"y": season}
    )
    for r in rows:
        await db.execute(
            text(
                f"INSERT INTO {s}.roster_changes "
                f"(season, team_id, team_abbr, net_pts, detail, model_version) "
                f"VALUES (:y, :tid, :ab, :np, CAST(:d AS jsonb), :mv)"
            ),
            {
                "y": season,
                "tid": r["team_id"],
                "ab": r["abbr"],
                "np": r["net_pts"],
                "d": json.dumps(r["detail"]),
                "mv": model,
            },
        )


# ---------------------------------------------------------------- NBA
async def compute_nba(season: int) -> list[dict]:
    conf = CONF["nba"]
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        abbrs = await _team_abbrs(db, "nba")
        cur = (
            await db.execute(
                text(
                    """
                    SELECT ap.team_id, ap.player_id
                    FROM nba.active_players ap
                    JOIN nba.games g   ON g.id = ap.game_id
                    JOIN nba.seasons se ON se.id = g.season_id
                    WHERE se.year = :y AND ap.status = 'PLAYED'
                    GROUP BY 1, 2
                    """
                ),
                {"y": season},
            )
        ).all()
        prev = (
            await db.execute(
                text(
                    """
                    SELECT ap.team_id, ap.player_id
                    FROM nba.active_players ap
                    JOIN nba.games g   ON g.id = ap.game_id
                    JOIN nba.seasons se ON se.id = g.season_id
                    WHERE se.year = :y AND ap.status = 'PLAYED'
                    GROUP BY 1, 2
                    """
                ),
                {"y": season - 1},
            )
        ).all()
        prod = {
            r[0]: (float(r[1] or 0), float(r[2] or 0))
            for r in (
                await db.execute(
                    text(
                        """
                        SELECT pgs.player_id,
                               sum(CASE WHEN pgs.minutes ~ ':'
                                        THEN split_part(pgs.minutes, ':', 1)::double precision
                                             + split_part(pgs.minutes, ':', 2)::double precision / 60.0
                                        ELSE NULLIF(pgs.minutes, '')::double precision END) AS mins,
                               sum(pgs.points) AS pts
                        FROM nba.player_game_stats pgs
                        JOIN nba.games g   ON g.id = pgs.game_id
                        JOIN nba.seasons se ON se.id = g.season_id
                        WHERE se.year = :y
                        GROUP BY 1
                        """
                    ),
                    {"y": season - 1},
                )
            ).all()
        }
        names = {
            r[0]: r[1]
            for r in (await db.execute(text("SELECT id, name FROM nba.players"))).all()
        }

    def val(pid: int) -> float:
        if pid in prod:
            m, p = prod[pid]
            return _nba_value(m, p)
        return conf["default_gain"]  # newcomer with no prior NBA production

    cur_by_team: dict[int, set[int]] = {}
    for t, p in cur:
        cur_by_team.setdefault(t, set()).add(p)
    prev_by_team: dict[int, set[int]] = {}
    for t, p in prev:
        prev_by_team.setdefault(t, set()).add(p)

    raw = []
    for t, curset in cur_by_team.items():
        prevset = prev_by_team.get(t, set())
        gained = curset - prevset
        lost = prevset - curset
        grew = sum(val(p) for p in gained)
        shrank = sum(val(p) for p in lost)
        raw.append((t, gained, lost, grew - shrank))
    mean = (sum(x[3] for x in raw) / len(raw)) if raw else 0.0
    rows = []
    for t, gained, lost, delta in raw:
        net = _clip(conf["k"] * (delta - mean), -conf["cap"], conf["cap"])
        rows.append(
            {
                "team_id": t,
                "abbr": abbrs.get(t),
                "net_pts": round(net, 3),
                "detail": {
                    "gained": [
                        {"player_id": p, "name": names.get(p), "value": round(val(p), 3)}
                        for p in sorted(gained, key=val, reverse=True)[:12]
                    ],
                    "lost": [
                        {"player_id": p, "name": names.get(p), "value": round(val(p), 3)}
                        for p in sorted(lost, key=val, reverse=True)[:12]
                    ],
                    "raw_delta": round(delta, 3),
                },
            }
        )
    await engine.dispose()
    return rows


# ---------------------------------------------------------------- MLB
async def compute_mlb(season: int) -> list[dict]:
    conf = CONF["mlb"]
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        abbrs = await _team_abbrs(db, "mlb")
        cur = (
            await db.execute(
                text(
                    """
                    SELECT team_id, player_id FROM mlb.batting_stats bs
                    JOIN mlb.seasons se ON se.id = bs.season_id WHERE se.year = :y
                    UNION
                    SELECT team_id, player_id FROM mlb.pitching_stats ps
                    JOIN mlb.seasons se ON se.id = ps.season_id WHERE se.year = :y
                    """
                ),
                {"y": season},
            )
        ).all()
        prev = (
            await db.execute(
                text(
                    """
                    SELECT team_id, player_id FROM mlb.batting_stats bs
                    JOIN mlb.seasons se ON se.id = bs.season_id WHERE se.year = :y
                    UNION
                    SELECT team_id, player_id FROM mlb.pitching_stats ps
                    JOIN mlb.seasons se ON se.id = ps.season_id WHERE se.year = :y
                    """
                ),
                {"y": season - 1},
            )
        ).all()
        bat = {
            r[0]: (float(r[1] or 0), float(r[2] or 0))
            for r in (
                await db.execute(
                    text(
                        """
                        SELECT bs.player_id, bs.plate_appearances, bs.ops
                        FROM mlb.batting_stats bs
                        JOIN mlb.seasons se ON se.id = bs.season_id WHERE se.year = :y
                        """
                    ),
                    {"y": season - 1},
                )
            ).all()
        }
        pit = {
            r[0]: (float(r[1] or 0), (float(r[2]) if r[2] is not None else 4.0))
            for r in (
                await db.execute(
                    text(
                        """
                        SELECT ps.player_id, ps.innings_pitched, ps.era
                        FROM mlb.pitching_stats ps
                        JOIN mlb.seasons se ON se.id = ps.season_id WHERE se.year = :y
                        """
                    ),
                    {"y": season - 1},
                )
            ).all()
        }
        names = {
            r[0]: r[1]
            for r in (await db.execute(text("SELECT id, name FROM mlb.players"))).all()
        }

    def val(pid: int) -> float:
        vb = _mlb_batter_value(*bat[pid]) if pid in bat else 0.0
        vp = _mlb_pitcher_value(*pit[pid]) if pid in pit else 0.0
        v = max(vb, vp)
        if v <= 0:
            return conf["default_gain"]  # newcomer with no prior MLB production
        return v

    cur_by_team: dict[int, set[int]] = {}
    for t, p in cur:
        cur_by_team.setdefault(t, set()).add(p)
    prev_by_team: dict[int, set[int]] = {}
    for t, p in prev:
        prev_by_team.setdefault(t, set()).add(p)

    raw = []
    for t, curset in cur_by_team.items():
        prevset = prev_by_team.get(t, set())
        gained = curset - prevset
        lost = prevset - curset
        grew = sum(val(p) for p in gained)
        shrank = sum(val(p) for p in lost)
        raw.append((t, gained, lost, grew - shrank))
    mean = (sum(x[3] for x in raw) / len(raw)) if raw else 0.0
    rows = []
    for t, gained, lost, delta in raw:
        net = _clip(conf["k"] * (delta - mean), -conf["cap"], conf["cap"])
        rows.append(
            {
                "team_id": t,
                "abbr": abbrs.get(t),
                "net_pts": round(net, 3),
                "detail": {
                    "gained": [
                        {"player_id": p, "name": names.get(p), "value": round(val(p), 3)}
                        for p in sorted(gained, key=val, reverse=True)[:12]
                    ],
                    "lost": [
                        {"player_id": p, "name": names.get(p), "value": round(val(p), 3)}
                        for p in sorted(lost, key=val, reverse=True)[:12]
                    ],
                    "raw_delta": round(delta, 3),
                },
            }
        )
    await engine.dispose()
    return rows


# ---------------------------------------------------------------- main
async def main(sport: str, season: int | None = None) -> None:
    if sport not in CONF:
        raise SystemExit(f"unknown sport {sport!r}")
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    if season is None:
        async with Session() as db:
            season = await db.scalar(
                text(f"SELECT max(season) FROM {sport}.power_ratings")
            )
    if season is None or season < 2:
        raise SystemExit(f"no usable season for {sport}")

    rows = await compute_nba(season) if sport == "nba" else await compute_mlb(season)
    async with Session() as db:
        await _write(db, sport, rows, CONF[sport]["model"], season)
        await db.commit()
    await engine.dispose()

    rows.sort(key=lambda r: -r["net_pts"])
    nz = [r for r in rows if abs(r["net_pts"]) > 1e-9]
    logger.info(
        "%s %d: %d teams, %d with a non-zero adjustment (range %.2f .. %.2f)",
        sport.upper(),
        season,
        len(rows),
        len(nz),
        min((r["net_pts"] for r in rows), default=0.0),
        max((r["net_pts"] for r in rows), default=0.0),
    )
    print(f"\n== {sport.upper()} {season} offseason net_pts (top gains / losses) ==")
    for r in rows[:8]:
        print(f"  +{r['net_pts']:+.2f}  {r['abbr']}")
    for r in rows[-8:]:
        print(f"  {r['net_pts']:+.2f}  {r['abbr']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit("usage: python -m app.ingestion.offseason_changes_multi {nba|mlb} [season]")
    sp = args[0]
    se = int(args[1]) if len(args) > 1 else None
    asyncio.run(main(sp, se))
