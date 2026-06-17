"""
OU-optimized XGBoost model — predicts total points.

Single source of truth for features, training, and live inference.
Can be imported (predict_total, set_model_path) or run standalone for training.

    python nfl_xgb_model_ou.py                    # test 2021
    python nfl_xgb_model_ou.py --test-year 2025   # test one year
    python nfl_xgb_model_ou.py --all              # test 2021-2025
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
from sklearn.metrics import mean_absolute_error
import xgboost as xgb

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("earl.xgb_ou")
log = logger.info

# ── Paths & connections ────────────────────────────────────────────────

MODEL_PATH = Path("/app/data/ou_model_full.pkl")
DB = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DSN = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football").replace("+asyncpg", "")

_model = None  # cached loaded model


# ── FEATURES — single source of truth ─────────────────────────────────

FEATURES = [
    # Market anchors (3)
    'opening_ou', 'spread', 'ou_movement',
    # Opponent-adjusted scoring diffs (5)
    'dpf', 'dpa', 'himp', 'aimp', 'dimp',
    # Short-term form (4)
    'home_win_pct_r5', 'away_win_pct_r5',
    'home_margin_r3', 'away_margin_r3',
    # Long-term identity (2)
    'home_margin_r10', 'away_margin_r10',
    # Situational (8)
    'rest_diff', 'travel_miles', 'tz_diff', 'is_short',
    'is_dome', 'temp', 'wind',
]  # 22 total

assert len(FEATURES) == 21, f"OU FEATURES count changed to {len(FEATURES)}"


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


# ── Model management ───────────────────────────────────────────────────

def set_model_path(model_path: str):
    """Override model path (used by admin panel)."""
    global MODEL_PATH, _model
    MODEL_PATH = Path(model_path)
    _model = None
    log(f"OU model path set to {model_path}")


def _load_model():
    """Load and cache the pickled XGBoost model."""
    global _model
    if _model is not None:
        return _model
    with open(MODEL_PATH, "rb") as f:
        _model = pickle.load(f)
    log(f"Loaded OU XGBoost model ({_model.n_features_in_} features)")
    return _model


# ── Inference: predict for a single game (imported by engine.py) ──────

async def predict_total(db, game_id, home_abbr, away_abbr, yr, wk,
                         home_stats, away_stats, line, season_avg_pts):
    """
    Predict total points for one game. Uses the *same* FEATURES list and
    *same* feature order as the batch training pipeline.

    Returns (predicted_total, confidence).
    """
    model = _load_model()
    conn = None
    try:
        conn = await asyncpg.connect(DSN)

        hpf = float(home_stats.ppg_for); hpa = float(home_stats.ppg_against)
        apf = float(away_stats.ppg_for); apa = float(away_stats.ppg_against)
        ou = float(line.over_under if (line and line.over_under) else season_avg_pts * 2)

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
            "SELECT spread, opening_spread, over_under, opening_ou "
            "FROM nfl.game_lines WHERE game_id=$1", game_id)
        spread_val = float(gl['spread']) if (gl and gl['spread'] is not None) else 0.0
        opening_ou = float(gl['opening_ou']) if (gl and gl['opening_ou'] is not None) else 0.0
        closing_ou = float(gl['over_under']) if (gl and gl['over_under'] is not None) else 0.0
        ou_mvmt_val = closing_ou - opening_ou

        # ── Form + momentum ──
        h_win_pct = await _avg_last(home_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int ELSE (g.away_score>g.home_score)::int END", 5)
        a_win_pct = await _avg_last(away_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int ELSE (g.away_score>g.home_score)::int END", 5)
        h_margin_r3 = await _avg_last(home_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 3)
        a_margin_r3 = await _avg_last(away_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 3)
        h_margin_r10 = await _avg_last(home_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 10)
        a_margin_r10 = await _avg_last(away_abbr,
            "CASE WHEN ht.abbreviation=$1 THEN g.home_score-g.away_score ELSE g.away_score-g.home_score END", 10)

        # ── Rest / weather from games table ──
        gr = await conn.fetchrow(
            "SELECT date::date, roof_type, temperature, wind_speed "
            "FROM nfl.games WHERE id=$1", game_id)
        gd = gr[0] if gr else None
        is_short = 1 if (gd and isinstance(gd, date) and gd.weekday() in (3, 4, 5)) else 0
        is_dome = 1 if (gr and gr[1] == 'dome') else 0
        temp = float(gr[2]) if (gr and gr[2] is not None) else 0.0
        wind = float(gr[3]) if (gr and gr[3] is not None) else 0.0

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
        hr_days = (gd - hl).days if (gd and hl) else 7
        ar_days = (gd - al).days if (gd and al) else 7
        rest_diff = hr_days - ar_days

        # ── Travel + timezone ──
        hc = COORDS.get(home_abbr, (0, 0)); ac = COORDS.get(away_abbr, (0, 0))
        travel_miles = haversine(hc[0], hc[1], ac[0], ac[1])
        if travel_miles < 50: travel_miles = 0
        tz_diff = TZ.get(home_abbr, -5) - TZ.get(away_abbr, -5)

        # ── Build feature vector (MUST match FEATURES order) ──
        feats = np.array([[
            opening_ou, spread_val, ou_mvmt_val,
            dpf, dpa, himp, aimp, dimp,
            h_win_pct, a_win_pct,
            h_margin_r3, a_margin_r3,
            h_margin_r10, a_margin_r10,
            rest_diff, travel_miles, tz_diff, is_short,
            is_dome, temp, wind,
        ]], dtype=np.float32)

        predicted_total = float(model.predict(feats)[0])
        conf = min(0.5 + abs(predicted_total - closing_ou) / 28, 0.90)
        return round(predicted_total, 1), round(conf, 2)

    except Exception as e:
        logger.error(f"OU pred failed [{game_id} {home_abbr}@{away_abbr}]: {e}")
        pred_total = (hpf + apa + apf + hpa) / 2
        return round(pred_total, 1), 0.50
    finally:
        if conn:
            await conn.close()


# ── Batch training & backtesting ──────────────────────────────────────

async def run():
    """Entry point for standalone training / backtesting."""
    import argparse
    parser = argparse.ArgumentParser(description="Train & backtest OU model")
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
                   (g.home_score + g.away_score) as total,
                   (g.home_score - g.away_score) as margin,
                   g.roof_type, g.temperature, g.wind_speed,
                   ht.id as home_team_id, at.id as away_team_id
            FROM games g JOIN seasons s ON s.id=g.season_id
            JOIN teams ht ON ht.id=g.home_team_id JOIN teams at ON at.id=g.away_team_id
            WHERE g.game_type='REG' AND g.home_score IS NOT NULL
            ORDER BY s.year, g.week, g.date
        """))
        df = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])

        r = await conn.execute(text("""
            SELECT game_id, spread, over_under, opening_spread, opening_ou
            FROM nfl.game_lines
        """))
        lines = {r.game_id: {
            'sp': r.spread or 0, 'ou': r.over_under or 0,
            'open_sp': r.opening_spread or 0, 'open_ou': r.opening_ou or 0,
        } for r in r.fetchall()}

        r = await conn.execute(text("""
            SELECT game_id, over_under - opening_ou as ou_mvmt
            FROM nfl.game_lines WHERE opening_ou IS NOT NULL
        """))
        ou_mvmt_map = {r.game_id: float(r.ou_mvmt or 0) for r in r.fetchall()}

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

    log(f"{len(df)} games ({df.season.min()}-{df.season.max()})")

    # ── Build team-game table ──
    log("Building team-game table...")
    ou_lookup = {r.id: lines.get(r.id, {}).get('ou', df.total.mean())
                 for _, r in df.iterrows()}
    rows = []
    for _, g in df.iterrows():
        went_over = 1 if g.total > ou_lookup[g.id] else 0
        rows.append({'game_id': g.id, 'season': g.season, 'week': g.week,
            'game_date': g.game_date, 'team': g.ha, 'opp': g.aa,
            'tid': g.home_team_id, 'oid': g.away_team_id,
            'pf': g.home_score, 'pa': g.away_score,
            'is_home': 1, 'margin': g.margin, 'total': g.total,
            'over': went_over, 'roof': g.roof_type,
            'temp': g.temperature, 'wind': g.wind_speed})
        rows.append({'game_id': g.id, 'season': g.season, 'week': g.week,
            'game_date': g.game_date, 'team': g.aa, 'opp': g.ha,
            'tid': g.away_team_id, 'oid': g.home_team_id,
            'pf': g.away_score, 'pa': g.home_score,
            'is_home': 0, 'margin': -g.margin, 'total': g.total,
            'over': went_over, 'roof': g.roof_type,
            'temp': g.temperature, 'wind': g.wind_speed})
    tg = pd.DataFrame(rows).sort_values(
        ['team','season','week','game_date']).reset_index(drop=True)

    # ── Rolling features ──
    log("Rolling features...")
    tg['pf_r5'] = tg.groupby('team')['pf'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['pa_r5'] = tg.groupby('team')['pa'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['won'] = tg['margin'] > 0
    tg['win_r5'] = tg.groupby('team')['won'].transform(
        lambda x: x.shift(1).rolling(5, min_periods=0).mean())
    tg['margin_r3'] = tg.groupby('team')['margin'].transform(
        lambda x: x.shift(1).rolling(3, min_periods=0).mean())
    tg['margin_r10'] = tg.groupby('team')['margin'].transform(
        lambda x: x.shift(1).rolling(10, min_periods=0).mean())

    # ── Opponent-adjusted PPG (for dpf/dpa) ──
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
    log("Merging home/away + situational...")
    h = tg[tg.is_home == 1][['game_id','opp','roof','temp','wind',
        'season','week','game_date','tid','oid',
        'hpf_adj','hpa_adj','win_r5','margin_r3','margin_r10']].rename(
        columns={'opp':'ateam','tid':'htid','oid':'atid',
            'hpf_adj':'hpf','hpa_adj':'hpa',
            'win_r5':'home_win_pct_r5',
            'margin_r3':'home_margin_r3','margin_r10':'home_margin_r10'})
    a = tg[tg.is_home == 0][['game_id','opp','hpf_adj','hpa_adj',
        'win_r5','margin_r3','margin_r10']].rename(
        columns={'opp':'hteam','hpf_adj':'apf','hpa_adj':'apa',
            'win_r5':'away_win_pct_r5',
            'margin_r3':'away_margin_r3','margin_r10':'away_margin_r10'})
    feats = h.merge(a, on='game_id')
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
    feats['spread'] = feats.game_id.map(lambda x: lines.get(x, {}).get('sp', 0))
    feats['ou_movement'] = feats.game_id.map(lambda x: ou_mvmt_map.get(x, 0))
    feats['opening_ou'] = feats.game_id.map(
        lambda x: lines.get(x, {}).get('open_ou', 0))

    # ── Situational ──
    tg2 = tg.sort_values(['team','season','week','game_date']).copy()
    tg2['game_date'] = pd.to_datetime(tg2['game_date'])
    tg2['prev_date'] = tg2.groupby('team')['game_date'].shift(1)
    tg2['rest'] = (tg2.game_date - tg2.prev_date).dt.days.fillna(7).clip(0, 21)
    hr = tg2[tg2.is_home==1][['game_id','rest']].rename(columns={'rest':'hr'})
    ar_ = tg2[tg2.is_home==0][['game_id','rest']].rename(columns={'rest':'ar'})
    feats = feats.merge(hr, on='game_id').merge(ar_, on='game_id')
    feats['rest_diff'] = feats.hr - feats.ar
    feats['travel_miles'] = feats.apply(
        lambda r: haversine(*COORDS.get(r.ateam,(0,0)),*COORDS.get(r.hteam,(0,0))), axis=1)
    feats.loc[feats.travel_miles < 50, 'travel_miles'] = 0
    feats['tz_diff'] = feats.apply(
        lambda r: TZ.get(r.hteam,-5)-TZ.get(r.ateam,-5), axis=1)
    feats['game_date'] = pd.to_datetime(feats['game_date'])
    feats['is_short'] = feats.game_date.dt.dayofweek.isin([3, 4, 5]).astype(int)
    feats['is_dome'] = (feats.roof == 'dome').astype(int)
    feats['temp'] = feats['temp'].fillna(0)
    feats['wind'] = feats['wind'].fillna(0)

    # ── Target ──
    feats['actual_total'] = feats.game_id.map(dict(zip(df.id, df.total)))

    log(f"{len(feats)} total feature rows ({len(FEATURES)} features)")

    # ── Train/Test ──
    test_years = [2021, 2022, 2023, 2024, 2025] if args.all else [args.test_year]

    for test_season in test_years:
        log(f"\n{'='*55}")
        log(f"OU MODEL: Testing {test_season}")
        log(f"{'='*55}")
        train_years = [test_season - i for i in range(1, args.train_window + 1)]
        tr_all = feats[feats.season.isin(train_years)].reset_index(drop=True)
        te_all = feats[feats.season == test_season].sort_values('week').reset_index(drop=True)
        max_wk = te_all.week.max()
        log(f"W1-W{max_wk}: train={len(tr_all)} ({train_years}), test={len(te_all)}")

        weekly, preds_all = [], []

        log(f"Training OU model on {len(tr_all)} games")
        w = np.ones(len(tr_all))
        for i in range(len(tr_all)):
            s = tr_all.at[tr_all.index[i], 'season']
            wk_v = tr_all.at[tr_all.index[i], 'week']
            if s == test_season: w[i] = 4.0 + (wk_v/max_wk)*2.0
            elif s == test_season - 1: w[i] = 3.0
            elif s == test_season - 2: w[i] = 2.0
            w[i] *= 1.0 + (wk_v/18)*0.5

        X_tr = tr_all[FEATURES].fillna(0).values.astype(np.float32)
        y_tr = tr_all.actual_total.values
        m = xgb.XGBRegressor(n_estimators=300, max_depth=5, learning_rate=0.04,
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
            y_te = te.actual_total.values
            p = m.predict(X_te)
            for i, gid in enumerate(te.game_id.values):
                preds_all.append({
                    'game_id': gid, 'week': wk,
                    'pred_total': p[i], 'actual_total': y_te[i]})
            mae = mean_absolute_error(y_te, p)
            log(f"  W{wk:2d}: test={len(te):2d} MAE={mae:.2f}")
            weekly.append({'week': wk, 'n': len(te), 'mae': round(mae, 2)})
            del X_te, y_te, p
        gc.collect()

        if not preds_all:
            continue
        pd_pred = pd.DataFrame(preds_all)

        # ── Evaluate ──
        omae = mean_absolute_error(pd_pred.actual_total, pd_pred.pred_total)
        errs = pd_pred.actual_total - pd_pred.pred_total

        async with engine.connect() as conn:
            r = await conn.execute(text(
                "SELECT game_id, over_under FROM nfl.game_lines"))
            sm = {r.game_id: r.over_under for r in r.fetchall()}
        pd_pred['ou'] = pd_pred.game_id.map(sm)
        ook = pd_pred.ou.notna()
        odf = pd_pred[ook].copy()
        diff = (odf.actual_total - odf.ou).abs()
        ou_push = (diff < 0.5).sum()
        ou_pred_over = odf.pred_total > odf.ou
        ou_actual_over = odf.actual_total > odf.ou
        ou_correct = ((ou_pred_over == ou_actual_over) & (diff >= 0.5)).sum()
        ou_incorrect = len(odf) - ou_correct - ou_push
        ou_pct = round(100 * ou_correct / max(ou_correct + ou_incorrect, 1), 1)

        # Heuristic: simple PPG blend
        h_load = pd_pred.merge(
            feats[['game_id','hpf','hpa','apf','apa']], on='game_id', how='left')
        h_load['heur_total'] = (h_load['hpf'] + h_load['apa']
                                + h_load['apf'] + h_load['hpa']) / 2
        h_ook = h_load.ou.notna()
        h_over = h_load.heur_total > h_load.ou
        h_actual = h_load.actual_total > h_load.ou
        h_diff = (h_load.actual_total - h_load.ou).abs()
        h_correct = ((h_over == h_actual) & (h_diff >= 0.5)).sum()
        h_incorrect = len(h_load[h_ook]) - h_correct - h_diff[h_ook].apply(
            lambda x: x < 0.5).sum()
        h_push = int(h_diff.apply(lambda x: x < 0.5).sum())
        h_pct = round(100 * h_correct / max(h_correct + h_incorrect, 1), 1)
        del h_load

        pred_std = float(pd_pred.pred_total.std())
        natural_over = float((pd_pred.actual_total > pd_pred.ou).mean())
        valid = pd_pred[pd_pred.ou.notna()]
        corr_total = float(np.corrcoef(
            valid.pred_total, valid.actual_total)[0, 1]) if len(valid) > 5 else 0.0

        print(f"\n{'='*55}")
        print(f"OU MODEL {test_season}: {len(FEATURES)} features")
        print(f"{'='*55}")
        print(f"  MAE: {omae:.2f} | err={errs.mean():.2f} | sigma={errs.std():.2f}")
        print(f"  +/-3pts: {(abs(errs)<3).mean():.1%} | +/-7pts: {(abs(errs)<7).mean():.1%}")
        print(f"  Corr(pred, actual): {corr_total:.3f}")
        print(f"  Pred Std: {pred_std:.1f}")
        print(f"  OU: {ou_correct}-{ou_incorrect}-{ou_push} ({ou_pct}%)")
        print(f"  Heuristic: {h_correct}-{h_incorrect}-{h_push} ({h_pct}%)")
        print(f"  Natural over rate: {natural_over:.1%}")
        print(f"\n  Weekly:")
        for r in weekly:
            print(f"    W{r['week']:2d}: n={r['n']:2d} MAE={r['mae']:.2f}")

        imp = pd.DataFrame(
            {'f': FEATURES, 'v': final_model.feature_importances_}
        ).sort_values('v', ascending=False)
        print(f"\n  Feature importance:")
        for _, r in imp.iterrows():
            pct = r.v * 100
            print(f"    {r.f:>20s}: {pct:.1f}%  {'█'*int(pct)}{'░'*(20-int(pct))}")

        # Save model
        with open(f'/app/data/ou_model_{test_season}.pkl', 'wb') as f:
            pickle.dump(final_model, f)
        with open('/app/data/ou_model_full.pkl', 'wb') as f:
            pickle.dump(final_model, f)
        log(f"OU model saved")

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
            "pred_std": round(pred_std, 1),
            "ou": {"correct": int(ou_correct), "incorrect": int(ou_incorrect),
                   "pushes": int(ou_push), "total": int(ook.sum()),
                   "pct": round(ou_pct, 1)},
            "heuristic_ou_pct": round(h_pct, 1),
            "natural_over_rate": round(float(natural_over), 3),
            "correlation": round(corr_total, 3),
            "feature_importance": [
                {"feature": r.f, "importance": round(r.v, 4)}
                for _, r in imp.iterrows()],
        }
        results_path = "/app/data/ou_results_baseline.json"
        try:
            with open(results_path) as f:
                existing = _json.load(f)
        except Exception:
            existing = []
        existing = [r for r in existing if r.get("test_year") != test_season]
        existing.append(result_entry)
        with open(results_path, 'w') as f:
            _json.dump(existing, f, indent=2)
        log(f"Done OU {test_season} in {__import__('time').time()-t0:.0f}s")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
