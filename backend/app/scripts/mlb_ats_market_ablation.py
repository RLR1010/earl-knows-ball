"""Ablation: does MLB ATS accuracy come from the MARKET LINE (legit pre-game signal)
or genuine team/pitcher features? Runs the exact run_backtest logic for test_year=2026
in 3 variants:
  A) full ATS features
  B) full minus market-line features (spread kept — ATS metric needs it)
  C) market-line features ONLY
If B craters toward 50% and C~=A -> model rides the legit closing line (no leak).
If B stays ~58% -> features carry real signal (also no leak). A leak keeps ~impossible accuracy.
"""
import sys, os, time
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

import numpy as np
import xgboost as xgb
from sklearn.metrics import mean_absolute_error

from app.handicapping.mlb.data_loader import get_data_loader, build_features as mlb_build_features
from app.handicapping.mlb.mlb_xgb_model_ats import _ensure_ats_features

TEST_YEAR = 2026
TRAIN_FROM = 2017

MARKET_FEATS = [
    "home_moneyline", "away_moneyline", "opening_home_ml", "opening_away_ml",
    "h_implied", "a_implied", "opening_home_implied", "opening_away_implied",
    "ml_implied_movement", "closing_ou", "closing_over_odds", "closing_under_odds",
    "opening_ou", "ou_movement", "has_verified_ou", "implied_total",
]


def run(feats, fcols, label):
    col_map = {"ha_tz": "tz_diff", "aa_tz": "tz_diff"}
    fcols = [col_map.get(c, c) for c in fcols]
    present = [c for c in fcols if c in feats.columns]

    train_feats = feats[feats["season_year"].isin(list(range(TRAIN_FROM, TEST_YEAR)))].copy()
    test_feats = feats[feats["season_year"] == TEST_YEAR].copy()

    tm = train_feats["spread"].notna() & train_feats["home_moneyline"].notna()
    t2m = test_feats["spread"].notna() & test_feats["home_moneyline"].notna()
    train_feats = train_feats[tm]
    test_feats = test_feats[t2m]

    X_train = train_feats[present].values
    y_train = train_feats["actual_margin"].values
    X_test = test_feats[present].values
    y_test = test_feats["actual_margin"].values

    model = xgb.XGBRegressor(
        n_estimators=600, max_depth=5, learning_rate=0.04, subsample=0.8,
        colsample_bytree=0.6, reg_lambda=1.0, gamma=0.1, min_child_weight=3,
        random_state=42, verbosity=0, eval_metric="rmse",
    )
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)
    mae = mean_absolute_error(y_test, y_pred)

    ml_pred_home = y_pred > 0
    ml_actual = test_feats["home_score"].values > test_feats["away_score"].values
    ml_acc = float(np.mean(ml_pred_home == ml_actual))

    spread = test_feats["spread"].values
    valid = ~np.isnan(spread)
    yp = y_pred[valid]; yt = y_test[valid]; sp = spread[valid]
    ats_correct = np.sign(yp + sp) == np.sign(yt + sp)
    ats_acc = float(np.mean(ats_correct))

    print(f"[{label}] features={len(present)}  test={len(test_feats)}  ML={ml_acc:.4f} "
          f"({int(np.sum(ml_pred_home==ml_actual))}/{len(ml_actual)})  ATS={ats_acc:.4f} "
          f"({int(np.sum(ats_correct))}/{len(ats_correct)})  MAE={mae:.3f}")
    return {"label": label, "ml": ml_acc, "ats": ats_acc, "mae": mae, "n": len(test_feats)}


def main():
    t0 = time.time()
    print("Loading all FINAL games + building features (slow step)...")
    feats = mlb_build_features(get_data_loader().load_games(status="FINAL"))
    print(f"Loaded {len(feats)} games, {len(feats.columns)} features [{time.time()-t0:.0f}s]")

    full = _ensure_ats_features()
    reduced = [c for c in full if c not in MARKET_FEATS]
    market_only = [c for c in full if c in MARKET_FEATS]

    print(f"\nFull={len(full)} Reduced(no market)={len(reduced)} MarketOnly={len(market_only)}")
    print("\n=== A) FULL ===")
    a = run(feats, full, "FULL")
    print("=== B) NO MARKET LINE ===")
    b = run(feats, reduced, "NO-MARKET")
    print("=== C) MARKET LINE ONLY ===")
    c = run(feats, market_only, "MARKET-ONLY")

    print("\n=== SUMMARY (2026) ===")
    print(f"{'variant':<14}{'ML':>8}{'ATS':>8}{'MAE':>8}{'n':>6}")
    for r in (a, b, c):
        print(f"{r['label']:<14}{r['ml']:>8.4f}{r['ats']:>8.4f}{r['mae']:>8.3f}{r['n']:>6}")
    print("\nB craters->50% & C ~= A : model rides legit closing line (no leak).")
    print("B stays ~58%            : genuine features carry signal (no leak).")
    print("B keeps ~impossible acc : STILL A LEAK.")


if __name__ == "__main__":
    main()
