"""
NBA XGBoost ATS Backtester — Opponent-adjusted scoring + streaks.

Features:
  - Opponent-adjusted offensive/defensive rating (rolling 10/20)
  - Rest / back-to-back
  - Moneyline implied probabilities, spread, spread movement, implied margin
  - ATS wins in last 5, straight-up wins in last 5/10

Trained only on 2020+ games with complete line data.
Results saved to /app/data/nba_backtest_results.json for admin page.

Usage:
    docker exec earl-knows-football-api-1 python -m app.handicapping.nba.nba_xgb_model_ats --mode all
"""
import asyncio, logging, warnings, json, math, os, pickle
from datetime import datetime, date
from typing import Optional
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from sklearn.metrics import mean_absolute_error
import xgboost as xgb

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("earl.nba_xgb_ats")
log = logger.info

DB = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DSN = DB.replace("+asyncpg", "")

COORDS = {
    "ATL": (33.8, -84.4), "BKN": (40.7, -73.9), "BOS": (42.4, -71.1),
    "CHA": (35.2, -80.8), "CHI": (41.9, -87.6), "CLE": (41.5, -81.7),
    "DAL": (32.8, -96.8), "DEN": (39.7, -104.9), "DET": (42.3, -83.0),
    "GSW": (37.8, -122.4), "HOU": (29.8, -95.4), "IND": (39.8, -86.2),
    "LAC": (34.0, -118.3), "LAL": (34.0, -118.3), "MEM": (35.1, -90.0),
    "MIA": (25.8, -80.2), "MIL": (43.0, -87.9), "MIN": (45.0, -93.3),
    "NOP": (29.9, -90.1), "NYK": (40.8, -73.9), "OKC": (35.5, -97.5),
    "ORL": (28.5, -81.4), "PHI": (39.9, -75.2), "PHX": (33.4, -112.1),
    "POR": (45.5, -122.7), "SAC": (38.6, -121.5), "SAS": (29.4, -98.5),
    "TOR": (43.6, -79.4), "UTA": (40.8, -112.0), "WAS": (38.9, -77.0),
}

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
RESULTS_DIR = Path("/app/data")

FEATURES = [
    # Opponent-adjusted scoring
    "h_adj_off_10", "h_adj_def_10", "a_adj_off_10", "a_adj_def_10",
    "h_adj_off_20", "h_adj_def_20", "a_adj_off_20", "a_adj_def_20",
    # Rest & Travel
    "rest_h", "rest_a", "rest_diff", "home_b2b", "away_b2b",
    "travel_miles",
    # Betting market
    "h_implied", "a_implied", "spread", "spread_movement",
    "implied_margin", "ml_spread_mismatch",
    # Form & streaks
    "h_ats_wins_5", "a_ats_wins_5",
    "h_ats_margin_5", "a_ats_margin_5",
    "h_wins_5", "h_wins_10", "a_wins_5", "a_wins_10",
]

FEATURE_DESCRIPTIONS = {
    "h_adj_off_10": "Home opponent-adjusted offense, rolling 10 (positive = scores above opponent avg allowed)",
    "h_adj_def_10": "Home opponent-adjusted defense, rolling 10 (negative = holds opponents below their avg)",
    "a_adj_off_10": "Away opponent-adjusted offense, rolling 10",
    "a_adj_def_10": "Away opponent-adjusted defense, rolling 10",
    "h_adj_off_20": "Home opponent-adjusted offense, rolling 20",
    "h_adj_def_20": "Home opponent-adjusted defense, rolling 20",
    "a_adj_off_20": "Away opponent-adjusted offense, rolling 20",
    "a_adj_def_20": "Away opponent-adjusted defense, rolling 20",
    "rest_h": "Home team rest days since last game",
    "rest_a": "Away team rest days since last game",
    "rest_diff": "Rest days advantage (home - away)",
    "home_b2b": "Binary: 1 if home team on back-to-back",
    "away_b2b": "Binary: 1 if away team on back-to-back",
    "travel_miles": "Away team travel distance in miles (haversine)",
    "h_implied": "Home team implied win probability from moneyline",
    "a_implied": "Away team implied win probability from moneyline",
    "spread": "Closing point spread (negative = home favorite)",
    "spread_movement": "Spread movement: opening - closing",
    "implied_margin": "Expected point margin from moneyline implied probability",
    "ml_spread_mismatch": "Disagreement between ML-implied margin and closing spread",
    "h_ats_wins_5": "Home team ATS wins in last 5 games (count 0-5)",
    "a_ats_wins_5": "Away team ATS wins in last 5 games (count 0-5)",
    "h_ats_margin_5": "Home team avg ATS cover margin last 5 games (+ = covering by that much)",
    "a_ats_margin_5": "Away team avg ATS cover margin last 5 games",
    "h_wins_5": "Home team straight-up wins in last 5 games",
    "h_wins_10": "Home team straight-up wins in last 10 games",
    "a_wins_5": "Away team straight-up wins in last 5 games",
    "a_wins_10": "Away team straight-up wins in last 10 games",
}

FEATURE_CATEGORIES = {
    "Opponent-Adjusted Scoring": ["h_adj_off_10", "h_adj_def_10", "a_adj_off_10", "a_adj_def_10",
                                  "h_adj_off_20", "h_adj_def_20", "a_adj_off_20", "a_adj_def_20"],
    "Rest & Travel": ["rest_h", "rest_a", "rest_diff", "home_b2b", "away_b2b", "travel_miles"],
    "Betting Market": ["h_implied", "a_implied", "spread", "spread_movement",
                       "implied_margin", "ml_spread_mismatch"],
    "Form & Streaks": ["h_ats_wins_5", "a_ats_wins_5",
                       "h_ats_margin_5", "a_ats_margin_5",
                       "h_wins_5", "h_wins_10", "a_wins_5", "a_wins_10"],
}


# ── Helpers ──
def _ml_implied(ml):
    if ml is None or ml == 0: return 0.5
    return abs(ml) / (abs(ml) + 100) if ml < 0 else 100.0 / (ml + 100)

def _implied_margin(h_implied):
    eps = 1e-6; p = np.clip(h_implied, eps, 1 - eps)
    return 2.35 * np.log(p / (1 - p))

def _ats_cover(margin, spread, is_home):
    if is_home: return 1 if margin > -spread else 0
    return 1 if margin < -spread else 0


# ── Data Loading ──
async def load_data(engine) -> pd.DataFrame:
    log("Loading games from 2020+ with betting lines...")
    async with engine.connect() as conn:
        r = await conn.execute(text("""
            SELECT g.id as game_id, s.year, g.date::date as game_date,
                   ht.abbreviation as ha, at.abbreviation as aa,
                   g.home_score, g.away_score,
                   (g.home_score - g.away_score) as margin, g.game_type,
                   g.home_field_goals_made as h_fgm,
                   g.home_field_goals_attempted as h_fga,
                   g.home_three_points_made as h_3pm,
                   g.home_three_points_attempted as h_3pa,
                   g.home_rebounds as h_reb, g.home_assists as h_ast,
                   g.away_field_goals_made as a_fgm,
                   g.away_field_goals_attempted as a_fga,
                   g.away_three_points_made as a_3pm,
                   g.away_three_points_attempted as a_3pa,
                   g.away_rebounds as a_reb, g.away_assists as a_ast,
                   blc.spread, blc.opening_spread, blc.over_under,
                   blc.home_moneyline, blc.away_moneyline,
                   blc.home_implied_probability, blc.away_implied_probability
            FROM nba.games g
            JOIN nba.seasons s ON s.id = g.season_id
            JOIN nba.teams ht ON ht.id = g.home_team_id
            JOIN nba.teams at ON at.id = g.away_team_id
            LEFT JOIN nba.betting_lines_consolidated blc ON blc.game_id = g.id
            WHERE g.home_score IS NOT NULL AND g.away_score IS NOT NULL AND s.year >= 2020
            ORDER BY s.year, g.date, g.id
        """))
        games = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])
    log(f"  Loaded {len(games)} games ({games.year.min()}-{games.year.max()})")
    has_line = games["home_moneyline"].notna() & games["spread"].notna()
    games = games[has_line].copy()
    log(f"  With complete line data: {len(games)} (dropped {(~has_line).sum()})")
    return games


# ── Feature Engineering ──
def build_features(df: pd.DataFrame) -> pd.DataFrame:
    log("Building team-game table...")
    rows = []
    for _, g in df.iterrows():
        rows.append({"game_id": g.game_id, "year": g.year, "game_date": g.game_date,
            "team": g.ha, "opp": g.aa, "pf": g.home_score, "pa": g.away_score,
            "is_home": 1, "spread": g.spread})
        rows.append({"game_id": g.game_id, "year": g.year, "game_date": g.game_date,
            "team": g.aa, "opp": g.ha, "pf": g.away_score, "pa": g.home_score,
            "is_home": 0, "spread": g.spread})
    tg = pd.DataFrame(rows).sort_values(["team", "game_date", "game_id"]).reset_index(drop=True)
    tg["ats_cover"] = tg.apply(lambda r: _ats_cover(r["pf"]-r["pa"], r["spread"], r["is_home"]==1), axis=1)
    tg["is_win"] = (tg["pf"] > tg["pa"]).astype(int)

    # Step 1: Compute per-team rolling PPG and OPPG (needed for opponent adjustment)
    log("  Computing team rolling PPG/OPPG (10/20)...")
    for w in [10, 20]:
        tg[f"ppg{w}"] = tg.groupby("team")["pf"].transform(lambda x: x.shift(1).rolling(w, min_periods=1).mean())
        tg[f"oppg{w}"] = tg.groupby("team")["pa"].transform(lambda x: x.shift(1).rolling(w, min_periods=1).mean())

    # Step 2: Map opponent's rolling PPG/OPPG into each row
    # For each game, the away team's row has the opponent's (home team's) rolling stats
    away_rows = tg[tg["is_home"] == 0].set_index("game_id")
    home_rows = tg[tg["is_home"] == 1].set_index("game_id")

    # For home team rows: opponent's stats come from away team row for same game
    tg.loc[tg["is_home"] == 1, "opp_ppg10"] = tg.loc[tg["is_home"] == 1, "game_id"].map(away_rows["ppg10"])
    tg.loc[tg["is_home"] == 1, "opp_oppg10"] = tg.loc[tg["is_home"] == 1, "game_id"].map(away_rows["oppg10"])
    tg.loc[tg["is_home"] == 1, "opp_ppg20"] = tg.loc[tg["is_home"] == 1, "game_id"].map(away_rows["ppg20"])
    tg.loc[tg["is_home"] == 1, "opp_oppg20"] = tg.loc[tg["is_home"] == 1, "game_id"].map(away_rows["oppg20"])

    # For away team rows: opponent's stats come from home team row for same game
    tg.loc[tg["is_home"] == 0, "opp_ppg10"] = tg.loc[tg["is_home"] == 0, "game_id"].map(home_rows["ppg10"])
    tg.loc[tg["is_home"] == 0, "opp_oppg10"] = tg.loc[tg["is_home"] == 0, "game_id"].map(home_rows["oppg10"])
    tg.loc[tg["is_home"] == 0, "opp_ppg20"] = tg.loc[tg["is_home"] == 0, "game_id"].map(home_rows["ppg20"])
    tg.loc[tg["is_home"] == 0, "opp_oppg20"] = tg.loc[tg["is_home"] == 0, "game_id"].map(home_rows["oppg20"])

    # Step 3: Per-game raw adjustment (points scored vs opponent's defense, points allowed vs opponent's offense)
    tg["raw_adj_off_10"] = tg["pf"] - tg["opp_oppg10"]
    tg["raw_adj_def_10"] = tg["pa"] - tg["opp_ppg10"]
    tg["raw_adj_off_20"] = tg["pf"] - tg["opp_oppg20"]
    tg["raw_adj_def_20"] = tg["pa"] - tg["opp_ppg20"]

    # Step 4: Rolling averages of raw adjustments (10/20 games, no look-ahead)
    log("  Rolling opponent-adjusted stats (10/20)...")
    for w in [10, 20]:
        for metric in ["off", "def"]:
            col = f"adj_{metric}_{w}"
            raw = f"raw_adj_{metric}_{w}"
            tg[col] = tg.groupby("team")[raw].transform(lambda x: x.shift(1).rolling(w, min_periods=1).mean())

    # Rest days
    log("  Rest & streaks...")
    tg["game_date_dt"] = pd.to_datetime(tg["game_date"])
    tg["prev_date"] = tg.groupby("team")["game_date_dt"].shift(1)
    tg["rest"] = (tg["game_date_dt"] - tg["prev_date"]).dt.days.fillna(2).clip(0, 30)

    # Wins & ATS wins
    # ATS cover margin: positive = covered, negative = didn't. Sign-adjusted per team perspective.
    # Home covers if margin > -spread. Home cover margin = margin + spread.
    # Away cover margin = -(margin + spread).
    # So for any team: cover_margin = (margin + spread) * (1 if is_home else -1)
    tg["cover_margin"] = (tg["pf"] - tg["pa"] + tg["spread"]) * np.where(tg["is_home"] == 1, 1, -1)

    tg["ats_wins_5"] = tg.groupby("team")["ats_cover"].transform(lambda x: x.shift(1).rolling(5, min_periods=1).sum())
    tg["ats_margin_5"] = tg.groupby("team")["cover_margin"].transform(lambda x: x.shift(1).rolling(5, min_periods=1).mean())
    tg["wins_5"] = tg.groupby("team")["is_win"].transform(lambda x: x.shift(1).rolling(5, min_periods=1).sum())
    tg["wins_10"] = tg.groupby("team")["is_win"].transform(lambda x: x.shift(1).rolling(10, min_periods=1).sum())

    # Step 5: Join home/away back to game-level features
    log("  Assembling game-level features...")
    h_cols = ["game_id","team","opp","rest","ats_wins_5","ats_margin_5","wins_5","wins_10",
              "adj_off_10","adj_def_10","adj_off_20","adj_def_20"]
    a_cols = ["game_id","team","opp","rest","ats_wins_5","ats_margin_5","wins_5","wins_10",
              "adj_off_10","adj_def_10","adj_off_20","adj_def_20"]

    h = tg[tg["is_home"]==1][h_cols].rename(columns={
        "team":"ha","opp":"aa","rest":"rest_h",
        "ats_wins_5":"h_ats_wins_5","ats_margin_5":"h_ats_margin_5",
        "wins_5":"h_wins_5","wins_10":"h_wins_10",
        "adj_off_10":"h_adj_off_10","adj_def_10":"h_adj_def_10",
        "adj_off_20":"h_adj_off_20","adj_def_20":"h_adj_def_20"})
    a = tg[tg["is_home"]==0][a_cols].rename(columns={
        "team":"aa","opp":"ha","rest":"rest_a",
        "ats_wins_5":"a_ats_wins_5","ats_margin_5":"a_ats_margin_5",
        "wins_5":"a_wins_5","wins_10":"a_wins_10",
        "adj_off_10":"a_adj_off_10","adj_def_10":"a_adj_def_10",
        "adj_off_20":"a_adj_off_20","adj_def_20":"a_adj_def_20"})

    feats = h.merge(a, on="game_id", suffixes=("_h","_a"))
    feats["ha"] = feats["ha_h"]; feats["aa"] = feats["aa_h"]
    feats["rest_diff"] = feats["rest_h"] - feats["rest_a"]
    feats["home_b2b"] = (feats["rest_h"]==0).astype(int)
    feats["away_b2b"] = (feats["rest_a"]==0).astype(int)

    feats["travel_miles"] = feats.apply(
        lambda r: haversine(*COORDS.get(r["aa"],(0,0)),*COORDS.get(r["ha"],(0,0))), axis=1)
    feats.loc[feats["travel_miles"] < 50, "travel_miles"] = 0.0

    orig = df[["game_id","year","game_date","margin","spread","opening_spread",
               "home_moneyline","away_moneyline","home_implied_probability","away_implied_probability"]].drop_duplicates("game_id")
    feats = feats.merge(orig, on="game_id", how="inner")
    feats["h_implied"] = feats["home_implied_probability"].fillna(feats.apply(lambda r: _ml_implied(r["home_moneyline"]), axis=1))
    feats["a_implied"] = feats["away_implied_probability"].fillna(feats.apply(lambda r: _ml_implied(r["away_moneyline"]), axis=1))
    feats["spread_movement"] = feats["opening_spread"].fillna(feats["spread"]) - feats["spread"]
    feats["implied_margin"] = feats["h_implied"].apply(_implied_margin)
    feats["ml_spread_mismatch"] = feats["implied_margin"] - (-feats["spread"])
    feats["actual_margin"] = feats["margin"]
    feats = feats.fillna(0)
    log(f"  Final: {len(feats)} rows, {len(FEATURES)} features")
    return feats


# ── Backtest ──
async def run_backtest(feats, test_year=2024, xgb_params=None):
    if xgb_params is None:
        xgb_params = {"n_estimators":350,"max_depth":5,"learning_rate":0.06,
            "subsample":0.75,"colsample_bytree":0.75,"reg_alpha":0.5,"reg_lambda":1.5,
            "random_state":42,"n_jobs":-1,"verbosity":0}
    tr = feats[(feats["year"]>=2020)&(feats["year"]<test_year)].reset_index(drop=True)
    te = feats[feats["year"]==test_year].reset_index(drop=True)
    log(f"\n{'─'*62}\nNBA BACKTEST — Test year {test_year}\n  Train: {len(tr)}g ({int(tr.year.min())}-{int(tr.year.max())})  Test: {len(te)}g")
    if len(tr)<100 or len(te)<10:
        return {"error":"insufficient_data","test_year":test_year}, pd.DataFrame(), None
    X_tr = tr[FEATURES].astype(np.float32); y_tr = tr["actual_margin"].values
    q01,q99 = np.percentile(y_tr,[1,99]); clip = (y_tr>=q01)&(y_tr<=q99)
    if (~clip).sum():
        tr = tr[clip].reset_index(drop=True); X_tr = tr[FEATURES].astype(np.float32); y_tr = tr["actual_margin"].values
        log(f"  Clipped {(~clip).sum()} outliers ({q01:.1f}-{q99:.1f})")
    w = np.ones(len(tr))
    for i in range(len(tr)):
        yb = test_year - tr.at[tr.index[i],"year"]
        if yb <= 1: w[i] = 4.0
        elif yb <= 2: w[i] = 3.0
        elif yb <= 3: w[i] = 2.0
        elif yb <= 5: w[i] = 1.5
    model = xgb.XGBRegressor(**xgb_params)
    model.fit(X_tr, y_tr, sample_weight=w, verbose=False)
    X_te = te[FEATURES].astype(np.float32); y_te = te["actual_margin"].values
    pred = model.predict(X_te)
    te = te.copy(); te["pred_margin"] = pred; te["pred_error"] = te["actual_margin"] - pred
    mae = mean_absolute_error(y_te, pred)
    rl = te[te["spread"].notna()].copy()
    rl["actual_ats_win"] = rl["actual_margin"] > -rl["spread"]
    rl["pred_ats_win"] = rl["pred_margin"] > -rl["spread"]
    # Exclude pushes entirely: can't win or lose on a push
    push_mask = (rl["actual_margin"] + rl["spread"]).abs() < 0.05
    non_push = rl[~push_mask]
    ats_corr = (non_push["actual_ats_win"] == non_push["pred_ats_win"]).sum()
    ats_inc = len(non_push) - ats_corr
    pushes = int(push_mask.sum())
    imp = pd.DataFrame({"feature":FEATURES,"importance":model.feature_importances_}).sort_values("importance",ascending=False)
    t = ats_corr+ats_inc+pushes
    result = {"test_year":test_year,"feature_set":"adj_scoring_streaks",
        "train_years":sorted(tr["year"].unique().tolist()),"total_games":int(len(te)),
        "mae":round(mae,2),"err_mean":round(te["pred_error"].mean(),2),"err_std":round(te["pred_error"].std(),2),
        "within_5":float(round((abs(te["pred_error"])<5).mean(),3)),
        "ats":{"correct":int(ats_corr),"incorrect":int(ats_inc),"pushes":int(pushes),"total":int(t),
               "pct":float(round(100*ats_corr/max(t,1),1))},
        "feature_importance":[{"feature":str(r["feature"]),"importance":float(round(float(r["importance"]),4))} for _,r in imp.iterrows()]}
    return result, te, model

def _ps(r):
    print(f"\n{'='*62}\nNBA BACKTEST — {r['test_year']}  ({r['feature_set']})  {len(r['feature_importance'])} feats")
    print(f"MAE: {r['mae']:.2f}  Bias: {r['err_mean']:+.2f}  ±5: {r['within_5']:.1%}")
    a=r['ats']; print(f"ATS: {a['correct']}-{a['incorrect']}-{a['pushes']} ({a['pct']:.1f}%) [{a['total']}g]")
    print("Top features:")
    for i,fi in enumerate(r['feature_importance'][:10]):
        print(f"  {i+1:2d}. {fi['feature']:>16s}: {fi['importance']:.4f}")
    print()

async def run_all():
    t0 = datetime.now(); engine = create_async_engine(DB)
    df = await load_data(engine); feats = build_features(df); await engine.dispose()
    results = []
    for yr in [2024,2025]:
        r,_,_ = await run_backtest(feats,test_year=yr)
        if "error" not in r: results.append(r)
    RESULTS_DIR.mkdir(parents=True,exist_ok=True)
    with open(RESULTS_DIR/"nba_backtest_results.json","w") as f: json.dump(results,f,indent=2,default=str)
    log(f"\n✅ Saved to {RESULTS_DIR/'nba_backtest_results.json'}\nTime: {datetime.now()-t0}")

async def run_single(yr=2024):
    engine = create_async_engine(DB); df = await load_data(engine); feats = build_features(df); await engine.dispose()
    r,_,_ = await run_backtest(feats,test_year=yr); return r


# ── Production Inference ──
ATS_MODEL_PATH = Path("/app/data/nba_margin_model_prod.pkl")
_ats_model = None
def set_model_path(p): global ATS_MODEL_PATH,_ats_model; ATS_MODEL_PATH=Path(p); _ats_model=None
def _load_ats_model():
    global _ats_model
    if _ats_model is not None: return _ats_model
    if not ATS_MODEL_PATH.exists(): raise FileNotFoundError(f"ATS model not found at {ATS_MODEL_PATH}")
    with open(ATS_MODEL_PATH,"rb") as f: payload=pickle.load(f)
    _ats_model = payload["model"] if isinstance(payload,dict) else payload; return _ats_model

async def predict_ats(game_id,home_abbr,away_abbr,yr,game_date,home_stats,away_stats,line_obj,conn=None):
    import asyncpg
    try: model = _load_ats_model()
    except: return None,0.0
    gd_obj = date.fromisoformat(game_date) if isinstance(game_date,str) else game_date; _cc=False
    if conn is None: conn=await asyncpg.connect(DSN); _cc=True
    try:
        async def _avg(abbr,sel,lim):
            r=await conn.fetchrow(f"SELECT AVG(sub.v) FROM (SELECT {sel} as v FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id JOIN nba.teams ht ON ht.id=g.home_team_id JOIN nba.teams at ON at.id=g.away_team_id WHERE (ht.abbreviation=$1 OR at.abbreviation=$1) AND (s.year<$2 OR (s.year=$2 AND g.date::date<$3)) AND g.home_score IS NOT NULL ORDER BY s.year DESC,g.date DESC LIMIT {lim}) sub",abbr,yr,gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.0

        # Team rolling PPG/OPPG (for opponent adjustment computation at inference time)
        h_ppg10 = await _avg(home_abbr,"CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END",10)
        h_oppg10 = await _avg(home_abbr,"CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END",10)
        a_ppg10 = await _avg(away_abbr,"CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END",10)
        a_oppg10 = await _avg(away_abbr,"CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END",10)
        h_ppg20 = await _avg(home_abbr,"CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END",20)
        h_oppg20 = await _avg(home_abbr,"CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END",20)
        a_ppg20 = await _avg(away_abbr,"CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END",20)
        a_oppg20 = await _avg(away_abbr,"CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END",20)

        # Opponent-adjusted: team's scoring vs what opponent typically allows
        # h_adj_off_10 = avg over last 10 games of (home_score - away_oppg)
        # But at inference we use current rolling averages as approximation
        async def _adj_roll(abbr, side, opp_oppg_var, opp_ppg_var, lim):
            opp_tbl = "at" if side=="h" else "ht"
            opp_side = "away" if side=="h" else "home"
            # For each prior game, compute (our_score - their_oppg) then average
            r=await conn.fetchrow(f"""
                SELECT AVG(sub.adj) FROM (
                    SELECT (CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END -
                            COALESCE(opp.oppg, 0)) as adj
                    FROM nba.games g
                    JOIN nba.seasons s ON s.id=g.season_id
                    JOIN nba.teams ht ON ht.id=g.home_team_id
                    JOIN nba.teams at ON at.id=g.away_team_id
                    LEFT JOIN LATERAL (
                        SELECT AVG(CASE WHEN ht2.abbreviation=$1 THEN g2.away_score ELSE g2.home_score END) as oppg
                        FROM nba.games g2
                        JOIN nba.seasons s2 ON s2.id=g2.season_id
                        JOIN nba.teams ht2 ON ht2.id=g2.home_team_id
                        JOIN nba.teams at2 ON at2.id=g2.away_team_id
                        WHERE (ht2.abbreviation={opp_tbl}.abbreviation OR at2.abbreviation={opp_tbl}.abbreviation)
                          AND (s2.year < s.year OR (s2.year = s.year AND g2.date::date < g.date::date))
                          AND g2.home_score IS NOT NULL
                        ORDER BY s2.year DESC, g2.date DESC LIMIT 10
                    ) opp ON true
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year<$2 OR (s.year=$2 AND g.date::date<$3))
                      AND g.home_score IS NOT NULL
                    ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.0

        # Simpler approach: use rolling averages for team and opponent
        h_adj_off_10 = h_ppg10 - a_oppg10  # home scores vs what away typically allows
        h_adj_def_10 = h_oppg10 - a_ppg10  # home allows vs what away typically scores
        a_adj_off_10 = a_ppg10 - h_oppg10
        a_adj_def_10 = a_oppg10 - h_ppg10
        h_adj_off_20 = h_ppg20 - a_oppg20
        h_adj_def_20 = h_oppg20 - a_ppg20
        a_adj_off_20 = a_ppg20 - h_oppg20
        a_adj_def_20 = a_oppg20 - h_ppg20

        # Rest
        async def _last(abbr):
            r=await conn.fetchrow("SELECT MAX(g.date::date) FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id WHERE (g.home_team_id=(SELECT id FROM nba.teams WHERE abbreviation=$1) OR g.away_team_id=(SELECT id FROM nba.teams WHERE abbreviation=$1)) AND (s.year<$2 OR (s.year=$2 AND g.date::date<$3)) AND g.home_score IS NOT NULL",abbr,yr,gd_obj)
            return r[0] if r and r[0] else None
        hl,al=await _last(home_abbr),await _last(away_abbr)
        gd=date.fromisoformat(game_date) if isinstance(game_date,str) else game_date
        rh=(gd-hl).days if (gd and hl) else 2; ra=(gd-al).days if (gd and al) else 2

        # Wins & ATS wins
        we="CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int ELSE (g.away_score>g.home_score)::int END"
        async def _wn(abbr,lim):
            r=await conn.fetchrow(f"SELECT COALESCE(SUM(sub.v),0) FROM (SELECT {we} as v FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id JOIN nba.teams ht ON ht.id=g.home_team_id JOIN nba.teams at ON at.id=g.away_team_id WHERE (ht.abbreviation=$1 OR at.abbreviation=$1) AND (s.year<$2 OR (s.year=$2 AND g.date::date<$3)) AND g.home_score IS NOT NULL ORDER BY s.year DESC,g.date DESC LIMIT {lim}) sub",abbr,yr,gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.0
        hw5=await _wn(home_abbr,5); hw10=await _wn(home_abbr,10); aw5=await _wn(away_abbr,5); aw10=await _wn(away_abbr,10)
        async def _aw(abbr,lim):
            r=await conn.fetchrow(f"SELECT COALESCE(SUM(sub.c),0) FROM (SELECT CASE WHEN ht.abbreviation=$1 THEN (g.home_score-g.away_score>-blc.spread)::int ELSE (g.away_score-g.home_score>blc.spread)::int END as c FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id JOIN nba.teams ht ON ht.id=g.home_team_id JOIN nba.teams at ON at.id=g.away_team_id JOIN nba.betting_lines_consolidated blc ON blc.game_id=g.id WHERE (ht.abbreviation=$1 OR at.abbreviation=$1) AND (s.year<$2 OR (s.year=$2 AND g.date::date<$3)) AND g.home_score IS NOT NULL AND blc.spread IS NOT NULL ORDER BY s.year DESC,g.date DESC LIMIT {lim}) sub",abbr,yr,gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.0
        # ATS cover margin (rolling 5)
        async def _am(abbr,lim=5):
            r=await conn.fetchrow(f"SELECT AVG(sub.m) FROM (SELECT CASE WHEN ht.abbreviation=$1 THEN (g.home_score-g.away_score+blc.spread) ELSE -(g.away_score-g.home_score+blc.spread) END as m FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id JOIN nba.teams ht ON ht.id=g.home_team_id JOIN nba.teams at ON at.id=g.away_team_id JOIN nba.betting_lines_consolidated blc ON blc.game_id=g.id WHERE (ht.abbreviation=$1 OR at.abbreviation=$1) AND (s.year<$2 OR (s.year=$2 AND g.date::date<$3)) AND g.home_score IS NOT NULL AND blc.spread IS NOT NULL ORDER BY s.year DESC,g.date DESC LIMIT {lim}) sub",abbr,yr,gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.0
        ham5=await _am(home_abbr); aam5=await _am(away_abbr)

        haw5=await _aw(home_abbr,5); aaw5=await _aw(away_abbr,5)

        hml=getattr(line_obj,'home_moneyline',None); aml=getattr(line_obj,'away_moneyline',None)
        hi=_ml_implied(hml); ai=_ml_implied(aml)
        im=_implied_margin(hi)
        sp=float(getattr(line_obj,'spread',0)or 0); osp=float(getattr(line_obj,'opening_spread',0)or sp)

        hc,ac=COORDS.get(home_abbr,(0,0)),COORDS.get(away_abbr,(0,0))
        tm=haversine(hc[0],hc[1],ac[0],ac[1])
        if tm<50: tm=0.0

        vals={"h_adj_off_10":h_adj_off_10,"h_adj_def_10":h_adj_def_10,"a_adj_off_10":a_adj_off_10,"a_adj_def_10":a_adj_def_10,
              "h_adj_off_20":h_adj_off_20,"h_adj_def_20":h_adj_def_20,"a_adj_off_20":a_adj_off_20,"a_adj_def_20":a_adj_def_20,

              "rest_h":float(rh),"rest_a":float(ra),"rest_diff":float(rh-ra),
              "home_b2b":float(rh==0),"away_b2b":float(ra==0),"travel_miles":tm,
              "h_implied":hi,"a_implied":ai,"spread":sp,"spread_movement":osp-sp,
              "implied_margin":float(im),
              "ml_spread_mismatch":float(im-(-sp)),
              "h_ats_wins_5":haw5,"a_ats_wins_5":aaw5,
              "h_ats_margin_5":ham5,"a_ats_margin_5":aam5,
              "h_wins_5":hw5,"h_wins_10":hw10,"a_wins_5":aw5,"a_wins_10":aw10}
        x=np.array([[vals.get(f,0.0) for f in FEATURES]],dtype=np.float32)
        margin=float(model.predict(x)[0]); conf=min(0.50+abs(margin)*0.04,0.95)
        return round(margin,1),round(conf,2)
    except Exception as e:
        logger.error(f"ATS pred failed [{game_id} {home_abbr}@{away_abbr}]: {e}")
        hm=float(getattr(home_stats,'run_margin',0.0)) if home_stats else 0.0
        am=float(getattr(away_stats,'run_margin',0.0)) if away_stats else 0.0
        return round(hm-am,1),0.50
    finally:
        if _cc and conn: await conn.close()

async def train_model(train_years=None,xgb_params=None):
    if train_years is None: train_years=[2020,2021,2022,2023,2024]
    if xgb_params is None: xgb_params={"n_estimators":350,"max_depth":5,"learning_rate":0.06,"subsample":0.75,"colsample_bytree":0.75,"reg_alpha":0.5,"reg_lambda":1.5,"random_state":42,"n_jobs":-1,"verbosity":0}
    engine=create_async_engine(DB); df=await load_data(engine); feats=build_features(df); await engine.dispose()
    tr=feats[feats["year"].isin(train_years)].reset_index(drop=True)
    model=xgb.XGBRegressor(**xgb_params); model.fit(tr[FEATURES].astype(np.float32),tr["actual_margin"].values,verbose=False)
    return model

if __name__ == "__main__":
    import argparse
    p=argparse.ArgumentParser(); p.add_argument("--test-year",type=int,default=2024); p.add_argument("--mode",type=str,default="single",choices=["single","all"])
    a=p.parse_args()
    if a.mode=="all": asyncio.run(run_all())
    else: asyncio.run(run_single(yr=a.test_year))
