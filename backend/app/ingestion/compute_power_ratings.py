"""
Earl Power Ratings (v0) -- points-denominated team strength. NFL first.

WHAT THIS IS
  A single number per team, denominated in POINTS on a neutral field:
      rating_home - rating_away + HFA  ~=  expected margin
  Because the rating is in points, it maps directly to a spread -> gambling-native.

METHOD
  Ridge-regularized least squares on game margins (Massey / SRS style):
    - home-field advantage fitted as an intercept
    - recency decay: recent games weigh more (DECAY ** weeks_ago)
    - shrinkage toward the PRIOR season's final ratings, strong early in the
      season and fading as real games accrue (PRIOR_C / as_of_week)

NO LOOK-AHEAD
  A rating for week W uses ONLY games with week <= W and status FINAL.

OUTPUT
  Immutable weekly snapshots in nfl.power_ratings, incl. rank, deltas vs the
  previous week, SOS, and a components JSON audit trail.

Run:  PYTHONPATH=. ./venv/bin/python -m app.ingestion.compute_power_ratings
"""
from __future__ import annotations

import asyncio
import json
import logging

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db_urls import ASYNC_DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("power_ratings")

MODEL_VERSION = "nfl-power-v0"
DECAY = 0.90        # recency weight per week
PRIOR_C = 6.0       # shrinkage strength: lambda_prior = PRIOR_C / as_of_week
RIDGE_EPS = 1e-3    # tiny ridge keeps the system solvable (esp. w/ few games)
HFA_PRIOR = 1.7     # league-average home-field advantage (pts)
HFA_PRIOR_W = 2.0   # shrink the fitted HFA toward HFA_PRIOR (small-sample guard)

DDL = """
CREATE TABLE IF NOT EXISTS nfl.power_ratings (
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


def solve_ratings(games, n_teams, prior, lam_prior):
    """Ridge least squares. games: list of (home_idx, away_idx, margin, week).
    prior: array(n_teams) anchor; lam_prior: shrinkage weight.
    Returns (ratings array, hfa, weights-per-team games count)."""
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

    # Penalty: shrink ratings toward prior (intercept unpenalized) + tiny ridge.
    P = np.zeros((n_teams + 1, n_teams + 1))
    P[:n_teams, :n_teams] = np.eye(n_teams) * (lam_prior + RIDGE_EPS)
    P[n_teams, n_teams] = HFA_PRIOR_W          # shrink HFA toward league prior
    target = np.concatenate([prior, [HFA_PRIOR]])

    AtW = A.T @ W
    lhs = AtW @ A + P
    rhs = AtW @ y + P @ target
    x = np.linalg.solve(lhs, rhs)
    return x[:n_teams], float(x[n_teams])


def compute_sos(games, ratings, n_teams):
    """Mean opponent rating per team (raw, uncentered)."""
    acc = np.zeros(n_teams)
    cnt = np.zeros(n_teams)
    for h, a, _m, _w in games:
        acc[h] += ratings[a]; cnt[h] += 1
        acc[a] += ratings[h]; cnt[a] += 1
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cnt > 0, acc / np.maximum(cnt, 1), 0.0)


async def fetch_played_games(db, year, max_week):
    rows = (await db.execute(text("""
        SELECT g.week, g.home_team_id, g.away_team_id, g.home_score, g.away_score
        FROM nfl.games g JOIN nfl.seasons s ON s.id = g.season_id
        WHERE s.year = :y AND g.game_type = 'REG' AND g.week <= :w
          AND g.status = 'FINAL' AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
        ORDER BY g.week, g.date
    """), {"y": year, "w": max_week})).all()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


async def fetch_market_games(db, year, max_week):
    """Market-implied margins from closing spreads of played games.
    Returns list of (week, home_id, away_id, market_margin_home).
    `closing_spread` is from the home team's perspective (negative = home favored),
    so the expected home margin is its negation."""
    rows = (await db.execute(text("""
        SELECT g.week, g.home_team_id, g.away_team_id, b.closing_spread
        FROM nfl.games g
        JOIN nfl.seasons s ON s.id = g.season_id
        JOIN nfl.betting_lines_consolidated b ON b.game_id = g.id
        WHERE s.year = :y AND g.game_type = 'REG' AND g.week <= :w
          AND b.closing_spread IS NOT NULL
        ORDER BY g.week, g.date
    """), {"y": year, "w": max_week})).all()
    return [(r[0], r[1], r[2], -float(r[3])) for r in rows]


async def season_final_ratings(db, year, n_teams, tidx):
    """Final ratings for a season, weak prior (p=0). Used as next season's anchor."""
    games = await fetch_played_games(db, year, 99)
    if not games:
        return np.zeros(n_teams)
    gs = [(tidx[h], tidx[a], hm - am, wk) for (wk, h, a, hm, am) in games]
    R, _ = solve_ratings(gs, n_teams, np.zeros(n_teams), 0.25)
    return R - R.mean()


async def main(target_year=None):
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        await db.execute(text(DDL))
        # additive columns for existing tables (DDL only creates fresh)
        for _col in ("wins integer", "losses integer", "ties integer",
                     "injury_adj double precision", "market_rating double precision",
                     "market_delta double precision"):
            await db.execute(text(
                f"ALTER TABLE nfl.power_ratings ADD COLUMN IF NOT EXISTS {_col}"))
        await db.commit()

        teams = (await db.execute(text(
            "SELECT id, abbreviation FROM nfl.teams ORDER BY id"))).all()
        team_ids = [t[0] for t in teams]
        abbr = {t[0]: t[1] for t in teams}
        n_teams = len(team_ids)
        tidx = {tid: i for i, tid in enumerate(team_ids)}

        years = [r[0] for r in (await db.execute(text(
            "SELECT DISTINCT s.year FROM nfl.seasons s JOIN nfl.games g ON g.season_id = s.id "
            "WHERE s.year IS NOT NULL ORDER BY s.year"))).all()]
        if target_year:
            # Prior for a season only needs the immediately preceding season's games.
            years = [y for y in years if y in (target_year - 1, target_year)]

        prior = np.zeros(n_teams)
        for year in years:
            all_games = await fetch_played_games(db, year, 99)
            if not all_games:
                continue
            market_all = await fetch_market_games(db, year, 99)
            max_wk = max(g[0] for g in all_games)

            # Offseason acquisitions/losses (roster turnover vs the prior season).
            # Added to this season's PRIOR anchor, so it is strongest at week 1 and
            # fades via PRIOR_C/week as real games accrue. Table optional -> guarded.
            try:
                off = (
                    await db.execute(
                        text("SELECT team_id, net_pts FROM nfl.roster_changes WHERE season = :y"),
                        {"y": year},
                    )
                ).all()
            except Exception:
                off = []
            n_off = 0
            for tid, npts in off:
                if tid in tidx and npts is not None:
                    prior[tidx[tid]] += float(npts)
                    n_off += 1
            if n_off:
                logger.info(
                    "nfl %s: applied offseason roster-change to prior (%d teams)", year, n_off
                )

            for w in range(1, max_wk + 1):
                gs = [(tidx[h], tidx[a], hm - am, wk)
                      for (wk, h, a, hm, am) in all_games if wk <= w]
                if not gs:
                    continue
                lam_prior = PRIOR_C / max(w, 1)
                R, hfa = solve_ratings(gs, n_teams, prior, lam_prior)
                Rc = R - R.mean()
                sos = compute_sos(gs, Rc, n_teams)

                # injury adjustment: dock rating points for unavailable contributors
                adj_rows = (await db.execute(text("""
                    SELECT team_id, adj, players FROM nfl.injury_adjustments
                    WHERE season = :y AND week = :w
                """), {"y": year, "w": w})).all()
                inj_by_team = {r[0]: (float(r[1] or 0.0), r[2]) for r in adj_rows}
                inj_vec = np.array([inj_by_team.get(t, (0.0, None))[0] for t in team_ids])
                Rc = Rc - inj_vec

                # market-implied rating: same solver, on closing spreads instead of margins
                mgs = [(tidx[h], tidx[a], mm, wk)
                       for (wk, h, a, mm) in market_all
                       if wk <= w and h in tidx and a in tidx]
                if mgs:
                    Rm, _ = solve_ratings(mgs, n_teams, np.zeros(n_teams), lam_prior)
                    Rm = Rm - Rm.mean()
                    # put the market on Earl's points scale (same dispersion) so the
                    # delta is a like-for-like "Earl vs the market" comparison
                    sd_r, sd_m = Rc.std(), Rm.std()
                    if sd_m > 1e-9:
                        Rm = Rm * (sd_r / sd_m)
                    Rm = Rm + Rc.mean()   # align league means (Earl's mean drops with injury docks)
                else:
                    Rm = np.zeros(n_teams)
                mkt_vec = Rc - Rm

                # rolling-stats components: last played row per team (week <= w)
                rs = {r[0]: r for r in (await db.execute(text("""
                    SELECT DISTINCT ON (team_abbr)
                           team_abbr, games_played, off_pts_r5, def_pts_r5,
                           epa_per_play_r5, def_epa_per_play_r5, point_diff_r5
                    FROM nfl.team_rolling_stats
                    WHERE season = :y AND week <= :w AND game_type = 'REG'
                    ORDER BY team_abbr, week DESC, game_date DESC
                """), {"y": year, "w": w})).all()}

                order = np.argsort(-Rc)
                rank_of = {team_ids[i]: int(pos) + 1 for pos, i in enumerate(order)}

                prev = {r[0]: (r[1], r[2]) for r in (await db.execute(text("""
                    SELECT team_id, rank, rating FROM nfl.power_ratings
                    WHERE season = :y AND week = :w
                """), {"y": year, "w": w - 1})).all()}

                date_row = (await db.execute(text("""
                    SELECT MAX(g.date)::date FROM nfl.games g JOIN nfl.seasons s ON s.id = g.season_id
                    WHERE s.year = :y AND g.week <= :w
                """), {"y": year, "w": w})).scalar()

                # cumulative REG record through week w (this snapshot's W-L-T)
                rec = {}
                for (gwk, gh, ga, ghm, gam) in all_games:
                    if gwk > w:
                        continue
                    hw, hl, ht = rec.get(gh, (0, 0, 0))
                    aw, al, at = rec.get(ga, (0, 0, 0))
                    if ghm > gam:
                        hw += 1; al += 1
                    elif gam > ghm:
                        aw += 1; hl += 1
                    else:
                        ht += 1; at += 1
                    rec[gh] = (hw, hl, ht)
                    rec[ga] = (aw, al, at)

                payload = []
                for i, tid in enumerate(team_ids):
                    rr = rs.get(abbr[tid])
                    games_played = int(rr[1]) if rr else 0
                    epa_diff = (float(rr[4]) - float(rr[5])) if rr and rr[4] is not None and rr[5] is not None else None
                    comps = {
                        "sos": round(float(sos[i]), 3),
                        "prior_rating": round(float(prior[i]), 3),
                        "off_ppg_r5": float(rr[2]) if rr and rr[2] is not None else None,
                        "def_ppg_r5": float(rr[3]) if rr and rr[3] is not None else None,
                        "epa_diff_r5": round(epa_diff, 4) if epa_diff is not None else None,
                        "point_diff_r5": float(rr[6]) if rr and rr[6] is not None else None,
                        "hfa": round(hfa, 3),
                        "injury_adj": round(float(inj_vec[i]), 2),
                        "market_rating": round(float(Rm[i]), 2),
                        "market_delta": round(float(mkt_vec[i]), 2),
                        "injuries": (
                            (json.loads(inj_by_team[tid][1])
                             if isinstance(inj_by_team[tid][1], (str, bytes))
                             else inj_by_team[tid][1])
                            if inj_by_team.get(tid) and inj_by_team[tid][1] is not None
                            else []
                        ),
                    }
                    p = prev.get(tid)
                    payload.append({
                        "season": year, "week": w, "as_of_date": date_row, "team_id": tid,
                        "team_abbr": abbr[tid], "rating": round(float(Rc[i]), 3),
                        "rank": rank_of[tid], "prev_rank": (p[0] if p else None),
                        "rating_delta": (round(float(Rc[i]) - float(p[1]), 3) if p else None),
                        "rank_delta": (rank_of[tid] - p[0] if p else None),
                        "sos": round(float(sos[i]), 3), "games_played": games_played,
                        "injury_adj": round(float(inj_vec[i]), 2),
                        "market_rating": round(float(Rm[i]), 2),
                        "market_delta": round(float(mkt_vec[i]), 2),
                        "wins": rec.get(tid, (0, 0, 0))[0],
                        "losses": rec.get(tid, (0, 0, 0))[1],
                        "ties": rec.get(tid, (0, 0, 0))[2],
                        "prior_rating": round(float(prior[i]), 3),
                        "components": json.dumps(comps), "model_version": MODEL_VERSION,
                    })

                await db.execute(text("""
                    INSERT INTO nfl.power_ratings
                      (season, week, as_of_date, team_id, team_abbr, rating, rank, prev_rank,
                       rating_delta, rank_delta, sos, games_played, prior_rating,
                       wins, losses, ties, injury_adj, market_rating, market_delta,
                       components, model_version)
                    VALUES (:season,:week,:as_of_date,:team_id,:team_abbr,:rating,:rank,:prev_rank,
                            :rating_delta,:rank_delta,:sos,:games_played,:prior_rating,
                            :wins,:losses,:ties,:injury_adj,:market_rating,:market_delta,
                            CAST(:components AS jsonb),:model_version)
                    ON CONFLICT (season, week, team_id) DO UPDATE SET
                       as_of_date=EXCLUDED.as_of_date, rating=EXCLUDED.rating, rank=EXCLUDED.rank,
                       prev_rank=EXCLUDED.prev_rank, rating_delta=EXCLUDED.rating_delta,
                       rank_delta=EXCLUDED.rank_delta, sos=EXCLUDED.sos,
                       games_played=EXCLUDED.games_played, prior_rating=EXCLUDED.prior_rating,
                       wins=EXCLUDED.wins, losses=EXCLUDED.losses, ties=EXCLUDED.ties,
                       injury_adj=EXCLUDED.injury_adj,
                       market_rating=EXCLUDED.market_rating, market_delta=EXCLUDED.market_delta,
                       components=EXCLUDED.components, model_version=EXCLUDED.model_version,
                       generated_at=now()
                """), payload)
                await db.commit()
                logger.info("season %s week %s: %d teams (HFA=%.2f)", year, w, len(payload), hfa)

            # roll forward prior = final ratings of this season
            gs = [(tidx[h], tidx[a], hm - am, wk) for (wk, h, a, hm, am) in all_games]
            prior, _ = solve_ratings(gs, n_teams, np.zeros(n_teams), 0.25)
            prior = prior - prior.mean()

    await engine.dispose()
    logger.info("done")


if __name__ == "__main__":
    import sys
    ty = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(main(ty))
