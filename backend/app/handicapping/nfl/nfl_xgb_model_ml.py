"""
ML-optimized XGBoost model — predicts home win probability.

Single source of truth for features, training, and live inference.
Can be imported (predict_home_win_prob, set_model_path) or run standalone.

    python nfl_xgb_model_ml.py                    # test 2021
    python nfl_xgb_model_ml.py --test-year 2025   # test one year
    python nfl_xgb_model_ml.py --all              # test 2021-2025
"""
import asyncio, logging, pickle, warnings, math, gc, os
warnings.filterwarnings('ignore')
from pathlib import Path
from datetime import date

import numpy as np
import pandas as pd
import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from sklearn.metrics import log_loss, brier_score_loss, accuracy_score, roc_auc_score
from sklearn.calibration import CalibratedClassifierCV
import xgboost as xgb

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("earl.xgb_ml")
log = logger.info

# ── Paths & connections ────────────────────────────────────────────────

MODEL_PATH = Path("/app/data/handicap_model_ml_full.pkl")
DB = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DSN = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football").replace("+asyncpg", "")

_model = None      # cached XGBClassifier
_calibrator = None # cached CalibratedClassifierCV


# ── FEATURES — single source of truth ─────────────────────────────────

FEATURES = [
    # Opponent-adjusted scoring (6)
    'hpf','hpa','apf','apa','dpf','dpa',
    # Market features (3)
    'himp','aimp','dimp',
    # Line features (2)
    'spread','spread_movement',
    # Short-term form (12)
    'home_win_pct_r5','away_win_pct_r5',
    'home_opp_win_pct_r5','away_opp_win_pct_r5',
    'home_margin_r3','away_margin_r3',
    'home_momentum_delta','away_momentum_delta',
    'home_cover_pct_r5','away_cover_pct_r5',
    'home_embarrassed','away_embarrassed',
    # Long-term identity (4)
    'home_margin_r10','away_margin_r10',
    'home_season_ats_pct','away_season_ats_pct',
    # Situational (6)
    'rest_diff','travel_miles','tz_diff','is_div','is_short','is_dome',
]  # 33 total

assert len(FEATURES) == 33, f"ML FEATURES count changed to {len(FEATURES)}"


# ── Constants ──────────────────────────────────────────────────────────

TZ = {"ARI":-7,"ATL":-5,"BAL":-5,"BUF":-5,"CAR":-5,"CHI":-6,"CIN":-5,"CLE":-5,
      "DAL":-6,"DEN":-7,"DET":-5,"GB":-6,"HOU":-6,"IND":-5,"JAX":-5,"KC":-6,
      "LAC":-8,"LAR":-8,"LV":-8,"MIA":-5,"MIN":-6,"NE":-5,"NO":-6,"NYG":-5,
      "NYJ":-5,"PHI":-5,"PIT":-5,"SEA":-8,"SF":-8,"TB":-5,"TEN":-6,"WAS":-5}
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


def ml_implied(ml):
    """Convert moneyline to implied probability."""
    if ml is None or ml == 0:
        return 0.5
    return abs(ml) / (abs(ml) + 100.0) if ml < 0 else 100.0 / (ml + 100.0)


# ── Model management ───────────────────────────────────────────────────

def set_model_path(model_path: str):
    """Override model path (used by admin panel)."""
    global MODEL_PATH, _model, _calibrator
    MODEL_PATH = Path(model_path)
    _model = None
    _calibrator = None
    log(f"ML model path set to {model_path}")


def _load_model():
    """Load and cache the pickled CalibratedClassifierCV."""
    global _calibrator
    if _calibrator is not None:
        return _calibrator
    with open(MODEL_PATH, "rb") as f:
        _calibrator = pickle.load(f)
    log(f"Loaded ML calibrated model")
    return _calibrator


# ── Inference: predict for a single game (imported by engine.py) ──────

async def predict_home_win_prob(db, game_id, home_abbr, away_abbr,
                                 yr, wk, home_stats, away_stats,
                                 line, season_avg_pts):
    """
    Predict home win probability for one game.

    Returns (home_win_prob, confidence, edge).
    """
    calibrator = _load_model()
    conn = None
    try:
        conn = await asyncpg.connect(DSN)

        hpf = float(home_stats.ppg_for); hpa = float(home_stats.ppg_against)
        apf = float(away_stats.ppg_for); apa = float(away_stats.ppg_against)
        ou = float(line.over_under if (line and line.over_under) else season_avg_pts * 2)

        # ── Helper: average of last N games ──
        async def _avg_last(abbr, select_expr, limit_n, extra_join="", extra_where=""):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.val) FROM (
                    SELECT {select_expr} as val
                    FROM nfl.games g JOIN nfl.seasons s ON s.id=g.season_id
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

        async def _avg_last_cover(abbr, limit_n, extra_where=""):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.covered) FROM (
                    SELECT CASE WHEN gl.spread IS NOT NULL AND
                        CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score>gl.spread
                             ELSE g.away_score-g.home_score>-gl.spread END
                        THEN 1 ELSE 0 END as covered
                    FROM nfl.games g JOIN nfl.seasons s ON s.id=g.season_id
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
            return v * (float(r[0 if use_def else 1]) / 16.5)

        for abbr, is_home in [(home_abbr, True), (away_abbr, False)]:
            r = await conn.fetchrow("""
                WITH prior AS (
                    SELECT g.week, s.year as ssn,
                           CASE WHEN ht.abbreviation=$2 THEN at.id ELSE ht.id END as opp
                    FROM nfl.games g JOIN nfl.seasons s ON s.id=g.season_id
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
            if is_home:
                hpf_a = _opp_adj(hpf, r, True); hpa_a = _opp_adj(hpa, r, False)
            else:
                apf_a = _opp_adj(apf, r, True); apa_a = _opp_adj(apa, r, False)

        dpf = hpf_a - apf_a; dpa = hpa_a - apa_a

        # ── Market features ──
        t = hpf + hpa + apf + apa
        himp = ou * (hpf + apa) / t if t > 0 else ou / 2
        aimp = ou * (apf + hpa) / t if t > 0 else ou / 2
        dimp = himp - aimp

        # ── Line features ──
        gl = await conn.fetchrow(
            "SELECT spread, opening_spread, home_moneyline, away_moneyline "
            "FROM nfl.game_lines WHERE game_id=$1", game_id)
        closing_sp = float(gl['spread']) if (gl and gl['spread'] is not None) else 0.0
        opening_sp = float(gl['opening_spread']) if (gl and gl['opening_spread'] is not None) else 0.0
        spread_mvmt = closing_sp - opening_sp

        # ── Form + momentum ──
        h_win_pct = await _avg_last(home_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int ELSE (g.away_score>g.home_score)::int END", 5)
        a_win_pct = await _avg_last(away_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int ELSE (g.away_score>g.home_score)::int END", 5)
        h_opp_win = a_win_pct; a_opp_win = h_win_pct  # cross-reference

        h_margin_r3 = await _avg_last(home_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 3)
        a_margin_r3 = await _avg_last(away_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 3)

        # Momentum delta: last vs second-last margin
        async def _mom_delta(abbr):
            r = await conn.fetchrow("""
                WITH last_two AS (
                    SELECT CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score
                           ELSE g.away_score-g.home_score END as tm
                    FROM nfl.games g JOIN nfl.seasons s ON s.id=g.season_id
                    JOIN nfl.teams ht ON ht.id=g.home_team_id
                    JOIN nfl.teams at ON at.id=g.away_team_id
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year<$2 OR (s.year=$2 AND g.week<$3))
                      AND g.game_type='REG' AND g.home_score IS NOT NULL
                    ORDER BY s.year DESC, g.week DESC LIMIT 2
                )
                SELECT MAX(tm) - MIN(tm) FROM last_two
            """, abbr, yr, wk)
            return float(r[0]) if (r and r[0] is not None) else 0.0
        h_mom_delta = await _mom_delta(home_abbr)
        a_mom_delta = await _mom_delta(away_abbr)

        h_cover_pct = await _avg_last_cover(home_abbr, 5)
        a_cover_pct = await _avg_last_cover(away_abbr, 5)

        async def _embarrassed(abbr):
            r = await conn.fetchrow("""
                SELECT CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score
                       ELSE g.away_score-g.home_score END as tm
                FROM nfl.games g JOIN nfl.seasons s ON s.id=g.season_id
                JOIN nfl.teams ht ON ht.id=g.home_team_id
                JOIN nfl.teams at ON at.id=g.away_team_id
                WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                  AND (s.year<$2 OR (s.year=$2 AND g.week<$3))
                  AND g.game_type='REG' AND g.home_score IS NOT NULL
                ORDER BY s.year DESC, g.week DESC LIMIT 1
            """, abbr, yr, wk)
            return 1 if (r and r[0] is not None and float(r[0]) <= -14) else 0
        h_embarrassed = await _embarrassed(home_abbr)
        a_embarrassed = await _embarrassed(away_abbr)

        h_margin_r10 = await _avg_last(home_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 10)
        a_margin_r10 = await _avg_last(away_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 10)

        h_season_ats = await _avg_last_cover(home_abbr, 100,
            extra_where="AND s.year=$2 AND g.week < $3")
        a_season_ats = await _avg_last_cover(away_abbr, 100,
            extra_where="AND s.year=$2 AND g.week < $3")

        # ── Situational ──
        rows = await conn.fetch("SELECT abbreviation, division FROM nfl.teams")
        divs = {r['abbreviation']: r['division'] for r in rows}
        is_div = 1 if divs.get(home_abbr) and divs[home_abbr] == divs.get(away_abbr) else 0

        hc = COORDS.get(home_abbr, (0, 0)); ac = COORDS.get(away_abbr, (0, 0))
        travel_miles = haversine(hc[0], hc[1], ac[0], ac[1])
        if travel_miles < 50: travel_miles = 0
        tz_diff = TZ.get(home_abbr, -5) - TZ.get(away_abbr, -5)

        gr = await conn.fetchrow(
            "SELECT date::date, roof_type FROM nfl.games WHERE id=$1", game_id)
        gd = gr[0] if gr else None
        is_short = 1 if (gd and isinstance(gd, date) and gd.weekday() in (3, 4, 5)) else 0
        is_dome = 1 if (gr and gr[1] == 'dome') else 0

        async def last_game(abbr):
            r = await conn.fetchrow("""
                SELECT MAX(g.date::date) FROM nfl.games g JOIN nfl.seasons s ON s.id=g.season_id
                WHERE (g.home_team_id=(SELECT id FROM nfl.teams WHERE abbreviation=$1)
                    OR g.away_team_id=(SELECT id FROM nfl.teams WHERE abbreviation=$1))
                  AND (s.year<$2 OR (s.year=$2 AND g.week<$3))
                  AND g.game_type='REG' AND g.home_score IS NOT NULL
            """, abbr, yr, wk)
            return r[0] if r and r[0] else None
        hl = await last_game(home_abbr); al = await last_game(away_abbr)
        rest_diff = ((gd - hl).days if (gd and hl) else 7
                     - (gd - al).days if (gd and al) else 7)

        # ── Build feature vector (MUST match FEATURES order) ──
        feats = np.array([[
            hpf_a, hpa_a, apf_a, apa_a, dpf, dpa,
            himp, aimp, dimp,
            closing_sp, spread_mvmt,
            h_win_pct, a_win_pct, h_opp_win, a_opp_win,
            h_margin_r3, a_margin_r3,
            h_mom_delta, a_mom_delta,
            h_cover_pct, a_cover_pct,
            h_embarrassed, a_embarrassed,
            h_margin_r10, a_margin_r10,
            h_season_ats, a_season_ats,
            rest_diff, travel_miles, tz_diff, is_div, is_short, is_dome,
        ]], dtype=np.float32)

        home_win_prob = float(calibrator.predict_proba(feats)[0, 1])
        market_implied = 1.0 / (1.0 + math.exp(-closing_sp / 7.0))
        edge = home_win_prob - market_implied
        conf = min(0.5 + abs(home_win_prob - 0.5) + abs(edge) * 0.5, 0.95)
        return round(home_win_prob, 4), round(conf, 2), round(edge, 4)

    except Exception as e:
        logger.error(f"ML pred failed [{game_id} {home_abbr}@{away_abbr}]: {e}")
        est_margin = (home_stats.ppg_for - away_stats.ppg_against) \
                     - (away_stats.ppg_for - home_stats.ppg_against)
        prob = 1.0 / (1.0 + math.exp(-est_margin / 7.0))
        return round(prob, 4), 0.50, 0.0
    finally:
        if conn:
            await conn.close()


# ── Batch training & backtesting ──────────────────────────────────────

async def run():
    """Entry point for standalone training / backtesting."""
    import argparse
    parser = argparse.ArgumentParser(description="Train & backtest ML model")
    parser.add_argument('--test-year', type=int, default=2021)
    parser.add_argument('--train-window', type=int, default=4)
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
                   (g.home_score - g.away_score) as margin,
                   g.home_score > g.away_score as home_win,
                   g.roof_type, g.temperature, g.wind_speed,
                   ht.id as home_team_id, at.id as away_team_id
            FROM games g JOIN seasons s ON s.id=g.season_id
            JOIN teams ht ON ht.id=g.home_team_id JOIN teams at ON at.id=g.away_team_id
            WHERE g.game_type='REG' AND g.home_score IS NOT NULL
            ORDER BY s.year, g.week, g.date
        """))
        df = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])
        log(f"Home win rate: {df.home_win.mean():.1%} ({df.home_win.sum()}/{len(df)})")

        r = await conn.execute(text("""
            SELECT game_id, spread, over_under, opening_spread, opening_ou,
                   home_moneyline, away_moneyline
            FROM nfl.game_lines
        """))
        lines = {r.game_id: {
            'sp': r.spread or 0, 'ou': r.over_under or 0,
            'open_sp': r.opening_spread or 0, 'open_ou': r.opening_ou or 0,
            'hml': r.home_moneyline, 'aml': r.away_moneyline,
        } for r in r.fetchall()}

        r = await conn.execute(text("""
            SELECT game_id, opening_spread - spread as spread_mvmt
            FROM nfl.game_lines WHERE opening_spread IS NOT NULL
        """))
        mvmt = {r.game_id: float(r.spread_mvmt or 0) for r in r.fetchall()}

        r = await conn.execute(text("SELECT abbreviation, division FROM teams"))
        team_div = {r.abbreviation: r.division for r in r.fetchall()}

        r = await conn.execute(text("""
            SELECT season, week, team_id, scoring_defense_rank, scoring_offense_rank
            FROM weekly_team_rankings
        """))
        rank_rows = [dict(r._mapping) for r in r.fetchall()]
        rankings = pd.DataFrame(rank_rows) if rank_rows else pd.DataFrame()
        if not rankings.empty:
            rankings['team_id'] = rankings['team_id'].astype(int)

    # ── Build team-game table ──
    log("Building team-game table...")
    cover_lookup = {}
    for _, g in df.iterrows():
        sp = lines.get(g.id, {}).get('sp', 0)
        cover_lookup[g.id] = g.margin > -sp

    rows = []
    for _, g in df.iterrows():
        home_covers = cover_lookup[g.id]
        rows.append({'game_id': g.id, 'season': g.season, 'week': g.week,
            'game_date': g.game_date, 'team': g.ha, 'opp': g.aa,
            'tid': g.home_team_id, 'oid': g.away_team_id,
            'pf': g.home_score, 'pa': g.away_score,
            'is_home': 1, 'won': g.home_win,
            'covered': int(home_covers), 'margin': g.margin,
            'roof': g.roof_type, 'temp': g.temperature, 'wind': g.wind_speed})
        rows.append({'game_id': g.id, 'season': g.season, 'week': g.week,
            'game_date': g.game_date, 'team': g.aa, 'opp': g.ha,
            'tid': g.away_team_id, 'oid': g.home_team_id,
            'pf': g.away_score, 'pa': g.home_score,
            'is_home': 0, 'won': not g.home_win,
            'covered': int(not home_covers), 'margin': -g.margin,
            'roof': g.roof_type, 'temp': g.temperature, 'wind': g.wind_speed})
    tg = pd.DataFrame(rows).sort_values(
        ['team','season','week','game_date']).reset_index(drop=True)

    # ── Rolling features + momentum ──
    log("Rolling PPG + form + momentum...")
    tg['pf_r5'] = tg.groupby('team')['pf'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['pa_r5'] = tg.groupby('team')['pa'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['win_r5'] = tg.groupby('team')['won'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['margin_r3'] = tg.groupby('team')['margin'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=0).mean())
    tg['margin_r10'] = tg.groupby('team')['margin'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=0).mean())
    tg['cover_pct_r5'] = tg.groupby('team')['covered'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['season_ats_pct'] = tg.groupby(['team','season'])['covered'].transform(
        lambda x: x.shift(1).expanding().mean())
    # Momentum delta: last - second-last
    tg['margin_lag1'] = tg.groupby('team')['margin'].shift(1)
    tg['margin_lag2'] = tg.groupby('team')['margin'].shift(2)
    tg['momentum_delta'] = tg['margin_lag1'] - tg['margin_lag2']
    tg['momentum_delta'] = tg['momentum_delta'].fillna(0)
    # Blowout bounce-back
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

    # ── Split home/away ──
    log("Merging home/away + features...")
    h = tg[tg.is_home == 1][['game_id','opp','roof','temp','wind',
        'season','week','game_date','tid','oid',
        'hpf_adj','hpa_adj','win_r5',
        'margin_r3','margin_r10','momentum_delta','cover_pct_r5',
        'season_ats_pct','embarrassed']].rename(
        columns={'opp':'ateam','tid':'htid','oid':'atid',
            'hpf_adj':'hpf','hpa_adj':'hpa','win_r5':'home_win_pct_r5',
            'margin_r3':'home_margin_r3','margin_r10':'home_margin_r10',
            'momentum_delta':'home_momentum_delta',
            'cover_pct_r5':'home_cover_pct_r5','season_ats_pct':'home_season_ats_pct',
            'embarrassed':'home_embarrassed'})
    a = tg[tg.is_home == 0][['game_id','opp','hpf_adj','hpa_adj','win_r5',
        'margin_r3','margin_r10','momentum_delta','cover_pct_r5',
        'season_ats_pct','embarrassed']].rename(
        columns={'opp':'hteam','hpf_adj':'apf','hpa_adj':'apa','win_r5':'away_win_pct_r5',
            'margin_r3':'away_margin_r3','margin_r10':'away_margin_r10',
            'momentum_delta':'away_momentum_delta',
            'cover_pct_r5':'away_cover_pct_r5','season_ats_pct':'away_season_ats_pct',
            'embarrassed':'away_embarrassed'})
    feats = h[['game_id','season','week','game_date','ateam','roof',
               'hpf','hpa','home_win_pct_r5',
               'home_margin_r3','home_margin_r10','home_momentum_delta',
               'home_cover_pct_r5','home_season_ats_pct','home_embarrassed']].merge(
        a[['game_id','hteam','apf','apa','away_win_pct_r5',
           'away_margin_r3','away_margin_r10','away_momentum_delta',
           'away_cover_pct_r5','away_season_ats_pct','away_embarrassed']],
        on='game_id')
    # Opponent strength: cross-reference win pcts
    feats['home_opp_win_pct_r5'] = feats['away_win_pct_r5']
    feats['away_opp_win_pct_r5'] = feats['home_win_pct_r5']
    feats['dpf'] = feats.hpf - feats.apf
    feats['dpa'] = feats.hpa - feats.apa

    # ── Market features ──
    log("Market features...")
    def _market(row):
        l = lines.get(row.game_id, {})
        ou = l.get('ou') or (row.hpf+row.hpa+row.apf+row.apa) / 2
        t = row.hpf+row.apf+row.hpa+row.apa
        hi = ou * (row.hpf+row.apa) / t if t > 0 else ou / 2
        ai = ou * (row.apf+row.hpa) / t if t > 0 else ou / 2
        hml = ml_implied(l.get('hml'))
        aml = ml_implied(l.get('aml'))
        return pd.Series({'himp':hi,'aimp':ai,'hml_i':hml,'aml_i':aml})
    im = feats.apply(_market, axis=1)
    feats['himp'] = im.himp; feats['aimp'] = im.aimp; feats['dimp'] = feats.himp - feats.aimp
    feats['home_ml_implied'] = im.hml_i
    feats['away_ml_implied'] = im.aml_i

    # ── Line features ──
    feats['spread'] = feats.game_id.map(lambda x: lines.get(x, {}).get('sp', 0))
    feats['spread_movement'] = feats.game_id.map(lambda x: mvmt.get(x, 0))

    # ── Situational ──
    log("Situational features...")
    tg2 = tg.sort_values(['team','season','week','game_date']).copy()
    tg2['game_date'] = pd.to_datetime(tg2['game_date'])
    tg2['prev_date'] = tg2.groupby('team')['game_date'].shift(1)
    tg2['rest'] = (tg2.game_date - tg2.prev_date).dt.days.fillna(7).clip(0, 21)
    hr = tg2[tg2.is_home==1][['game_id','rest']].rename(columns={'rest':'hr'})
    ar_ = tg2[tg2.is_home==0][['game_id','rest']].rename(columns={'rest':'ar'})
    feats = feats.merge(hr, on='game_id').merge(ar_, on='game_id')
    feats['rest_diff'] = feats.hr - feats.ar
    feats['h_bye'] = (feats.hr >= 13).astype(int)
    feats['a_bye'] = (feats.ar >= 13).astype(int)
    feats['travel_miles'] = feats.apply(
        lambda r: haversine(*COORDS.get(r.ateam,(0,0)),*COORDS.get(r.hteam,(0,0))), axis=1)
    feats.loc[feats.travel_miles < 50, 'travel_miles'] = 0
    feats['tz_diff'] = feats.apply(
        lambda r: TZ.get(r.hteam,-5)-TZ.get(r.ateam,-5), axis=1)
    feats['is_div'] = feats.apply(
        lambda r: 1 if team_div.get(r.hteam) == team_div.get(r.ateam) else 0, axis=1)
    feats['game_date'] = pd.to_datetime(feats['game_date'])
    feats['is_short'] = feats.game_date.dt.dayofweek.isin([3, 4, 5]).astype(int)
    feats['is_dome'] = (feats.roof == 'dome').astype(int)

    # ── Target ──
    feats['home_win'] = feats.game_id.map(
        dict(zip(df.id, df.home_win.astype(int))))

    log(f"{len(feats)} total feature rows ({len(FEATURES)} features)")

    # ── Train/Test ──
    test_years = [2021, 2022, 2023, 2024, 2025] if args.all else [args.test_year]

    for test_season in test_years:
        log(f"\n{'='*55}")
        log(f"ML MODEL: Testing {test_season}")
        log(f"{'='*55}")
        train_years = [test_season - i for i in range(1, args.train_window + 1)]
        tr_all = feats[feats.season.isin(train_years)].reset_index(drop=True)
        te_all = feats[feats.season == test_season].sort_values('week').reset_index(drop=True)
        max_wk = te_all.week.max()
        log(f"static model W1-W{max_wk}: train={len(tr_all)} ({train_years}), test={len(te_all)}")

        weekly, preds_all = [], []

        log(f"Training static ML model on {len(tr_all)} games")
        w = np.ones(len(tr_all))
        for i in range(len(tr_all)):
            s = tr_all.at[tr_all.index[i], 'season']
            wk_v = tr_all.at[tr_all.index[i], 'week']
            if s == test_season: w[i] = 4.0 + (wk_v/max_wk)*2.0
            elif s == test_season - 1: w[i] = 3.0
            elif s == test_season - 2: w[i] = 2.0
            w[i] *= 1.0 + (wk_v/18)*0.5

        X_tr = tr_all[FEATURES].fillna(0).values.astype(np.float32)
        y_tr = tr_all.home_win.values.astype(np.int32)

        m = xgb.XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.04,
            subsample=0.75, colsample_bytree=0.7,
            reg_alpha=1.0, reg_lambda=2.0,
            scale_pos_weight=(1 - y_tr.mean()) / max(y_tr.mean(), 0.01),
            random_state=42, n_jobs=-1, verbosity=0)
        m.fit(X_tr, y_tr, sample_weight=w, verbose=False)

        calibrator = CalibratedClassifierCV(m, method='sigmoid', cv='prefit')
        calibrator.fit(X_tr, y_tr)
        final_model = calibrator
        del X_tr, y_tr, w

        for wk in range(1, max_wk + 1):
            te = te_all[te_all.week == wk].reset_index(drop=True)
            if te.empty:
                continue
            X_te = te[FEATURES].fillna(0).values.astype(np.float32)
            y_te = te.home_win.values
            prob = final_model.predict_proba(X_te)[:, 1]
            pred_class = (prob >= 0.5).astype(int)

            for i, gid in enumerate(te.game_id.values):
                preds_all.append({
                    'game_id': gid, 'week': wk,
                    'home_win_prob': round(float(prob[i]), 4),
                    'pred_home_win': int(pred_class[i]),
                    'actual_home_win': int(y_te[i]),
                })
            acc = accuracy_score(y_te, pred_class)
            brier = brier_score_loss(y_te, prob)
            log(f"  W{wk:2d}: test={len(te):2d} Acc={acc:.1%} Brier={brier:.3f}")
            weekly.append({'week': wk, 'n': len(te), 'acc': round(acc, 3),
                           'brier': round(brier, 4)})
            del X_te, prob
        gc.collect()

        if not preds_all:
            continue
        pd_pred = pd.DataFrame(preds_all)

        # ── Evaluate ──
        ml_c = (pd_pred.pred_home_win == pd_pred.actual_home_win).sum()
        ml_i = len(pd_pred) - ml_c

        async with engine.connect() as conn:
            r = await conn.execute(text("""
                SELECT game_id, spread, home_moneyline, away_moneyline, over_under
                FROM nfl.game_lines
            """))
            mll = {r.game_id: {
                'sp': r.spread, 'hml': r.home_moneyline,
                'aml': r.away_moneyline, 'ou': r.over_under,
            } for r in r.fetchall()}

        def _ml_value(prob, ml):
            ip = ml_implied(ml)
            return prob > ip + 0.05

        val_bets = 0; val_wins = 0
        for _, row in pd_pred.iterrows():
            l = mll.get(row.game_id, {})
            if not l:
                continue
            hml = l.get('hml'); aml = l.get('aml')
            hp = row.home_win_prob
            if hml and _ml_value(hp, hml):
                val_bets += 1
                if row.actual_home_win: val_wins += 1
            if aml and _ml_value(1 - hp, aml):
                val_bets += 1
                if not row.actual_home_win: val_wins += 1

        log_loss_val = log_loss(
            pd_pred.actual_home_win, pd_pred.home_win_prob)
        brier = brier_score_loss(
            pd_pred.actual_home_win, pd_pred.home_win_prob)
        auc = roc_auc_score(
            pd_pred.actual_home_win, pd_pred.home_win_prob)

        print(f"\n{'='*55}")
        print(f"ML MODEL {test_season}: Home Win Probability ({len(FEATURES)} feats)")
        print(f"{'='*55}")
        print(f"  Raw ML: {ml_c}-{ml_i} ({100*ml_c/len(pd_pred):.1f}%)")
        print(f"  Log Loss: {log_loss_val:.4f} | Brier: {brier:.4f} | AUC: {auc:.3f}")
        print(f"  Value bets: {val_wins}/{val_bets} ({100*val_wins/max(val_bets,1):.1f}%)")
        print(f"  Home win rate: {pd_pred.actual_home_win.mean():.1%}")
        print(f"  Mean pred prob: {pd_pred.home_win_prob.mean():.1%}")
        print(f"\n  Weekly:")
        for r in weekly:
            print(f"    W{r['week']:2d}: n={r['n']:2d} Acc={r['acc']:.1%} Brier={r['brier']:.4f}")

        imp = pd.DataFrame(
            {'f': FEATURES, 'v': m.feature_importances_}
        ).sort_values('v', ascending=False)
        print(f"\n  Feature importance:")
        for _, r in imp.iterrows():
            print(f"    {r.f:>22s}: {r.v:.3f} {'█'*int(r.v*100)}{'░'*(20-int(r.v*100))}")

        # Save model
        with open(f'/app/data/handicap_model_ml_{test_season}.pkl', 'wb') as f:
            pickle.dump(calibrator, f)
        with open('/app/data/handicap_model_ml_full.pkl', 'wb') as f:
            pickle.dump(calibrator, f)
        log(f"ML model saved")

        # Save results JSON
        import json as _json
        result_entry = {
            "test_year": test_season,
            "train_years": train_years,
            "total_games": len(pd_pred),
            "accuracy": round(ml_c / max(ml_i + ml_c, 1), 4),
            "log_loss": round(log_loss_val, 4),
            "brier": round(brier, 4),
            "auc": round(auc, 4),
            "val_bets": val_bets,
            "val_wins": val_wins,
            "val_pct": round(100 * val_wins / max(val_bets, 1), 1),
            "ml": {"correct": int(ml_c), "incorrect": int(ml_i),
                   "total": len(pd_pred), "pct": round(100*ml_c/len(pd_pred), 1)},
            "feature_importance": [
                {"feature": r.f, "importance": round(r.v, 4)}
                for _, r in imp.iterrows()],
        }
        results_path = "/app/data/ml_backtest_results.json"
        try:
            with open(results_path) as f:
                existing = _json.load(f)
        except Exception:
            existing = []
        existing = [r for r in existing if r.get("test_year") != test_season]
        existing.append(result_entry)
        with open(results_path, 'w') as f:
            _json.dump(existing, f, indent=2)
        log(f"Done ML {test_season} in {__import__('time').time()-t0:.0f}s")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
