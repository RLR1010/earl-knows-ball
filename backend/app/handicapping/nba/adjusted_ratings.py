"""True opponent-adjusted ratings + Strength of Schedule for the NBA.

Computes basketball-reference-style SOS and KenPom/Dean-Oliver-style adjusted
Offensive/Defensive ratings, rolling (updated every game, inclusive) and
cumulative (season-long).

Convention (MEMORY.md): every game row is INCLUSIVE — the row for game G holds
statistics including game G's own result. The data_loader reads the PRIOR row
(game_id < g.id ORDER BY game_id DESC) for the leak-safe "entering game G" value.
So this module computes values as-of the current game, inclusive.

A rating is computed by an EXACT linear solve (CRAC / KenPom) over the games
played so far this season. Per team i with n_i games and game counts games_ij
vs opponent j:
    AO_i - sum_j (games_ij/n_i) AD_j = ro_i + L_def
    AD_i - sum_j (games_ij/n_i) AO_j = rd_i + L_off
where ro_i/rd_i are i's mean points for/against per 100 poss (over games <=
current) and L_off==L_def==league average efficiency. SOS (BBRef: "points above
/below average, zero is average") = mean over opponents faced of (AO_opp-AD_opp).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from sqlalchemy import text

logger = logging.getLogger(__name__)

_POSS_C = 1.07


def symmetric_poss(fga, fta, fgm, orb, drb, tov,
                   o_fga, o_fta, o_fgm, o_orb, o_drb, o_tov):
    """Single symmetric game possession, 0.5*(TmPoss+OppPoss), constant 1.07."""
    def _tm(fg, ft, fgm_, orr, drr, to, o_orr, o_drr):
        if (orr + o_drr) > 0:
            return fg + 0.4 * ft - _POSS_C * (orr / (orr + o_drr)) * (fg - fgm_) + to
        return fg + 0.4 * ft + to
    tm = _tm(fga, fta, fgm, orb, drb, tov, o_orb, o_drb)
    op = _tm(o_fga, o_fta, o_fgm, o_orb, o_drb, o_tov, orb, drb)
    return 0.5 * (tm + op)


def _load_games(engine) -> pd.DataFrame:
    sql = """
        SELECT
          g.id AS game_id, g.season_id, g.date, g.game_type,
          g.home_team_id, g.away_team_id, g.home_score, g.away_score,
          g.home_field_goals_made AS h_fgm, g.home_field_goals_attempted AS h_fga,
          g.away_field_goals_made AS a_fgm, g.away_field_goals_attempted AS a_fga,
          g.home_free_throws_attempted AS h_fta, g.away_free_throws_attempted AS a_fta,
          g.home_total_turnovers AS h_tov, g.away_total_turnovers AS a_tov,
          g.home_offensive_rebounds AS h_orb, g.away_offensive_rebounds AS a_orb,
          g.home_defensive_rebounds AS h_drb, g.away_defensive_rebounds AS a_drb,
          g.home_team_id AS home_db_id, g.away_team_id AS away_db_id
        FROM nba.games g
        WHERE g.status = 'FINAL'
          AND g.game_type IN ('REG','POST','PLAYIN')
        ORDER BY g.date, g.id
    """
    return pd.read_sql(sql, engine)


def compute_adjusted(engine) -> pd.DataFrame:
    """Compute rolling & cumulative adjusted ratings + SOS for every game row.

    Returns one row per team-side per game with columns:
      game_id, season_id, date, game_type, team_id, opp_id,
      adj_off, adj_def, sos, cum_adj_ortg, cum_adj_drtg, cum_sos
    (adj_* and cum_* are the same INCLUSIVE value at that row).
    """
    df = _load_games(engine)
    if df.empty:
        return df

    # Scope to the seasons this pipeline produces (2016-17 .. 2025-26). Each
    # season is solved independently, so excluding pre-2016 seasons (which
    # predate our box-score coverage) is safe and expected.
    df = df[df["season_id"].between(26, 35)].reset_index(drop=True)

    df["poss"] = df.apply(
        lambda r: symmetric_poss(
            r.h_fga, r.h_fta, r.h_fgm, r.h_orb, r.h_drb, r.h_tov,
            r.a_fga, r.a_fta, r.a_fgm, r.a_orb, r.a_drb, r.a_tov,
        ), axis=1
    ).replace(0, 1)

    # HARD GUARD: a stray NaN possession must never silently poison a whole
    # season's inclusive cumulative ratings. If any REG/POST/PLAYIN game lacks
    # possession data, fail loudly so the data defect is fixed, not papered over.
    bad = df[df["poss"].isna()]
    if not bad.empty:
        sample = bad[["game_id", "season_id", "game_type", "date", "home_db_id", "away_db_id"]].head(20)
        raise ValueError(
            f"{len(bad)} REG/POST/PLAYIN game(s) have NaN possessions; "
            f"each would poison a whole season's cum_adj/cum_sos. Fix the box data first. "
            f"Sample: {sample.to_dict('records')}"
        )

    df["off_home"] = df.home_score / df.poss * 100.0
    df["def_home"] = df.away_score / df.poss * 100.0
    df["off_away"] = df.away_score / df.poss * 100.0
    df["def_away"] = df.home_score / df.poss * 100.0

    out_rows = []
    for season_id, sdf in df.groupby("season_id", sort=True):
        sdf = sdf.sort_values(["date", "game_id"]).reset_index(drop=True)
        teams = sorted(set(sdf.home_db_id.tolist() + sdf.away_db_id.tolist()))
        idx = {t: i for i, t in enumerate(teams)}
        N = len(teams)

        # ---- Pass 1: solve BBRef-SRS over REGULAR-SEASON games (BBRef's SRS/SOS are
        #      regular-season stats; postseason does not contribute).
        ss = sdf[sdf.game_type == "REG"]
        ss_mov = np.zeros(N)
        ss_cnt = np.zeros(N, dtype=np.int64)
        ss_ij = np.zeros((N, N), dtype=np.int64)
        for _, r in ss.iterrows():
            hi, ai = idx[r.home_db_id], idx[r.away_db_id]
            m = r.home_score - r.away_score
            ss_mov[hi] += m; ss_cnt[hi] += 1; ss_ij[hi, ai] += 1
            ss_mov[ai] -= m; ss_cnt[ai] += 1; ss_ij[ai, hi] += 1
        P_ss = ss_ij.astype(np.float64) / np.maximum(ss_cnt, 1)[:, None]
        mm_ss = np.where(ss_cnt > 0, ss_mov / np.maximum(ss_cnt, 1), 0.0)
        srs_final = mm_ss.copy()
        for _ in range(500):
            nw = mm_ss + P_ss @ srs_final
            if np.max(np.abs(nw - srs_final)) < 1e-9:
                srs_final = nw; break
            srs_final = nw
        # center so league-average SRS = 0 (games-weighted)
        wmean_final = float(np.sum(srs_final * ss_cnt) / max(float(ss_cnt.sum()), 1e-9))
        srs_final = srs_final - wmean_final

        # Pass 2: populates rolling cum_adj (inclusive, leak-safe) + BBRef-SRS SOS
        sum_off = np.zeros(N)
        sum_def = np.zeros(N)
        cnt = np.zeros(N, dtype=np.int64)
        games_ij = np.zeros((N, N), dtype=np.int64)  # times i played j (i-side games)
        opp_srs_sum = np.zeros(N)   # games-weighted sum of OPPONENT season-final SRS
        opp_cnt = np.zeros(N)

        for _, r in sdf.iterrows():
            h, a = r.home_db_id, r.away_db_id
            hi, ai = idx[h], idx[a]
            margin = r.home_score - r.away_score   # home perspective (unused in pass 2 cum_adj; kept for clarity)
            # append game for BOTH sides
            for i, j, oe, de in (
                (hi, ai, r.off_home, r.def_home),
                (ai, hi, r.off_away, r.def_away),
            ):
                sum_off[i] += oe
                sum_def[i] += de
                cnt[i] += 1
                games_ij[i, j] += 1

            # Pomeroy-style iterative adjusted efficiency (canonical, convergent).
            n_i = np.maximum(cnt, 1).astype(float)
            raw_off = sum_off / n_i        # points scored per 100 poss mean
            raw_def = sum_def / n_i        # points allowed per 100 poss mean
            AdjO = raw_off.copy()
            AdjD = raw_def.copy()
            off_share = games_ij.astype(float) / n_i[:, None]
            for _ in range(100):
                opp_AdjD = off_share @ AdjD
                opp_AdjO = off_share @ AdjO
                avgO = float(AdjO.mean())
                avgD = float(AdjD.mean())
                newO = raw_off + (avgD - opp_AdjD)
                newD = raw_def + (avgO - opp_AdjO)
                delta = float(np.max(np.abs(newO - AdjO)) + np.max(np.abs(newD - AdjD)))
                AdjO, AdjD = newO, newD
                if delta < 1e-9:
                    break
            AO = AdjO
            AD = AdjD
            # (matrix-form rank-deficient gauge is replaced by this iterative solver)

            # ---- BBRef-SRS SOS: games-weighted mean of opponent REGULAR-SEASON SRS.
            #      Accumulate only over REG games (BBRef's SOS is a regular-season stat);
            #      POST rows keep the final regular-season SOS so they match BBRef too.
            if r.game_type == "REG":
                opp_srs_sum[hi] += srs_final[ai]
                opp_cnt[hi] += 1
                opp_srs_sum[ai] += srs_final[hi]
                opp_cnt[ai] += 1

            sos_h = opp_srs_sum[hi] / opp_cnt[hi] if opp_cnt[hi] else 0.0
            sos_a = opp_srs_sum[ai] / opp_cnt[ai] if opp_cnt[ai] else 0.0
            out_rows.append({
                "game_id": r.game_id, "season_id": season_id,
                "date": r.date.isoformat(), "game_type": r.game_type,
                "team_id": h, "opp_id": a,
                "adj_off": round(float(AO[hi]), 4), "adj_def": round(float(AD[hi]), 4),
                "sos": round(float(sos_h), 4),
                "cum_adj_ortg": round(float(AO[hi]), 4), "cum_adj_drtg": round(float(AD[hi]), 4),
                "cum_sos": round(float(sos_h), 4),
            })
            out_rows.append({
                "game_id": r.game_id, "season_id": season_id,
                "date": r.date.isoformat(), "game_type": r.game_type,
                "team_id": a, "opp_id": h,
                "adj_off": round(float(AO[ai]), 4), "adj_def": round(float(AD[ai]), 4),
                "sos": round(float(sos_a), 4),
                "cum_adj_ortg": round(float(AO[ai]), 4), "cum_adj_drtg": round(float(AD[ai]), 4),
                "cum_sos": round(float(sos_a), 4),
            })

    return pd.DataFrame(out_rows, columns=[
        "game_id", "season_id", "date", "game_type", "team_id", "opp_id",
        "adj_off", "adj_def", "sos", "cum_adj_ortg", "cum_adj_drtg", "cum_sos",
    ])


def write_adjusted_to_tables(engine, season_filter=None) -> dict:
    """Compute adjusted ratings/SOS and write them into the NBA tables.

    - team_rolling_stats.adj_off_10/adj_def_10  (rolling, per game row)
    - cumulative_game_stats.cum_adj_ortg/cum_adj_drtg/cum_sos (cumulative)

    Keyed on (game_id, team_id) which is unique per team-side per game in both
    tables. Returns {rolling_updated, cumulative_updated} counts.
    """
    df = compute_adjusted(engine)
    if df.empty:
        return {"rolling": 0, "cumulative": 0, "rows": 0}
    if season_filter is not None:
        df = df[df.season_id == season_filter].copy()
    clean = df.copy()
    n = len(clean)
    # single-statement bulk UPDATE per table via unnest (fast, ~one round-trip each)
    with engine.begin() as conn:
        # rolling
        conn.execute(
            text("""
              WITH up(g, t, o, d) AS (
                SELECT * FROM unnest(:gids, :tids, :offs, :defs)
              )
              UPDATE nba.team_rolling_stats r SET adj_off_10=u.o, adj_def_10=u.d
              FROM up u WHERE r.game_id=u.g AND r.team_id=u.t
            """), {
                "gids": clean.game_id.tolist(), "tids": clean.team_id.tolist(),
                "offs": clean.adj_off.tolist(), "defs": clean.adj_def.tolist(),
            })
        # cumulative
        conn.execute(
            text("""
              WITH up(g, t, o, d, s) AS (
                SELECT * FROM unnest(:gids, :tids, :offs, :defs, :soss)
              )
              UPDATE nba.cumulative_game_stats r SET cum_adj_ortg=u.o, cum_adj_drtg=u.d, cum_sos=u.s
              FROM up u WHERE r.game_id=u.g AND r.team_id=u.t
            """), {
                "gids": clean.game_id.tolist(), "tids": clean.team_id.tolist(),
                "offs": clean.cum_adj_ortg.tolist(), "defs": clean.cum_adj_drtg.tolist(),
                "soss": clean.cum_sos.tolist(),
            })
    return {"rolling": n, "cumulative": n, "rows": n}
