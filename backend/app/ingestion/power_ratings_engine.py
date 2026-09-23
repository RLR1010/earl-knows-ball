"""Earl Power Ratings (v0) -- generic across NFL / NBA / MLB.

Same method as the NFL engine (ridge-regularized least squares on game margins,
Massey/SRS style, recency decay, shrinkage toward the prior season), but
parameterised per sport so NBA and MLB can run without touching the live NFL
engine (`compute_power_ratings.py`).

Per-sport differences (see SPORTS):
  * schema / table names
  * how a "week" is derived: NFL has a week column; NBA/MLB derive sequential
    weeks from the season's first played game.
  * market source for the vs-Market column: NFL/NBA use the point spread; MLB
    uses the MONEYLINE (its "spread" is the meaningless +/-1.5 run line).
  * injuries: NFL only (NBA/MLB injury_adj = 0 for now).

OUTPUT: immutable weekly snapshots in {schema}.power_ratings.

Run:
    PYTHONPATH=. ./venv/bin/python -m app.ingestion.power_ratings_engine nba 2025
    PYTHONPATH=. ./venv/bin/python -m app.ingestion.power_ratings_engine mlb 2026
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import sys

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("power_ratings")

DECAY = 0.90        # recency weight per week
PRIOR_C = 6.0       # shrinkage strength: lambda_prior = PRIOR_C / as_of_week
RIDGE_EPS = 1e-3    # tiny ridge keeps the system solvable

SPORTS = {
    "nfl": dict(
        schema="nfl", game_types=("REG",), week_mode="column",
        market="spread", hfa_prior=1.7, model_version="nfl-power-v0",
    ),
    "nba": dict(
        schema="nba", game_types=("REG",), week_mode="date",
        market="spread", hfa_prior=2.5, model_version="nba-power-v0",
    ),
    "mlb": dict(
        schema="mlb", game_types=("R",), week_mode="date",
        market="moneyline", hfa_prior=0.2, model_version="mlb-power-v0",
        ml_sigma=3.0,
    ),
}

_DDL = """
CREATE TABLE IF NOT EXISTS {s}.power_ratings (
    season        integer      NOT NULL,
    week          integer      NOT NULL,
    as_of_date    date,
    team_id       integer      NOT NULL,
    team_abbr     text,
    rating        double precision,
    rank          integer,
    prev_rank     integer,
    rating_delta  double precision,
    rank_delta    integer,
    sos           double precision,
    games_played  integer,
    wins          integer,
    losses        integer,
    ties          integer,
    prior_rating  double precision,
    injury_adj    double precision DEFAULT 0,
    market_rating double precision,
    market_delta  double precision,
    components    jsonb,
    model_version text,
    generated_at  timestamptz  DEFAULT now(),
    PRIMARY KEY (season, week, team_id)
);
"""


def solve_ratings(games, n_teams, prior, lam_prior, hfa_prior):
    """Ridge least squares. games: list of (home_idx, away_idx, margin, week).
    prior: array(n_teams) anchor; lam_prior: shrinkage weight; hfa_prior: HFA anchor.
    Returns (ratings array, hfa)."""
    if not games:
        return np.zeros(n_teams), 0.0
    m = len(games)
    A = np.zeros((m, n_teams + 1))          # last col = HFA intercept
    y = np.zeros(m)
    w = np.zeros(m)
    latest_week = max(g[3] for g in games)
    for i, (h, a, margin, wk) in enumerate(games):
        A[i, h] = 1.0
        A[i, a] = -1.0
        A[i, n_teams] = 1.0
        y[i] = margin
        w[i] = DECAY ** (latest_week - wk)
    W = np.diag(w)

    # Penalty: shrink ratings toward prior (intercept shrunk toward HFA prior).
    pen = np.zeros((n_teams + 1, n_teams + 1))
    pen[:n_teams, :n_teams] = np.eye(n_teams) * lam_prior
    pen[n_teams, n_teams] = 2.0             # shrink fitted HFA toward its anchor
    reg = np.zeros(n_teams + 1)
    reg[:n_teams] = lam_prior * prior
    reg[n_teams] = 2.0 * hfa_prior

    AtA = A.T @ W @ A + pen
    Aty = A.T @ W @ y + reg
    beta = np.linalg.solve(AtA + RIDGE_EPS * np.eye(n_teams + 1), Aty)
    return beta[:n_teams], float(beta[n_teams])


def compute_sos(games, ratings, n_teams):
    """Average (opponent rating + HFA if the opponent was home) per team."""
    tot = np.zeros(n_teams)
    cnt = np.zeros(n_teams)
    for (h, a, _m, _wk) in games:
        tot[h] += ratings[a]            # home team faced away opponent
        cnt[h] += 1
        tot[a] += ratings[h]            # away team faced home opponent
        cnt[a] += 1
    return np.divide(tot, cnt, out=np.zeros(n_teams), where=cnt > 0)


def _probit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return statistics.NormalDist().inv_cdf(p)


async def _played_games(db, cfg, year):
    s = cfg["schema"]
    sid = await db.scalar(text(f"SELECT id FROM {s}.seasons WHERE year = :y"), {"y": year})
    if sid is None:
        return None, []
    gts = "', '".join(cfg["game_types"])
    wk_col = "g.week" if cfg["week_mode"] == "column" else "NULL"
    rows = (
        await db.execute(
            text(
                f"""
                SELECT g.id, g.date, g.home_team_id, g.away_team_id, g.home_score, g.away_score,
                       {wk_col} AS week
                FROM {s}.games g
                WHERE g.season_id = :sid AND g.status = 'FINAL'
                  AND g.game_type IN ('{gts}')
                  AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
                ORDER BY g.date
                """
            ),
            {"sid": sid},
        )
    ).all()
    if not rows:
        return sid, []
    if cfg["week_mode"] == "column":
        games = [(int(r[6]), r[2], r[3], r[4], r[5], r[1]) for r in rows]
    else:
        first = min(r[1] for r in rows)
        games = [(((r[1] - first).days // 7) + 1, r[2], r[3], r[4], r[5], r[1]) for r in rows]
    return sid, games


async def _market_games(db, cfg, year):
    s = cfg["schema"]
    wk = "g.week" if cfg["week_mode"] == "column" else "NULL"
    if cfg["market"] == "moneyline":
        rows = (
            await db.execute(
                text(
                    f"SELECT {wk}, g.home_team_id, g.away_team_id, "
                    f"       b.closing_home_implied_probability, b.closing_away_implied_probability, g.date "
                    f"FROM {s}.games g JOIN {s}.betting_lines_consolidated b ON b.game_id = g.id "
                    f"WHERE g.season_id = (SELECT id FROM {s}.seasons WHERE year = :y) "
                    f"  AND b.closing_home_implied_probability IS NOT NULL "
                    f"  AND b.closing_away_implied_probability IS NOT NULL"
                ),
                {"y": year},
            )
        ).all()
        out = []
        for r in rows:
            ph, pa = float(r[3]), float(r[4])
            tot = ph + pa
            if tot <= 0:
                continue
            p_home = ph / tot                      # de-vig
            out.append((r[0], r[1], r[2], _probit(p_home) * cfg.get("ml_sigma", 3.0), r[5]))
        return out
    rows = (
        await db.execute(
            text(
                f"SELECT {wk}, g.home_team_id, g.away_team_id, b.closing_spread, g.date "
                f"FROM {s}.games g JOIN {s}.betting_lines_consolidated b ON b.game_id = g.id "
                f"WHERE g.season_id = (SELECT id FROM {s}.seasons WHERE year = :y) "
                f"  AND b.closing_spread IS NOT NULL"
            ),
            {"y": year},
        )
    ).all()
    return [(r[0], r[1], r[2], -float(r[3]), r[4]) for r in rows]   # home-perspective margin


async def main(sport: str, year: int | None = None) -> None:
    cfg = SPORTS[sport]
    s = cfg["schema"]
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            await db.execute(text(_DDL.format(s=s)))
            if year is None:
                year = await db.scalar(text(f"SELECT max(season) FROM {s}.power_ratings")) or 2026

            sid, games = await _played_games(db, cfg, year)
            if not games:
                logger.warning("%s %s: no played games; nothing to do", sport, year)
                return
            first = min(g[5] for g in games)

            market_rows = await _market_games(db, cfg, year)
            # derive week for market rows (date mode)
            if cfg["week_mode"] == "date":
                market_rows = [
                    (((r[4] - first).days // 7) + 1 if r[4] is not None else None, r[1], r[2], r[3])
                    for r in market_rows
                ]
            market_rows = [r for r in market_rows if r[0] is not None]

            teams = (
                await db.execute(
                    text(
                        f"SELECT t.id, t.abbreviation FROM {s}.teams t "
                        f"WHERE EXISTS (SELECT 1 FROM {s}.games g JOIN {s}.seasons se ON se.id=g.season_id "
                        f"  WHERE se.year = :y AND (g.home_team_id = t.id OR g.away_team_id = t.id)) "
                        f"ORDER BY t.id"
                    ),
                    {"y": year},
                )
            ).all()
            abbr = {r[0]: r[1] for r in teams}
            team_ids = [r[0] for r in teams]
            tidx = {t: i for i, t in enumerate(team_ids)}
            n_teams = len(team_ids)

            # prior from previous season's FINAL snapshot
            prior = np.zeros(n_teams)
            prows = (
                await db.execute(
                    text(
                        f"""
                        SELECT team_id, rating FROM {s}.power_ratings
                        WHERE season = :y AND week = (
                            SELECT max(week) FROM {s}.power_ratings WHERE season = :y)
                        """
                    ),
                    {"y": year - 1},
                )
            ).all()
            for tid, rating in prows:
                if tid in tidx and rating is not None:
                    prior[tidx[tid]] = float(rating)
            if prows:
                logger.info("%s %s: carried prior for %d teams from %s", sport, year, len(prows), year - 1)

            # Offseason acquisitions/losses (roster turnover vs the prior season).
            # Added to the season's prior anchor, so it acts strongest in WEEK 1 and
            # fades through the same shrinkage as more games are played. Table is
            # optional (NFL has no {s}.roster_changes yet) -> guarded read.
            offrows = []
            try:
                offrows = (
                    await db.execute(
                        text(f"SELECT team_id, net_pts FROM {s}.roster_changes WHERE season = :y"),
                        {"y": year},
                    )
                ).all()
            except Exception:  # table absent for this sport -> no offseason term
                offrows = []
            n_off = 0
            for tid, npts in offrows:
                if tid in tidx and npts is not None:
                    prior[tidx[tid]] += float(npts)
                    n_off += 1
            if n_off:
                logger.info(
                    "%s %s: applied offseason roster-change term to prior for %d teams",
                    sport, year, n_off,
                )

            max_wk = max(g[0] for g in games)
            prev: dict = {}
            for w in range(1, max_wk + 1):
                gs = [(tidx[h], tidx[a], hm - am, wk)
                      for (wk, h, a, hm, am, _d) in games if wk <= w
                      if h in tidx and a in tidx]
                if not gs:
                    continue
                lam_prior = PRIOR_C / max(w, 1)
                R, hfa = solve_ratings(gs, n_teams, prior, lam_prior, cfg["hfa_prior"])
                Rc = R - R.mean()
                sos = compute_sos(gs, Rc, n_teams)

                # injury adjustment: dock rating units for unavailable contributors
                adj_rows = (
                    await db.execute(
                        text(
                            f"SELECT team_id, adj, players FROM {s}.injury_adjustments "
                            f"WHERE season = :y AND week = :w"
                        ),
                        {"y": year, "w": w},
                    )
                ).all()
                inj_by_team = {r[0]: (float(r[1] or 0.0), r[2]) for r in adj_rows}
                inj_vec = np.array([inj_by_team.get(t, (0.0, None))[0] for t in team_ids])
                Rc = Rc - inj_vec

                # market rating (comparison only -- never an input to Rc)
                mgs = [(tidx[h], tidx[a], mg, wk)
                       for (wk, h, a, mg) in market_rows if wk <= w
                       if h in tidx and a in tidx]
                Rm = np.zeros(n_teams)
                if mgs:
                    Rm_raw, _ = solve_ratings(mgs, n_teams, np.zeros(n_teams), lam_prior, 0.0)
                    sd_r = float(Rc.std()) or 1.0
                    sd_m = float(Rm_raw.std()) or 1.0
                    Rm = Rm_raw * (sd_r / sd_m) + float(Rc.mean())
                mkt_vec = Rc - Rm

                order = sorted(range(n_teams), key=lambda i: -Rc[i])
                rank_of = {team_ids[i]: r + 1 for r, i in enumerate(order)}

                date_row = max(g[5] for g in games if g[0] <= w)

                # win/loss/tie record through week w
                rec: dict[int, tuple[int, int, int]] = {}
                for (gwk, h, a, hm, am, _d) in games:
                    if gwk > w:
                        continue
                    hw, hl, ht = rec.get(h, (0, 0, 0))
                    aw, al, at = rec.get(a, (0, 0, 0))
                    if hm > am:
                        hw += 1; al += 1
                    elif am > hm:
                        aw += 1; hl += 1
                    else:
                        ht += 1; at += 1
                    rec[h] = (hw, hl, ht)
                    rec[a] = (aw, al, at)

                payload = []
                for i, tid in enumerate(team_ids):
                    inj_detail = inj_by_team.get(tid, (0.0, None))[1]
                    if isinstance(inj_detail, (list, dict)):
                        inj_list = inj_detail
                    else:
                        try:
                            inj_list = json.loads(inj_detail) if inj_detail else []
                        except Exception:
                            inj_list = []
                    comps = {
                        "sos": round(float(sos[i]), 3),
                        "prior_rating": round(float(prior[i]), 3),
                        "hfa": round(hfa, 3),
                        "injury_adj": round(float(inj_vec[i]), 2),
                        "market_rating": round(float(Rm[i]), 2),
                        "market_delta": round(float(mkt_vec[i]), 2),
                        "injuries": inj_list,
                    }
                    p = prev.get(tid)
                    payload.append({
                        "season": year, "week": w, "as_of_date": date_row, "team_id": tid,
                        "team_abbr": abbr.get(tid), "rating": round(float(Rc[i]), 3),
                        "rank": rank_of[tid], "prev_rank": (p[0] if p else None),
                        "rating_delta": (round(float(Rc[i]) - float(p[1]), 3) if p else None),
                        "rank_delta": (rank_of[tid] - p[0] if p else None),
                        "sos": round(float(sos[i]), 3),
                        "games_played": int(len([1 for g in games if g[0] <= w and (g[1] == tid or g[2] == tid)])),
                        "injury_adj": round(float(inj_vec[i]), 2),
                        "market_rating": round(float(Rm[i]), 2),
                        "market_delta": round(float(mkt_vec[i]), 2),
                        "wins": rec.get(tid, (0, 0, 0))[0],
                        "losses": rec.get(tid, (0, 0, 0))[1],
                        "ties": rec.get(tid, (0, 0, 0))[2],
                        "prior_rating": round(float(prior[i]), 3),
                        "components": json.dumps(comps), "model_version": cfg["model_version"],
                    })
                prev = {tid: (rank_of[tid], Rc[i]) for i, tid in enumerate(team_ids)}

                # full replace for the week so teams no longer rated cannot linger
                await db.execute(
                    text(f"DELETE FROM {s}.power_ratings WHERE season = :y AND week = :w"),
                    {"y": year, "w": w},
                )

                for row in payload:
                    await db.execute(
                        text(
                            f"""
                            INSERT INTO {s}.power_ratings
                              (season, week, as_of_date, team_id, team_abbr, rating, rank, prev_rank,
                               rating_delta, rank_delta, sos, games_played, prior_rating,
                               wins, losses, ties, injury_adj, market_rating, market_delta,
                               components, model_version, generated_at)
                            VALUES (:season, :week, :as_of_date, :team_id, :team_abbr, :rating, :rank,
                                    :prev_rank, :rating_delta, :rank_delta, :sos, :games_played,
                                    :prior_rating, :wins, :losses, :ties, :injury_adj, :market_rating,
                                    :market_delta, CAST(:components AS jsonb), :model_version, now())
                            ON CONFLICT (season, week, team_id) DO UPDATE SET
                              as_of_date=EXCLUDED.as_of_date, rating=EXCLUDED.rating, rank=EXCLUDED.rank,
                              prev_rank=EXCLUDED.prev_rank, rating_delta=EXCLUDED.rating_delta,
                              rank_delta=EXCLUDED.rank_delta, sos=EXCLUDED.sos,
                              games_played=EXCLUDED.games_played, prior_rating=EXCLUDED.prior_rating,
                              wins=EXCLUDED.wins, losses=EXCLUDED.losses, ties=EXCLUDED.ties,
                              injury_adj=EXCLUDED.injury_adj, market_rating=EXCLUDED.market_rating,
                              market_delta=EXCLUDED.market_delta, components=EXCLUDED.components,
                              model_version=EXCLUDED.model_version, generated_at=now()
                            """
                        ),
                        row,
                    )
                await db.commit()
                logger.info("%s %s wk %d: top=%s %.2f (hfa %.2f)", sport, year, w,
                            abbr.get(team_ids[order[0]]), Rc[order[0]], hfa)
            logger.info("%s %s: wrote %d weeks", sport, year, max_wk)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    _sport = sys.argv[1] if len(sys.argv) > 1 else "mlb"
    _year = int(sys.argv[2]) if len(sys.argv) > 2 else None
    asyncio.run(main(_sport, _year))
