"""
ATS-optimized XGBoost model — predicts margin of victory against the spread.

Single source of truth for features, training, and live inference.
Can be imported (predict_margin, set_model_path) or run standalone for training.

    python nfl_xgb_model_ats.py                          # test 2021
    python nfl_xgb_model_ats.py --test-year 2025         # test one year
    python nfl_xgb_model_ats.py --all                    # test 2021-2025
    python nfl_xgb_model_ats.py --weekly-retrain         # weekly retrain mode
"""
import asyncio, logging, pickle, warnings, math, gc, os, shutil
warnings.filterwarnings('ignore')
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from sklearn.metrics import mean_absolute_error
import xgboost as xgb

from app.handicapping.nfl.situational import TEAM_COORDS

# ── Training DB persistence (safe import) ──
try:
    from app.handicapping.db_training import (
        save_training_run,
        update_pkl_filename,
        get_current_training_run,
        get_model_pkl_path,
    )
    _DB_HELPERS_AVAILABLE = True
except ImportError:
    _DB_HELPERS_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("earl.xgb_ats")
log = logger.info

# ── Paths & connections ────────────────────────────────────────────────

MODEL_PATH = Path("/app/data/handicap_model_ats_full.pkl")
DB = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DSN = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football").replace("+asyncpg", "")

_model = None  # cached loaded model


# ── FEATURES — single source of truth ─────────────────────────────────

FEATURES = [
    # Opponent-adjusted scoring (6)
    'hpf','hpa','apf','apa','dpf','dpa',
    # Market features (3)
    'himp','aimp','dimp',
    # Line movement + odds movement (3)
    'spread_movement', 'sp_h_odds_mvmt', 'sp_a_odds_mvmt',
    # Short-term form (8)
    'home_win_pct_r5','away_win_pct_r5',
    'home_margin_r3','away_margin_r3',
    'home_cover_pct_r5','away_cover_pct_r5',
    'home_embarrassed','away_embarrassed',
    # Long-term identity (4)
    'home_season_ats_pct','away_season_ats_pct',
    'home_margin_r10','away_margin_r10',
    # Situational (2)
    'travel_miles','is_dome',
]  # 26 total

assert len(FEATURES) == 26, f"ATS FEATURES count changed to {len(FEATURES)}"


# ── Haversine helper ───────────────────────────────────────────────────

COORDS = {"ARI":(33.5,-112.1),"ATL":(33.8,-84.4),"BAL":(39.3,-76.6),"BUF":(42.8,-78.9),
          "CAR":(35.2,-80.9),"CHI":(41.9,-87.6),"CIN":(39.1,-84.5),"CLE":(41.5,-81.7),
          "DAL":(32.8,-96.8),"DEN":(39.7,-105.0),"DET":(42.3,-83.0),"GB":(44.5,-88.0),
          "HOU":(29.7,-95.4),"IND":(39.8,-86.2),"JAX":(30.3,-81.7),"KC":(39.1,-94.5),
          "LAC":(32.8,-117.1),"LAR":(33.9,-118.3),"LV":(36.1,-115.2),"MIA":(25.8,-80.2),
          "MIN":(44.9,-93.2),"NE":(42.1,-71.3),"NO":(29.9,-90.1),"NYG":(40.8,-74.1),
          "NYJ":(40.8,-74.1),"PHI":(39.9,-75.2),"PIT":(40.4,-80.0),"SEA":(47.6,-122.3),
          "SF":(37.4,-121.9),"TB":(27.9,-82.5),"TEN":(36.2,-86.8),"WAS":(38.9,-76.9)}

def haversine(lat1,lon1,lat2,lon2):
    R=3958.8;dlat=math.radians(lat2-lat1);dlon=math.radians(lon2-lon1)
    a=math.sin(dlat/2)**2+math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))


# ── Model management ───────────────────────────────────────────────────

def set_model_path(model_path: str):
    """Override model path (used by admin panel)."""
    global MODEL_PATH, _model
    MODEL_PATH = Path(model_path)
    _model = None
    log(f"ATS model path set to {model_path}")


def _load_model():
    """Load and cache the pickled XGBoost model."""
    global _model
    if _model is not None:
        return _model
    with open(MODEL_PATH, "rb") as f:
        _model = pickle.load(f)
    log(f"Loaded ATS XGBoost model ({_model.n_features_in_} features)")
    return _model


# ── Inference: predict for a single game (imported by engine.py) ──────

async def predict_margin(db, game_id: int, home_abbr: str, away_abbr: str,
                          yr: int, wk: int, home_stats, away_stats,
                          line, season_avg_pts: float) -> tuple[float, float]:
    """
    Predict home margin for one game. Uses the *same* FEATURES list and
    *same* feature order as the batch training pipeline.

    Returns (margin, confidence).
    """
    model = _load_model()
    conn = None
    try:
        conn = await asyncpg.connect(DSN)

        hpf = float(home_stats.ppg_for); hpa = float(home_stats.ppg_against)
        apf = float(away_stats.ppg_for); apa = float(away_stats.ppg_against)
        ou_raw = line.over_under if (line and line.over_under) else season_avg_pts * 2
        ou = float(ou_raw)

        # ── Helper: average of last N games via subquery ──
        async def _avg_last(abbr, select_expr, limit_n, extra_join="", extra_where=""):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.val) FROM (
                    SELECT {select_expr} as val
                    FROM nfl.games g
                    JOIN nfl.seasons s ON s.id=g.season_id
                    JOIN nfl.teams ht ON ht.id=g.home_team_id
                    JOIN nfl.teams at ON at.id=g.away_team_id
                    {extra_join}
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year < $2 OR (s.year = $2 AND g.week < $3))
                      AND g.game_type='REG' AND g.home_score IS NOT NULL
                      {extra_where}
                    ORDER BY s.year DESC, g.week DESC LIMIT {limit_n}
                ) sub
            """, abbr, yr, wk)
            return float(r[0]) if (r and r[0] is not None) else 0.0

        async def _avg_last_with_spread(abbr, limit_n, extra_where=""):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.covered) FROM (
                    SELECT CASE WHEN gl.spread IS NOT NULL AND
                        CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score > -gl.spread
                             ELSE g.away_score-g.home_score > gl.spread END
                        THEN 1 ELSE 0 END as covered
                    FROM nfl.games g
                    JOIN nfl.seasons s ON s.id=g.season_id
                    JOIN nfl.teams ht ON ht.id=g.home_team_id
                    JOIN nfl.teams at ON at.id=g.away_team_id
                    JOIN nfl.game_lines gl ON gl.game_id=g.id
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year < $2 OR (s.year = $2 AND g.week < $3))
                      AND g.game_type='REG' AND g.home_score IS NOT NULL
                      AND gl.spread IS NOT NULL
                      {extra_where}
                    ORDER BY s.year DESC, g.week DESC LIMIT {limit_n}
                ) sub
            """, abbr, yr, wk)
            return float(r[0]) if (r and r[0] is not None) else 0.5

        # ── Opponent-adjusted PPG ──
        def _opp_adj(v, r, use_def=True):
            if not r or r[0] is None: return v
            idx = 0 if use_def else 1
            return v * (float(r[idx]) / 16.5)

        for abbr, is_home in [(home_abbr, True), (away_abbr, False)]:
            r = await conn.fetchrow("""
                WITH prior AS (
                    SELECT g.week, s.year as ssn,
                           CASE WHEN ht.abbreviation=$2 THEN at.id ELSE ht.id END as opp
                    FROM nfl.games g
                    JOIN nfl.seasons s ON s.id=g.season_id
                    JOIN nfl.teams ht ON ht.id=g.home_team_id
                    JOIN nfl.teams at ON at.id=g.away_team_id
                    WHERE s.year=$3 AND g.game_type='REG'
                      AND (ht.abbreviation=$2 OR at.abbreviation=$2)
                      AND (g.week<$4 OR (g.week=$4 AND g.id<$1))
                    ORDER BY s.year DESC, g.week DESC, g.date DESC LIMIT 5
                )
                SELECT AVG(wr.scoring_defense_rank), AVG(wr.scoring_offense_rank)
                FROM prior LEFT JOIN nfl.weekly_team_rankings wr
                ON wr.team_id=prior.opp AND wr.season=prior.ssn AND wr.week=prior.week
            """, game_id, abbr, yr, wk)
            def_r = (r[0], r[1]) if r else (None, None)
            if is_home:
                hpf_a = _opp_adj(hpf, def_r, True)
                hpa_a = _opp_adj(hpa, def_r, False)
            else:
                apf_a = _opp_adj(apf, def_r, True)
                apa_a = _opp_adj(apa, def_r, False)

        dpf = hpf_a - apf_a; dpa = hpa_a - apa_a

        # ── Form: win pct ──
        h_win_pct = await _avg_last(home_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int ELSE (g.away_score>g.home_score)::int END", 5)
        a_win_pct = await _avg_last(away_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int ELSE (g.away_score>g.home_score)::int END", 5)

        # ── Momentum: margin r3, cover r5, margin r10, season ATS ──
        h_margin_r3 = await _avg_last(home_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 3)
        a_margin_r3 = await _avg_last(away_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 3)
        h_cover_pct = await _avg_last_with_spread(home_abbr, 5)
        a_cover_pct = await _avg_last_with_spread(away_abbr, 5)
        h_margin_r10 = await _avg_last(home_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 10)
        a_margin_r10 = await _avg_last(away_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 10)

        # Embarrassed: lost by 14+ in most recent game
        async def _embarrassed(abbr):
            r = await conn.fetchrow("""
                SELECT CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score
                       ELSE g.away_score-g.home_score END as tm
                FROM nfl.games g JOIN nfl.seasons s ON s.id=g.season_id
                JOIN nfl.teams ht ON ht.id=g.home_team_id
                JOIN nfl.teams at ON at.id=g.away_team_id
                WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                  AND (s.year < $2 OR (s.year = $2 AND g.week < $3))
                  AND g.game_type='REG' AND g.home_score IS NOT NULL
                ORDER BY s.year DESC, g.week DESC LIMIT 1
            """, abbr, yr, wk)
            return 1 if (r and r[0] is not None and float(r[0]) <= -14) else 0
        h_embarrassed = await _embarrassed(home_abbr)
        a_embarrassed = await _embarrassed(away_abbr)

        # Season ATS%
        h_season_ats = await _avg_last_with_spread(home_abbr, 100,
            extra_where="AND s.year=$2 AND g.week < $3")
        a_season_ats = await _avg_last_with_spread(away_abbr, 100,
            extra_where="AND s.year=$2 AND g.week < $3")

        # ── Market features (implied scoring from OU) ──
        t = hpf + hpa + apf + apa
        himp = ou * (hpf + apa) / t if t > 0 else ou / 2
        aimp = ou * (apf + hpa) / t if t > 0 else ou / 2
        dimp = himp - aimp

        # ── Line movement + spread odds movement ──
        gr = await conn.fetchrow("""
            SELECT spread, opening_spread, roof_type,
                   spread_home_odds, spread_away_odds,
                   opening_spread_home_odds, opening_spread_away_odds
            FROM nfl.game_lines WHERE game_id=$1
        """, game_id)
        opening_sp = float(gr['opening_spread']) if (gr and gr['opening_spread'] is not None) else 0.0
        closing_sp = float(gr['spread']) if (gr and gr['spread'] is not None) else 0.0
        spread_mvmt = closing_sp - opening_sp
        is_dome = 1 if (gr and gr['roof_type'] == 'dome') else 0

        # Spread odds movement (juice shading)
        sp_h_odds_mvmt = 0.0; sp_a_odds_mvmt = 0.0
        if gr:
            close_h = gr['spread_home_odds']
            open_h = gr['opening_spread_home_odds']
            close_a = gr['spread_away_odds']
            open_a = gr['opening_spread_away_odds']
            if open_h is not None and close_h is not None:
                sp_h_odds_mvmt = float(close_h) - float(open_h)
            if open_a is not None and close_a is not None:
                sp_a_odds_mvmt = float(close_a) - float(open_a)

        # ── Travel miles ──
        hc = TEAM_COORDS.get(home_abbr, (0, 0))
        ac = TEAM_COORDS.get(away_abbr, (0, 0))
        travel_miles = haversine(hc[0], hc[1], ac[0], ac[1])
        if travel_miles < 50:
            travel_miles = 0.0

        # ── Build feature vector (MUST match FEATURES order) ──
        feats = np.array([[
            hpf_a, hpa_a, apf_a, apa_a, dpf, dpa,
            himp, aimp, dimp,
            spread_mvmt, sp_h_odds_mvmt, sp_a_odds_mvmt,
            h_win_pct, a_win_pct,
            h_margin_r3, a_margin_r3,
            h_cover_pct, a_cover_pct,
            h_embarrassed, a_embarrassed,
            h_season_ats, a_season_ats,
            h_margin_r10, a_margin_r10,
            travel_miles, is_dome,
        ]], dtype=np.float32)

        margin = float(model.predict(feats)[0])
        confidence_base = min(0.5 + abs(margin) * 0.025, 0.85)
        conf = round(min(confidence_base + min(abs(spread_mvmt) * 0.015, 0.08), 0.92), 2)
        return round(margin, 1), round(conf, 2)

    except Exception as e:
        logger.error(f"ATS pred failed [{game_id} {home_abbr}@{away_abbr}]: {e}")
        margin = (home_stats.ppg_for - away_stats.ppg_against) - (away_stats.ppg_for - home_stats.ppg_against)
        return round(margin, 1), 0.50
    finally:
        if conn:
            await conn.close()


# ── Batch training & backtesting ───────────────────────────────────────

async def run():
    """Entry point for standalone training / backtesting."""
    import argparse
    parser = argparse.ArgumentParser(description="Train & backtest ATS model")
    parser.add_argument('--test-year', type=int, default=2021)
    parser.add_argument('--train-window', type=int, default=4)
    parser.add_argument('--weekly-retrain', action='store_true')
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()

    t0 = __import__('time').time()
    engine = create_async_engine(DB)

    log("Loading data...")
    async with engine.connect() as conn:
        r = await conn.execute(text("""
            SELECT g.id, s.year as season, g.week, g.date::date as game_date,
                   ht.abbreviation as ha, at.abbreviation as aa,
                   g.home_score, g.away_score,
                   (g.home_score - g.away_score) as margin, g.roof_type,
                   ht.id as home_team_id, at.id as away_team_id
            FROM games g JOIN seasons s ON s.id=g.season_id
            JOIN teams ht ON ht.id=g.home_team_id JOIN teams at ON at.id=g.away_team_id
            WHERE g.game_type='REG' AND g.home_score IS NOT NULL
            ORDER BY s.year, g.week, g.date
        """))
        df = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])

        r = await conn.execute(text("""
            SELECT game_id, spread, over_under, opening_spread, opening_ou,
                   spread_home_odds, spread_away_odds,
                   opening_spread_home_odds, opening_spread_away_odds
            FROM nfl.game_lines
        """))
        lines = {r.game_id: {
            'sp': r.spread or 0, 'ou': r.over_under or 0,
            'open_sp': r.opening_spread or 0, 'open_ou': r.opening_ou or 0,
            'sp_h_odds': r.spread_home_odds, 'sp_a_odds': r.spread_away_odds,
            'op_sp_h_odds': r.opening_spread_home_odds, 'op_sp_a_odds': r.opening_spread_away_odds,
        } for r in r.fetchall()}

        r = await conn.execute(text("""
            SELECT game_id, opening_spread - spread as spread_mvmt
            FROM nfl.game_lines WHERE opening_spread IS NOT NULL
        """))
        mvmt = {r.game_id: float(r.spread_mvmt or 0) for r in r.fetchall()}

        r = await conn.execute(text("""
            SELECT abbreviation, division FROM teams
        """))
        team_div = {r.abbreviation: r.division for r in r.fetchall()}

        r = await conn.execute(text("""
            SELECT season, week, team_id, scoring_defense_rank, scoring_offense_rank
            FROM weekly_team_rankings
        """))
        rank_rows = [dict(r._mapping) for r in r.fetchall()]
        rankings = pd.DataFrame(rank_rows) if rank_rows else pd.DataFrame()
        if not rankings.empty:
            rankings['team_id'] = rankings['team_id'].astype(int)

    log(f"{len(df)} games ({df.season.min()}-{df.season.max()})")

    # ── Build team-game table ──
    log("Building team-game table...")
    spread_lookup = {}
    cover_lookup = {}
    for _, g in df.iterrows():
        sp = lines.get(g.id, {}).get('sp', 0)
        spread_lookup[g.id] = sp
        cover_lookup[g.id] = g.margin > -sp

    rows = []
    for _, g in df.iterrows():
        rows.append({'game_id': g.id, 'season': g.season, 'week': g.week,
            'game_date': g.game_date, 'team': g.ha, 'opp': g.aa,
            'tid': g.home_team_id, 'oid': g.away_team_id,
            'pf': g.home_score, 'pa': g.away_score,
            'is_home': 1, 'margin': g.margin,
            'covered': int(cover_lookup[g.id]), 'roof': g.roof_type})
        rows.append({'game_id': g.id, 'season': g.season, 'week': g.week,
            'game_date': g.game_date, 'team': g.aa, 'opp': g.ha,
            'tid': g.away_team_id, 'oid': g.home_team_id,
            'pf': g.away_score, 'pa': g.home_score,
            'is_home': 0, 'margin': -g.margin,
            'covered': int(not cover_lookup[g.id]), 'roof': g.roof_type})
    tg = pd.DataFrame(rows).sort_values(
        ['team','season','week','game_date']).reset_index(drop=True)

    # ── Rolling stats ──
    log("Rolling PPG + form...")
    tg['pf_r5'] = tg.groupby('team')['pf'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['pa_r5'] = tg.groupby('team')['pa'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['won'] = tg['margin'] > 0
    tg['win_r5'] = tg.groupby('team')['won'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())

    log("Momentum features...")
    tg['margin_r3'] = tg.groupby('team')['margin'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=0).mean())
    tg['margin_r10'] = tg.groupby('team')['margin'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=0).mean())
    tg['cover_pct_r5'] = tg.groupby('team')['covered'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['season_ats_pct'] = tg.groupby(['team','season'])['covered'].transform(
        lambda x: x.shift(1).expanding().mean())
    tg['prev_margin'] = tg.groupby('team')['margin'].shift(1)
    tg['embarrassed'] = ((tg['prev_margin'] < 0) & (tg['prev_margin'] <= -14)).astype(int)
    tg['embarrassed'] = tg['embarrassed'].fillna(0).astype(int)

    # ── Opponent-adjusted PPG ──
    log("Opponent-adjusted PPG...")
    rk_lookup = {}
    if not rankings.empty:
        for _, rk in rankings.iterrows():
            rk_lookup[(int(rk.team_id), rk.season, rk.week)] = (
                rk.scoring_defense_rank, rk.scoring_offense_rank)
    tg['hpf_adj'] = tg['pf_r5']
    tg['hpa_adj'] = tg['pa_r5']
    for idx in range(len(tg)):
        if idx % 5000 == 0:
            log(f"  adj {idx}/{len(tg)}...")
        g = tg.iloc[idx]
        mask = (tg.team == g.team) & (
            (tg.season < g.season) | ((tg.season == g.season) & (tg.week < g.week)))
        prev5 = tg[mask].tail(5)
        if prev5.empty:
            continue
        apf = 0.0; apa = 0.0; cnt = 0
        for _, pg in prev5.iterrows():
            rk = rk_lookup.get((pg.oid, pg.season, pg.week), (16.5, 16.5))
            def_w = float(rk[0]) / 16.5
            off_w = float(rk[1]) / 16.5
            apf += pg.pf * def_w
            apa += pg.pa * off_w
            cnt += 1
        if cnt > 0:
            tg.at[idx, 'hpf_adj'] = apf / cnt
            tg.at[idx, 'hpa_adj'] = apa / cnt

    # ── Split home/away and merge ──
    log("Merging home/away...")
    h = tg[tg.is_home == 1][['game_id','opp','roof','season','week','game_date','tid','oid',
        'hpf_adj','hpa_adj','win_r5','margin_r3','margin_r10',
        'cover_pct_r5','season_ats_pct','embarrassed']].rename(
        columns={'opp':'ateam','tid':'htid','oid':'atid',
            'hpf_adj':'hpf','hpa_adj':'hpa','win_r5':'home_win_pct_r5',
            'margin_r3':'home_margin_r3','margin_r10':'home_margin_r10',
            'cover_pct_r5':'home_cover_pct_r5','season_ats_pct':'home_season_ats_pct',
            'embarrassed':'home_embarrassed'})
    a = tg[tg.is_home == 0][['game_id','opp','hpf_adj','hpa_adj','win_r5',
        'margin_r3','margin_r10','cover_pct_r5','season_ats_pct','embarrassed']].rename(
        columns={'opp':'hteam','hpf_adj':'apf','hpa_adj':'apa','win_r5':'away_win_pct_r5',
            'margin_r3':'away_margin_r3','margin_r10':'away_margin_r10',
            'cover_pct_r5':'away_cover_pct_r5','season_ats_pct':'away_season_ats_pct',
            'embarrassed':'away_embarrassed'})
    feats = h[['game_id','season','week','game_date','ateam','roof',
               'hpf','hpa','home_win_pct_r5',
               'home_margin_r3','home_margin_r10',
               'home_cover_pct_r5','home_season_ats_pct','home_embarrassed']].merge(
        a[['game_id','hteam','apf','apa','away_win_pct_r5',
           'away_margin_r3','away_margin_r10',
           'away_cover_pct_r5','away_season_ats_pct','away_embarrassed']], on='game_id')
    feats['travel_miles'] = feats.apply(
        lambda r: haversine(*COORDS.get(r.ateam,(0,0)),*COORDS.get(r.hteam,(0,0))), axis=1)
    feats.loc[feats.travel_miles < 50, 'travel_miles'] = 0
    feats['dpf'] = feats.hpf - feats.apf
    feats['dpa'] = feats.hpa - feats.apa

    # ── Market features ──
    log("Market features...")
    def _implied(row):
        l = lines.get(row.game_id, {})
        ou = l.get('ou') or (row.hpf+row.hpa+row.apf+row.apa) / 2
        t = row.hpf+row.apf+row.hpa+row.apa
        hi = ou * (row.hpf+row.apa) / t if t > 0 else ou / 2
        ai = ou * (row.apf+row.hpa) / t if t > 0 else ou / 2
        return pd.Series({'himp': hi, 'aimp': ai})
    im = feats.apply(_implied, axis=1)
    feats['himp'] = im.himp; feats['aimp'] = im.aimp; feats['dimp'] = feats.himp - feats.aimp

    # ── Line features ──
    feats['spread_movement'] = feats.game_id.map(lambda x: mvmt.get(x, 0))

    # Spread odds movement (juice shading)
    def _calc_home_odds_mvmt(gid):
        l = lines.get(gid, {})
        open_h = l.get('op_sp_h_odds'); close_h = l.get('sp_h_odds')
        if open_h is not None and close_h is not None:
            return float(close_h) - float(open_h)
        return 0
    def _calc_away_odds_mvmt(gid):
        l = lines.get(gid, {})
        open_a = l.get('op_sp_a_odds'); close_a = l.get('sp_a_odds')
        if open_a is not None and close_a is not None:
            return float(close_a) - float(open_a)
        return 0
    feats['sp_h_odds_mvmt'] = feats.game_id.map(_calc_home_odds_mvmt)
    feats['sp_a_odds_mvmt'] = feats.game_id.map(_calc_away_odds_mvmt)

    feats['is_dome'] = (feats.roof == 'dome').astype(int)
    feats['actual_margin'] = feats.game_id.map(dict(zip(df.id, df.margin)))

    log(f"{len(feats)} total feature rows ({len(FEATURES)} features)")

    # ── Train/Test ──
    test_years = [2021, 2022, 2023, 2024, 2025] if args.all else [args.test_year]

    for test_season in test_years:
        log(f"\n{'='*55}")
        log(f"ATS MODEL: Testing {test_season}")
        log(f"{'='*55}")
        train_years = [test_season - i for i in range(1, args.train_window + 1)]
        tr_all = feats[feats.season.isin(train_years)].reset_index(drop=True)
        te_all = feats[feats.season == test_season].sort_values('week').reset_index(drop=True)
        max_wk = te_all.week.max()
        log(f"static model W1-W{max_wk}: train={len(tr_all)} ({train_years}), test={len(te_all)}")

        weekly, preds_all = [], []

        if args.weekly_retrain:
            for wk in range(1, max_wk + 1):
                prior = te_all[te_all.week < wk] if wk > 1 else pd.DataFrame()
                tr = pd.concat([tr_all, prior]).reset_index(drop=True)
                te = te_all[te_all.week == wk].reset_index(drop=True)
                if te.empty or len(tr) < 10:
                    continue

                X_tr = tr[FEATURES].fillna(0).values.astype(np.float32)
                y_tr = tr.actual_margin.values
                X_te = te[FEATURES].fillna(0).values.astype(np.float32)
                y_te = te.actual_margin.values

                w = np.ones(len(X_tr))
                for i in range(len(tr)):
                    s = tr.at[tr.index[i], 'season']
                    wk_v = tr.at[tr.index[i], 'week']
                    if s == test_season: w[i] = 4.0 + (wk_v/max_wk)*2.0
                    elif s == test_season - 1: w[i] = 3.0
                    elif s == test_season - 2: w[i] = 2.0
                    w[i] *= 1.0 + (wk_v/18)*0.5

                m = xgb.XGBRegressor(n_estimators=200, max_depth=4, learning_rate=0.05,
                    subsample=0.7, colsample_bytree=0.7, reg_alpha=1.0, reg_lambda=2.0,
                    random_state=42, n_jobs=-1, verbosity=0)
                m.fit(X_tr, y_tr, sample_weight=w, verbose=False)
                p = m.predict(X_te)
                for i, gid in enumerate(te.game_id.values):
                    preds_all.append({
                        'game_id': gid, 'week': wk,
                        'pred_margin': p[i], 'actual_margin': y_te[i]})
                mae = mean_absolute_error(y_te, p)
                log(f"  W{wk:2d}: train={len(tr):3d} test={len(te):2d} MAE={mae:.2f}")
                weekly.append({'week': wk, 'n': len(te), 'mae': round(mae, 2)})
                final_model = m
                del X_tr, y_tr, X_te, y_te, p, w; gc.collect()
        else:
            log(f"Training static ATS model on {len(tr_all)} games ({train_years})")
            w = np.ones(len(tr_all))
            for i in range(len(tr_all)):
                s = tr_all.at[tr_all.index[i], 'season']
                wk_v = tr_all.at[tr_all.index[i], 'week']
                if s == test_season: w[i] = 4.0 + (wk_v/max_wk)*2.0
                elif s == test_season - 1: w[i] = 3.0
                elif s == test_season - 2: w[i] = 2.0
                w[i] *= 1.0 + (wk_v/18)*0.5

            X_tr = tr_all[FEATURES].fillna(0).values.astype(np.float32)
            y_tr = tr_all.actual_margin.values
            m = xgb.XGBRegressor(n_estimators=200, max_depth=4, learning_rate=0.05,
                subsample=0.7, colsample_bytree=0.7, reg_alpha=1.0, reg_lambda=2.0,
                random_state=42, n_jobs=-1, verbosity=0)
            m.fit(X_tr, y_tr, sample_weight=w, verbose=False)
            final_model = m
            del X_tr, y_tr, w

            for wk in range(1, max_wk + 1):
                te = te_all[te_all.week == wk].reset_index(drop=True)
                if te.empty:
                    continue
                X_te = te[FEATURES].fillna(0).values.astype(np.float32)
                y_te = te.actual_margin.values
                p = m.predict(X_te)
                for i, gid in enumerate(te.game_id.values):
                    preds_all.append({
                        'game_id': gid, 'week': wk,
                        'pred_margin': p[i], 'actual_margin': y_te[i]})
                mae = mean_absolute_error(y_te, p)
                log(f"  W{wk:2d}: test={len(te):2d} MAE={mae:.2f}")
                weekly.append({'week': wk, 'n': len(te), 'mae': round(mae, 2)})
                del X_te, y_te, p
            gc.collect()

        if not preds_all:
            log(f"No predictions for {test_season}!")
            continue
        pd_pred = pd.DataFrame(preds_all)
        outfile = f"/app/data/ats_backtest_{test_season}.csv"
        pd_pred.to_csv(outfile, index=False)
        log(f"Predictions saved to {outfile}")

        # ── Evaluate ──
        omae = mean_absolute_error(pd_pred.actual_margin, pd_pred.pred_margin)
        errs = pd_pred.actual_margin - pd_pred.pred_margin

        async with engine.connect() as conn:
            r = await conn.execute(text(
                "SELECT game_id, spread, over_under FROM nfl.game_lines"))
            sm = {r.game_id: {'sp': r.spread, 'ou': r.over_under} for r in r.fetchall()}
        pd_pred['sl'] = pd_pred.game_id.map(
            lambda x: sm[x]['sp'] if x in sm else None)

        mk = pd_pred.sl.notna()
        h = mk.sum()
        ac = ((pd_pred.loc[mk, 'pred_margin'] > -pd_pred.loc[mk, 'sl'])
              == (pd_pred.loc[mk, 'actual_margin'] > -pd_pred.loc[mk, 'sl'])).sum()
        ai = h - ac

        print(f"\n{'='*55}")
        print(f"ATS MODEL {test_season}: {len(FEATURES)} features")
        print(f"{'='*55}")
        print(f"  MAE: {omae:.2f} | err={errs.mean():.2f} | sigma={errs.std():.2f}")
        print(f"  +/-3pts: {(abs(errs)<3).mean():.1%} | +/-5pts: {(abs(errs)<5).mean():.1%}")
        print(f"  ATS: {ac}-{ai} ({100*ac/max(h,1):.1f}%)")
        print(f"\n  Weekly:")
        for r in weekly:
            print(f"    W{r['week']:2d}: n={r['n']:2d} MAE={r['mae']:.2f}")

        imp = pd.DataFrame(
            {'f': FEATURES, 'v': final_model.feature_importances_}
        ).sort_values('v', ascending=False)
        print(f"\n  Feature importance:")
        for _, r in imp.iterrows():
            print(f"    {r.f:>16s}: {r.v:.3f} {'█'*int(r.v*100)}{'░'*(20-int(r.v*100))}")

        # Save model
        with open(f'/app/data/handicap_model_ats_{test_season}.pkl', 'wb') as f:
            pickle.dump(final_model, f)
        with open('/app/data/handicap_model_ats_full.pkl', 'wb') as f:
            pickle.dump(final_model, f)
        log(f"ATS model saved")

        # Save results JSON
        import json as _json
        result_entry = {
            "test_year": test_season,
            "train_years": train_years,
            "total_games": len(pd_pred),
            "mae": round(omae, 2),
            "err_mean": round(float(errs.mean()), 2),
            "err_std": round(float(errs.std()), 2),
            "within_3": round((abs(errs) < 3).mean(), 3),
            "within_5": round((abs(errs) < 5).mean(), 3),
            "ats": {
                "correct": int(ac), "incorrect": int(ai),
                "total": int(h), "pct": round(100*ac/max(h, 1), 1)},
            "feature_importance": [
                {"feature": r.f, "importance": round(r.v, 4)}
                for _, r in imp.iterrows()],
            "features": FEATURES,
        }
        # Save results to DB (or fall back to JSON)
        if _DB_HELPERS_AVAILABLE:
            pkl_dir = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/nfl")
            pkl_dir.mkdir(parents=True, exist_ok=True)

            training_id = save_training_run(
                sport="nfl",
                model_type="ats",
                results_json=result_entry,
                pkl_filename="",
                algorithm="xgboost",
                description=f"NFL ATS backtest: {test_season}",
                test_year=test_season,
                train_years=None,
            )

            pkl_name = f"{training_id}.pkl"
            src_pkl = pkl_dir / f"nfl_ats_{test_season}.pkl"
            pkl_path = pkl_dir / pkl_name
            if src_pkl.exists():
                shutil.copy2(str(src_pkl), str(pkl_path))

            update_pkl_filename("nfl", training_id, pkl_name)
            log(f"Results saved to DB (training_id={training_id}, pkl={pkl_name})")
        else:
            results_path = "/app/data/ats_backtest_results.json"
            try:
                with open(results_path) as f:
                    existing = _json.load(f)
            except Exception:
                existing = []
            existing = [r for r in existing if r.get("test_year") != test_season]
            existing.append(result_entry)
            with open(results_path, 'w') as f:
                _json.dump(existing, f, indent=2)
            log(f"Results saved to {results_path}")
        log(f"Done ATS {test_season} in {__import__('time').time()-t0:.0f}s")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
