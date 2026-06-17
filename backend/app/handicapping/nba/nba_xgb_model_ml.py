"""
NBA XGBoost Moneyline Backtester — binary classifier predicting home team win probability.

Dedicated ML model optimized for winner prediction in the NBA:
- Binary classification (XGBClassifier with objective='binary:logistic')
- Team quality features (scoring margin, efficiency)
- Rest/back-to-back factors (critical in NBA)
- Home court advantage history
- Market anchors + movement

Usage:
    docker exec earl-knows-football-api-1 python -m app.handicapping.nba.nba_xgb_model_ml --test-year 2023
    docker exec earl-knows-football-api-1 python -m app.handicapping.nba.nba_xgb_model_ml --mode all
"""
import asyncio
import pickle
from typing import Optional
import logging
import warnings
import json
import math
from datetime import datetime, date

warnings.filterwarnings("ignore")
from pathlib import Path
import numpy as np
import asyncpg
import pandas as pd
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from sklearn.metrics import accuracy_score, log_loss, roc_auc_score, brier_score_loss
import xgboost as xgb

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("earl.nba_xgb_ml")
log = logger.info

import os
DB = os.environ.get("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
DSN = DB.replace("+asyncpg", "")

# ── Team coordinates ──
COORDS = {
    "ATL": (33.8, -84.4), "BKN": (40.7, -73.9), "BOS": (42.4, -71.1),
    "CHA": (35.2, -80.8), "CHI": (41.9, -87.6), "CLE": (41.5, -81.7),
    "DAL": (32.8, -96.8), "DEN": (39.7, -104.9), "DET": (42.3, -83.0),
    "GSW": (37.8, -122.4), "HOU": (29.8, -95.4), "IND": (39.8, -86.2),
    "LAC": (34.0, -118.3), "LAL": (34.0, -118.3), "MEM": (35.1, -90.0),
    "MIA": (25.8, -80.2), "MIL": (43.0, -87.9), "MIN": (45.0, -93.3),
    "NJ": (40.7, -73.9), "NOP": (29.9, -90.1), "NYK": (40.8, -73.9),
    "OKC": (35.5, -97.5), "ORL": (28.5, -81.4), "PHI": (39.9, -75.2),
    "PHX": (33.4, -112.1), "POR": (45.5, -122.7), "SAC": (38.6, -121.5),
    "SAS": (29.4, -98.5), "SEA": (47.6, -122.3), "TOR": (43.6, -79.4),
    "UTA": (40.8, -112.0), "WAS": (38.9, -77.0),
}

ALTITUDE = {
    "ATL": 1050, "BKN": 10, "BOS": 141, "CHA": 748, "CHI": 579,
    "CLE": 653, "DAL": 430, "DEN": 5280, "DET": 585, "GSW": 52,
    "HOU": 43, "IND": 718, "LAC": 262, "LAL": 262, "MEM": 259,
    "MIA": 6, "MIL": 617, "MIN": 815, "NJ": 10, "NOP": 7,
    "NYK": 10, "OKC": 1195, "ORL": 82, "PHI": 28, "PHX": 1086,
    "POR": 50, "SAC": 23, "SAS": 636, "SEA": 141, "TOR": 249,
    "UTA": 4220, "WAS": 10,
}

# ── ML Feature Set for NBA ──
# Tier 1: Market anchor (1) — implied odds
# Tier 2: Team quality (scoring margin, efficiency)
# Tier 3: Rest & schedule (back-to-back, travel)
# Tier 4: Situational (playoff, division, altitude)
# Tier 5: Recent form

NBA_ML_FEATURES = [
    "home_implied",
    "ml_implied_movement",
    "h_ppg10", "a_ppg10",
    "h_oppg10", "a_oppg10",
    "h_margin10", "a_margin10",
    "h_fg_pct10", "a_fg_pct10",
    "h_3pt_pct10", "a_3pt_pct10",
    "h_winpct", "a_winpct",
    "h_home_ppg", "a_away_ppg",
    "h_form_l10", "a_form_l10",
    "rest_diff", "rest_h", "rest_a",
    "travel_miles",
    "is_div", "is_conf",
    "altitude_diff",
    "is_playoff",
]

FEATURES_TRAINING = [f for f in NBA_ML_FEATURES if f not in ("home_implied",)]


def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def load_data(engine):
    """Load all completed NBA games with scores, lines, and stats."""
    log("Loading games...")
    async with engine.connect() as conn:
        r = await conn.execute(text("""
            SELECT g.id, s.year, g.date::date as game_date,
                   ht.abbreviation as ha, at.abbreviation as aa,
                   g.home_score, g.away_score,
                   (g.home_score - g.away_score) as margin,
                   g.game_type,
                   g.home_team_id as htid, g.away_team_id as atid,
                   ht.conference as hconf, at.conference as aconf,
                   ht.division as hdiv, at.division as adiv,
                   g.home_field_goals_made as h_fgm,
                   g.home_field_goals_attempted as h_fga,
                   g.home_three_points_made as h_3pm,
                   g.home_three_points_attempted as h_3pa,
                   g.away_field_goals_made as a_fgm,
                   g.away_field_goals_attempted as a_fga,
                   g.away_three_points_made as a_3pm,
                   g.away_three_points_attempted as a_3pa
            FROM nba.games g
            JOIN nba.seasons s ON s.id = g.season_id
            JOIN nba.teams ht ON ht.id = g.home_team_id
            JOIN nba.teams at ON at.id = g.away_team_id
            WHERE g.home_score IS NOT NULL AND g.away_score IS NOT NULL
            ORDER BY s.year, g.date, g.id
        """))
        games = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])

        log("Loading betting lines...")
        r = await conn.execute(text("""
            SELECT game_id, home_moneyline, away_moneyline,
                   home_implied_probability, away_implied_probability,
                   spread, over_under
            FROM nba.betting_lines_consolidated
            WHERE home_moneyline IS NOT NULL AND away_moneyline IS NOT NULL
        """))
        lines = pd.DataFrame([dict(r._mapping) for r in r.fetchall()])

    log(f"  Games: {len(games)} ({games.year.min()}-{games.year.max()})")
    log(f"  Lines with ML: {len(lines)}")

    games = games.rename(columns={"id": "game_id"})
    df = games.merge(lines, on="game_id", how="inner")
    log(f"  Merged: {len(df)} rows with ML data")
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build ML-optimized features for NBA win prediction."""
    log("Building team-game table...")

    rows = []
    for _, g in df.iterrows():
        rows.append({
            "game_id": g["game_id"], "year": g["year"], "game_date": g["game_date"],
            "team": g["ha"], "opp": g["aa"],
            "pf": g["home_score"], "pa": g["away_score"],
            "margin": g["margin"], "is_home": 1,
            "fgm": g.h_fgm, "fga": g.h_fga,
            "pm3": g.h_3pm, "pa3": g.h_3pa,
            "conference": g.hconf, "division": g.hdiv,
            "game_type": g.game_type,
        })
        rows.append({
            "game_id": g["game_id"], "year": g["year"], "game_date": g["game_date"],
            "team": g["aa"], "opp": g["ha"],
            "pf": g["away_score"], "pa": g["home_score"],
            "margin": -g["margin"], "is_home": 0,
            "fgm": g.a_fgm, "fga": g.a_fga,
            "pm3": g.a_3pm, "pa3": g.a_3pa,
            "conference": g.aconf, "division": g.adiv,
            "game_type": g.game_type,
        })

    tg = pd.DataFrame(rows).sort_values(["team", "game_date", "game_id"]).reset_index(drop=True)
    tg["game_date_dt"] = pd.to_datetime(tg["game_date"])

    # Rolling stats
    for window in [5, 10, 20]:
        tg[f"ppg{window}"] = tg.groupby("team")["pf"].transform(
            lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        tg[f"oppg{window}"] = tg.groupby("team")["pa"].transform(
            lambda x: x.shift(1).rolling(window, min_periods=1).mean())
        tg[f"margin{window}"] = tg.groupby("team")["margin"].transform(
            lambda x: x.shift(1).rolling(window, min_periods=1).mean())

    # Shooting efficiency
    for window in [10]:
        tg[f"fg_pct{window}"] = (
            tg.groupby("team")["fgm"].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).sum())
            / tg.groupby("team")["fga"].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).sum()))
        tg[f"3pt_pct{window}"] = (
            tg.groupby("team")["pm3"].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).sum())
            / tg.groupby("team")["pa3"].transform(
                lambda x: x.shift(1).rolling(window, min_periods=1).sum()))

    # Win percentage
    tg["is_win"] = (tg["pf"] > tg["pa"]).astype(int)
    tg["winpct"] = tg.groupby("team")["is_win"].transform(
        lambda x: x.shift(1).expanding().mean())
    tg["winpct_l10"] = tg.groupby("team")["is_win"].transform(
        lambda x: x.shift(1).rolling(10, min_periods=1).mean())

    # Rest days
    tg["prev_date"] = tg.groupby("team")["game_date_dt"].shift(1)
    tg["rest"] = (tg["game_date_dt"] - tg["prev_date"]).dt.days.fillna(1).clip(0, 30)

    # Home/away splits
    for team in tg["team"].unique():
        mask = tg["team"] == team
        for is_h in [1, 0]:
            sub = tg[mask & (tg["is_home"] == is_h)].copy()
            if len(sub) < 2:
                continue
            sub["home_ppg"] = sub["pf"].shift(1).expanding().mean()
            for idx, row in sub.iterrows():
                tg.at[idx, f"{'h' if is_h else 'a'}_home_ppg"] = row["home_ppg"]

    # Rejoin to game level
    h = tg[tg["is_home"] == 1][
        ["game_id", "team", "ppg10", "oppg10", "margin10",
         "fg_pct10", "3pt_pct10",
         "rest", "winpct", "winpct_l10",
         "h_home_ppg", "division", "conference"]
    ].rename(columns={
        "team": "ha",
        "ppg10": "h_ppg10", "oppg10": "h_oppg10", "margin10": "h_margin10",
        "fg_pct10": "h_fg_pct10", "3pt_pct10": "h_3pt_pct10",
        "rest": "rest_h", "winpct": "h_winpct", "winpct_l10": "h_winpct_l10",
        "division": "hdiv",
    })

    a = tg[tg["is_home"] == 0][
        ["game_id", "team", "ppg10", "oppg10", "margin10",
         "fg_pct10", "3pt_pct10",
         "rest", "winpct", "winpct_l10",
         "a_home_ppg", "division", "conference"]
    ].rename(columns={
        "team": "aa",
        "ppg10": "a_ppg10", "oppg10": "a_oppg10", "margin10": "a_margin10",
        "fg_pct10": "a_fg_pct10", "3pt_pct10": "a_3pt_pct10",
        "rest": "rest_a", "winpct": "a_winpct", "winpct_l10": "a_winpct_l10",
        "division": "adiv",
    })

    feats = h.merge(a, on="game_id")
    feats["rest_diff"] = feats["rest_h"] - feats["rest_a"]
    feats["is_div"] = (feats["hdiv"] == feats["adiv"]).astype(int)

    # Conference
    conf_map = df.set_index("game_id")[["hconf", "aconf"]].to_dict("index")
    feats["is_conf"] = feats["game_id"].apply(
        lambda gid: 1 if gid in conf_map and conf_map[gid]["hconf"] == conf_map[gid]["aconf"] else 0
    )

    # Add original game data
    orig_cols = df[["game_id", "year", "game_date", "margin", "home_score", "away_score",
                     "home_moneyline", "away_moneyline",
                     "home_implied_probability", "away_implied_probability",
                     "spread", "over_under", "game_type"]].copy()
    feats = feats.merge(orig_cols, on="game_id")

    feats["home_wins"] = (feats["margin"] > 0).astype(int)
    feats["home_implied"] = feats["home_implied_probability"].fillna(0.5)
    feats["away_implied"] = feats["away_implied_probability"].fillna(0.5)

    def ml_to_implied(ml):
        if pd.isna(ml) or ml == 0:
            return 0.5
        return 100 / (ml + 100) if ml > 100 else abs(ml) / (abs(ml) + 100)

    feats["opening_home_implied"] = feats["home_moneyline"].apply(ml_to_implied)
    feats["ml_implied_movement"] = feats["home_implied"] - feats["opening_home_implied"]

    feats["h_form_l10"] = (feats["h_winpct_l10"] * 10).fillna(5)
    feats["a_form_l10"] = (feats["a_winpct_l10"] * 10).fillna(5)

    # Travel
    feats["travel_miles"] = feats.apply(
        lambda r: haversine(*COORDS.get(r["aa"], (0, 0)), *COORDS.get(r["ha"], (0, 0))),
        axis=1)
    feats.loc[feats["travel_miles"] < 50, "travel_miles"] = 0

    # Altitude
    feats["altitude_diff"] = feats["ha"].map(ALTITUDE) - feats["aa"].map(ALTITUDE)

    # Playoff flag
    feats["is_playoff"] = (feats["game_type"].isin(["P", "PO", "PS"])).astype(int)

    feats["game_date_dt"] = pd.to_datetime(feats["game_date"])
    feats["month"] = feats["game_date_dt"].dt.month

    return feats


async def run_backtest(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    test_year: int = 2023,
    train_years: list[int] = None,
    xgb_params: dict = None,
) -> dict:
    """Run XGBoost ML backtest for a single year."""
    if train_years is None:
        train_years = [y for y in range(test_year - 5, test_year)]

    if xgb_params is None:
        xgb_params = {
            "n_estimators": 400,
            "max_depth": 5,
            "learning_rate": 0.04,
            "subsample": 0.7,
            "colsample_bytree": 0.7,
            "reg_alpha": 1.0,
            "reg_lambda": 2.0,
            "min_child_weight": 2,
            "random_state": 42,
            "n_jobs": -1,
            "verbosity": 0,
        }

    tr_mask = feats["year"].isin(train_years)
    te_mask = feats["year"] == test_year
    tr = feats[tr_mask].sort_values(["game_date"]).reset_index(drop=True)
    te = feats[te_mask].sort_values(["month", "game_date"]).reset_index(drop=True)

    log(f"  Train: {len(tr)} games ({train_years})")
    log(f"  Test:  {len(te)} games ({test_year})")

    if len(tr) < 50 or len(te) < 10:
        log("  ⚠ Not enough data, skipping")
        return {"error": "insufficient_data"}

    # Residual training: predict edge vs opening implied
    opening_home_implied_te = te["opening_home_implied"].values
    opening_home_implied_tr = tr["opening_home_implied"].values
    home_implied_te = te["home_implied"].values

    X_tr = tr[FEATURES_TRAINING].fillna(0).astype(np.float32)
    y_tr_residual = tr["home_wins"].values - opening_home_implied_tr

    X_te = te[FEATURES_TRAINING].fillna(0).astype(np.float32)
    y_te = te["home_wins"].values

    n_tr = len(tr)
    w = np.ones(n_tr)
    for i in range(n_tr):
        s = tr.at[tr.index[i], "year"]
        years_back = test_year - s
        if years_back <= 1:
            w[i] = 4.0
        elif years_back <= 2:
            w[i] = 3.0
        elif years_back <= 3:
            w[i] = 2.0
        elif years_back <= 5:
            w[i] = 1.5

    residual_model = xgb.XGBRegressor(objective="reg:squarederror", **xgb_params)
    residual_model.fit(X_tr, y_tr_residual, sample_weight=w, verbose=False)

    pred_residual = residual_model.predict(X_te)
    pred_prob = np.clip(opening_home_implied_te + pred_residual, 0.01, 0.99)
    pred_class = (pred_prob > 0.5).astype(int)

    # Evaluate
    from sklearn.metrics import accuracy_score, roc_auc_score as _roc_auc, \
        brier_score_loss as _brier, log_loss as _logloss

    accuracy = float(round(accuracy_score(y_te, pred_class), 4))
    auc = float(round(_roc_auc(y_te, pred_prob), 4))
    brier = float(round(_brier(y_te, pred_prob), 4))
    ll = float(round(_logloss(y_te, pred_prob), 4))

    # Baselines
    bl_closing = (home_implied_te > 0.5).astype(int)
    bl_closing_acc = float(round(accuracy_score(y_te, bl_closing), 4))
    bl_opening = (opening_home_implied_te > 0.5).astype(int)
    bl_opening_acc = float(round(accuracy_score(y_te, bl_opening), 4))

    # ROI
    home_moneylines = te["home_moneyline"].values
    roi_results = {}
    for thresh in [0.5, 0.55, 0.6, 0.65]:
        bets = []
        for i in range(len(y_te)):
            prob = pred_prob[i]
            ml = home_moneylines[i] if not np.isnan(home_moneylines[i]) else None

            if prob > thresh:
                won = y_te[i] == 1
                if ml is not None and not np.isnan(ml):
                    payout = 100 / abs(ml) if ml < 0 else ml / 100
                else:
                    payout = 1.0
                bets.append(payout if won else -1.0)
            elif (1 - prob) > thresh:
                bets.append(1.0 if y_te[i] == 0 else -1.0)

        if bets:
            roi_results[str(thresh)] = {
                "bets": len(bets), "wins": sum(1 for b in bets if b > 0),
                "losses": len(bets) - sum(1 for b in bets if b > 0),
                "pct": round(100 * sum(1 for b in bets if b > 0) / len(bets), 1),
                "roi": round(100 * sum(bets) / len(bets), 1),
            }
        else:
            roi_results[str(thresh)] = {"bets": 0, "roi": 0.0}

    # Monthly
    monthly = []
    for m in sorted(te["month"].unique()):
        sub = te[te["month"] == m]
        if len(sub) < 5:
            continue
        sub_residual = residual_model.predict(sub[FEATURES_TRAINING].fillna(0).astype(np.float32))
        sub_prob = np.clip(sub["opening_home_implied"].values + sub_residual, 0.01, 0.99)
        sub_acc = float(round(accuracy_score(sub["home_wins"].values, (sub_prob > 0.5).astype(int)), 4))
        monthly.append({"month": int(m), "games": int(len(sub)), "accuracy": sub_acc})

    # Feature importance
    imp = pd.DataFrame({
        "feature": list(FEATURES_TRAINING),
        "importance": list(residual_model.feature_importances_),
    }).sort_values("importance", ascending=False)

    result = {
        "test_year": test_year,
        "train_years": train_years,
        "feature_set": "nba_ml",
        "total_games": len(te),
        "accuracy": accuracy,
        "correct": int((pred_class == y_te).sum()),
        "incorrect": int((pred_class != y_te).sum()),
        "auc": auc,
        "brier": brier,
        "log_loss": ll,
        "opening_baseline": {"accuracy": bl_opening_acc, "correct": int(bl_opening.sum()), "incorrect": len(te) - int(bl_opening.sum())},
        "closing_baseline": {"accuracy": bl_closing_acc, "correct": int(bl_closing.sum()), "incorrect": len(te) - int(bl_closing.sum())},
        "roi_by_threshold": roi_results,
        "monthly": monthly,
        "feature_importance": [
            {"feature": str(r["feature"]), "importance": float(round(float(r["importance"]), 4))}
            for _, r in imp.iterrows()
        ],
    }

    print_ml_summary(result, te)
    return result


def print_ml_summary(result: dict, te: pd.DataFrame):
    print(f"\n{'='*62}")
    print(f"NBA MONEYLINE BACKTEST — {result['test_year']} Season")
    print(f"Features: {result['feature_set']} ({len(result['feature_importance'])} feats)")
    print(f"Train: {result['train_years']}")
    print(f"{'='*62}")

    print(f"\n🏆 WINNER PREDICTION")
    print(f"  Games:     {result['total_games']}")
    print(f"  Accuracy:  {result['accuracy']:.4f} ({result['correct']}-{result['incorrect']})")
    print(f"  AUC:       {result['auc']:.4f}")
    print(f"  Brier:     {result['brier']:.4f}")

    if "closing_baseline" in result:
        cl = result["closing_baseline"]
        bl_acc = cl["accuracy"]
        diff = (result['accuracy'] - bl_acc) * 100
        print(f"\n📊 VS CLOSING MARKET")
        print(f"  Closing: {bl_acc:.4f} ({cl['correct']}-{cl['incorrect']}) ({'+' if diff>=0 else ''}{diff:.1f}%)")

    if "opening_baseline" in result:
        ol = result["opening_baseline"]
        bl_acc = ol["accuracy"]
        diff = (result['accuracy'] - bl_acc) * 100
        print(f"  Opening: {bl_acc:.4f} ({ol['correct']}-{ol['incorrect']}) ({'+' if diff>=0 else ''}{diff:.1f}%)")

    if result.get("roi_by_threshold"):
        print(f"\n💰 ROI BY THRESHOLD")
        print(f"  {'Thresh':>8s}  {'Bets':>5s}  {'W-L':>10s}  {'Win%':>6s}  {'ROI':>6s}")
        for t, r in sorted(result["roi_by_threshold"].items()):
            if r["bets"] == 0:
                continue
            print(f"  {t:>8s}  {r['bets']:>5d}  {r['wins']}-{r['losses']:>4d}  {r['pct']:>5.1f}%  {r['roi']:>5.1f}%")

    if result["monthly"]:
        print(f"\n📅 MONTHLY BREAKDOWN")
        print(f"  {'Month':>6s}  {'Games':>5s}  {'Acc':>6s}")
        for m in result["monthly"]:
            print(f"  {m['month']:>6d}  {m['games']:>5d}  {m['accuracy']:.4f}")

    print(f"\n🔑 TOP FEATURES")
    for i, fi in enumerate(result["feature_importance"][:15]):
        bar = "█" * int(fi["importance"] * 100)
        print(f"  {i+1:2d}. {fi['feature']:>20s}: {fi['importance']:.4f} {bar}")
    print()


# ── Model management & inference (imported by nba_engine.py) ──────────

ML_MODEL_PATH = Path("/app/data/nba_ml_residual_model_prod.pkl")
_ml_model = None


def set_model_path(path: str):
    global ML_MODEL_PATH, _ml_model
    ML_MODEL_PATH = Path(path)
    _ml_model = None


def _load_ml_model():
    global _ml_model
    if _ml_model is not None:
        return _ml_model
    if not ML_MODEL_PATH.exists():
        raise FileNotFoundError(f"ML model not found at {ML_MODEL_PATH}")
    with open(ML_MODEL_PATH, "rb") as f:
        payload = pickle.load(f)
    model = payload["model"] if isinstance(payload, dict) else payload
    _ml_model = model
    log(f"Loaded ML model ({len(FEATURES_TRAINING)} features)")
    return _ml_model


def _ml_implied(v):
    if v is None or v == 0:
        return 0.5
    return abs(v) / (abs(v) + 100) if v < 0 else 100.0 / (v + 100)


async def predict_ml(game_id: int, home_abbr: str, away_abbr: str,
                      yr: int, game_date: str,
                      home_stats, away_stats,
                      line_obj,
                      conn: Optional[asyncpg.Connection] = None) -> tuple[Optional[float], float, float]:
    """Predict home win probability for one NBA game."""
    try:
        model = _load_ml_model()
    except FileNotFoundError:
        logger.warning("ML model not found at %s", ML_MODEL_PATH)
        return None, 0.0, 0.0

    gd_obj = date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
    _close_conn = False
    if conn is None:
        conn = await asyncpg.connect(DSN)
        _close_conn = True
    try:
        h_ml = getattr(line_obj, 'home_moneyline', None)
        hi = _ml_implied(h_ml)
        ohi = hi  # No opening column available, use closing
        mm = hi - ohi  # Will be 0

        async def _avg(abbr, sel, lim, extra="", default=0.0):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.v) FROM (
                    SELECT {sel} as v FROM nba.games g
                    JOIN nba.seasons s ON s.id=g.season_id
                    JOIN nba.teams ht ON ht.id=g.home_team_id JOIN nba.teams at ON at.id=g.away_team_id
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL {extra} ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else default

        h_ppg10 = await _avg(home_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END", 10)
        a_ppg10 = await _avg(away_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.home_score ELSE g.away_score END", 10)
        h_oppg10 = await _avg(home_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END", 10)
        a_oppg10 = await _avg(away_abbr, "CASE WHEN ht.abbreviation=$1 THEN g.away_score ELSE g.home_score END", 10)

        async def _wp(abbr, lim=999):
            r = await conn.fetchrow(f"""
                SELECT AVG(sub.w) FROM (
                    SELECT CASE WHEN ht.abbreviation=$1 THEN (g.home_score>g.away_score)::int
                           ELSE (g.away_score>g.home_score)::int END as w
                    FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
                    JOIN nba.teams ht ON ht.id=g.home_team_id JOIN nba.teams at ON at.id=g.away_team_id
                    WHERE (ht.abbreviation=$1 OR at.abbreviation=$1)
                      AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                      AND g.home_score IS NOT NULL ORDER BY s.year DESC, g.date DESC LIMIT {lim}
                ) sub
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 0.5

        h_wp, a_wp = await _wp(home_abbr, 999), await _wp(away_abbr, 999)
        h_f10, a_f10 = await _wp(home_abbr, 10), await _wp(away_abbr, 10)

        # Home/road splits
        async def _homescore(abbr):
            r = await conn.fetchrow(f"""
                SELECT AVG(g.home_score) FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
                JOIN nba.teams ht ON ht.id=g.home_team_id
                WHERE ht.abbreviation=$1 AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                  AND g.home_score IS NOT NULL
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 105.0

        async def _awayscore(abbr):
            r = await conn.fetchrow(f"""
                SELECT AVG(g.away_score) FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
                JOIN nba.teams at ON at.id=g.away_team_id
                WHERE at.abbreviation=$1 AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                  AND g.home_score IS NOT NULL
            """, abbr, yr, gd_obj)
            return float(r[0]) if (r and r[0] is not None) else 102.0

        hh_ppg = await _homescore(home_abbr)
        aa_ppg = await _awayscore(away_abbr)

        # Rest
        async def _last(abbr):
            r = await conn.fetchrow("""
                SELECT MAX(g.date::date) FROM nba.games g JOIN nba.seasons s ON s.id=g.season_id
                WHERE (g.home_team_id=(SELECT id FROM nba.teams WHERE abbreviation=$1)
                    OR g.away_team_id=(SELECT id FROM nba.teams WHERE abbreviation=$1))
                  AND (s.year < $2 OR (s.year = $2 AND g.date::date < $3))
                  AND g.home_score IS NOT NULL
            """, abbr, yr, gd_obj)
            return r[0] if r and r[0] else None

        hl, al = await _last(home_abbr), await _last(away_abbr)
        gd = date.fromisoformat(game_date) if isinstance(game_date, str) else game_date
        rh = (gd - hl).days if (gd and hl) else 3
        ra = (gd - al).days if (gd and al) else 3
        rd = rh - ra

        # Travel
        hc, ac = COORDS.get(home_abbr, (0, 0)), COORDS.get(away_abbr, (0, 0))
        R = 3958.8
        dl = math.radians(ac[0] - hc[0])
        dn = math.radians(ac[1] - hc[1])
        a = math.sin(dl/2)**2 + math.cos(math.radians(hc[0])) * math.cos(math.radians(ac[0])) * math.sin(dn/2)**2
        tm = R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        if tm < 50:
            tm = 0.0

        rows = await conn.fetch("SELECT abbreviation, division, conference FROM nba.teams")
        divs = {r['abbreviation']: r['division'] for r in rows}
        confs = {r['abbreviation']: r['conference'] for r in rows}
        idiv = 1 if (divs.get(home_abbr) and divs.get(away_abbr) and divs[home_abbr] == divs[away_abbr]) else 0
        iconf = 1 if (confs.get(home_abbr) and confs.get(away_abbr) and confs[home_abbr] == confs[away_abbr]) else 0

        gr = await conn.fetchrow("SELECT game_type FROM nba.games WHERE id=$1", game_id)
        is_playoff = 1 if (gr and gr['game_type'] in ('P', 'PO', 'PS')) else 0

        alt_diff = ALTITUDE.get(home_abbr, 0) - ALTITUDE.get(away_abbr, 0)

        vals = {
            "ml_implied_movement": mm,
            "h_ppg10": h_ppg10, "a_ppg10": a_ppg10,
            "h_oppg10": h_oppg10, "a_oppg10": a_oppg10,
            "h_margin10": h_ppg10 - h_oppg10,
            "a_margin10": a_ppg10 - a_oppg10,
            "h_fg_pct10": 0.45, "a_fg_pct10": 0.45,
            "h_3pt_pct10": 0.35, "a_3pt_pct10": 0.35,
            "h_winpct": h_wp, "a_winpct": a_wp,
            "h_home_ppg": hh_ppg, "a_away_ppg": aa_ppg,
            "h_form_l10": h_f10 * 10, "a_form_l10": a_f10 * 10,
            "rest_diff": rd, "rest_h": rh, "rest_a": ra,
            "travel_miles": tm,
            "is_div": idiv, "is_conf": iconf,
            "altitude_diff": alt_diff,
            "is_playoff": is_playoff,
        }

        x = np.array([[vals.get(f, 0.0) for f in FEATURES_TRAINING]], dtype=np.float32)
        resid = float(model.predict(x)[0])
        prob = float(np.clip(ohi + resid, 0.01, 0.99))
        edge = prob - hi
        conf = min(0.5 + abs(prob - 0.5) + abs(edge) * 0.5, 0.95)
        return round(prob, 4), round(conf, 2), round(edge, 4)

    except Exception as e:
        logger.error(f"ML pred failed [{game_id} {home_abbr}@{away_abbr}]: {e}")
        return None, 0.50, 0.0
    finally:
        if _close_conn and conn:
            await conn.close()


async def train_model(year: int, train_years: list[int]) -> object:
    """Train ML model from scratch on given years."""
    engine = create_async_engine(DB)
    df = await load_data(engine)
    feats = build_features(df)
    await engine.dispose()

    tr_all = feats[feats["year"].isin(train_years)].reset_index(drop=True)
    log(f"Training ML on {len(tr_all)} games ({train_years})")

    opening_implied = tr_all["opening_home_implied"].values
    y_residual = tr_all["home_wins"].values - opening_implied
    q01, q99 = np.percentile(y_residual, [1, 99])
    valid = (y_residual >= q01) & (y_residual <= q99)
    tr_all = tr_all[valid].reset_index(drop=True)
    y_residual = y_residual[valid]

    X_tr = tr_all[FEATURES_TRAINING].fillna(0).astype(np.float32)

    w = np.ones(len(tr_all))
    for i in range(len(tr_all)):
        s = tr_all.at[tr_all.index[i], "year"]
        years_back = year - s
        if years_back <= 1:
            w[i] = 4.0
        elif years_back <= 2:
            w[i] = 3.0
        elif years_back <= 3:
            w[i] = 2.0
        elif years_back <= 5:
            w[i] = 1.5

    model = xgb.XGBRegressor(
        n_estimators=400, max_depth=5, learning_rate=0.04,
        subsample=0.7, colsample_bytree=0.7,
        reg_alpha=1.0, reg_lambda=2.0,
        min_child_weight=2,
        random_state=42, n_jobs=-1, verbosity=0,
    )
    model.fit(X_tr, y_residual, sample_weight=w, verbose=False)
    log(f"ML model trained ({len(FEATURES_TRAINING)} features)")
    return model


async def run_all_years(
    test_years: list[int] = None,
    train_from: int = 2011,
):
    """Run ML backtests across multiple years."""
    if test_years is None:
        test_years = [2021, 2022, 2023, 2024, 2025, 2026]

    t0 = datetime.now()
    engine = create_async_engine(DB)
    df = await load_data(engine)
    feats = build_features(df)

    all_results = []
    for year in test_years:
        result = await run_backtest(df, feats, test_year=year)
        if "error" not in result:
            all_results.append(result)

    await engine.dispose()

    print(f"\n{'='*62}")
    print("NBA MONEYLINE BACKTEST — ALL YEARS")
    print(f"{'='*62}")
    print(f"\n{'Year':>4s}  {'Games':>5s}  {'Acc':>6s}  {'AUC':>5s}  {'Brier':>6s}")
    print("─" * 48)
    total_c = 0
    total_i = 0
    for r in sorted(all_results, key=lambda x: x["test_year"]):
        print(f"  {r['test_year']:>4d}  {r['total_games']:>5d}  {r['accuracy']:.4f}  {r['auc']:.4f}  {r['brier']:.4f}")
        total_c += r["correct"]
        total_i += r["incorrect"]
    tp = total_c + total_i
    print(f"  {'─'*42}")
    print(f"  {'TOTAL':>4s}  {tp:>5d}  {round(100*total_c/max(tp,1),1):>5.1f}%  ({total_c:>4d}-{total_i:>4d})")

    log(f"\nTotal time: {datetime.now() - t0}")


async def run_single(test_year: int = 2023):
    """Run a single ML backtest for quick iteration."""
    engine = create_async_engine(DB)
    df = await load_data(engine)
    feats = build_features(df)
    await run_backtest(df, feats, test_year=test_year)
    await engine.dispose()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NBA XGBoost Moneyline Backtester")
    parser.add_argument("--test-year", type=int, default=2023, help="Year to test on")
    parser.add_argument("--mode", type=str, default="single",
                        choices=["single", "all"], help="Single year or all years")
    args = parser.parse_args()
    if args.mode == "all":
        asyncio.run(run_all_years())
    else:
        asyncio.run(run_single(test_year=args.test_year))
