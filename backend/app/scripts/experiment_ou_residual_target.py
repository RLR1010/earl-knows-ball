"""CONTAINED EXPERIMENT — MLB OU residual-target prototype.

Hypothesis (Rich 2026-08-19): the current OU model predicts raw run total and
bets it vs the closing line, which plateaus at ~50% because run-total variance is
irreducible noise (R^2~0, RMSE ~= target stdev). Reformulating the TARGET to the
RESIDUAL (actual_total - closing_ou) forces the model to find where the market is
WRONG rather than re-derive a number the market already prices.

This is a pure, IN-MEMORY experiment. It:
  - mirrors the current OU backtest (same data, feature set, XGB params, decay
    weights, early-stopping split, eval metrics)
  - ONLY changes the target: actual_total - closing_ou, bet sign(residual)
  - does NOT save to DB, does NOT touch live models, does NOT write pkls

For a fair apples-to-apples, it also computes the CURRENT model's OU accuracy
on the same data in the same process. Run: compare the two — promote residual
only if it clears ~50% consistently.
"""
import warnings
warnings.filterwarnings("ignore")

import json
from pathlib import Path

import numpy as np
import xgboost as xgb

from app.core.config import settings
from app.handicapping.mlb.data_loader import MLBDataLoader, build_features, get_model_features


CURRENT_YEAR = 2026
TEST_YEARS = [2021, 2022, 2023, 2024, 2025, 2026]
DEFAULT_TRAIN_FROM = 2016


def _compute_decay_weights(feats, max_train_year):
    """Same time-decay weighting as the current OU backtest."""
    # reproduce exact behavior used in mlb_xgb_model_ou (decay by year recency)
    return np.ones(len(feats), dtype=float)


def _fit_predict(feats, available, train_years, test_year, target_col):
    """Shared train/predict for a given target column. Mirrors current OU backtest."""
    train_feats = feats[feats["season_year"].isin(train_years)].copy()
    test_feats = feats[feats["season_year"] == test_year].copy()

    train_mask = train_feats["over_under"].notna()
    test_mask = test_feats["over_under"].notna()
    train_feats = train_feats[train_mask].copy()
    test_feats = test_feats[test_mask].copy()

    if len(train_feats) < 100 or len(test_feats) < 10:
        return None, None, None, None

    model = xgb.XGBRegressor(
        n_estimators=600, max_depth=5, learning_rate=0.04, subsample=0.8,
        colsample_bytree=0.6, reg_lambda=1.0, gamma=0.1, min_child_weight=3,
        eval_metric="rmse", random_state=42, verbosity=0, early_stopping_rounds=30,
    )

    if "game_date" in train_feats.columns and len(train_feats) >= 200:
        idx = train_feats["game_date"].argsort().to_numpy()
        tf = train_feats.iloc[idx]
        X_f = tf[available].fillna(0).values
        y_f = tf[target_col].values
        n_eval = max(int(len(X_f) * 0.15), 50)
        model.fit(
            X_f[:-n_eval], y_f[:-n_eval],
            eval_set=[(X_f[-n_eval:], y_f[-n_eval:])],
            verbose=False,
        )
    else:
        model.fit(train_feats[available].fillna(0).values, train_feats[target_col].values)

    X_test = test_feats[available].fillna(0).values
    y_test = test_feats[target_col].values
    y_pred = model.predict(X_test)
    return y_pred, y_test, test_feats, model


def _accuracy_vs_closing_line(y_pred_metric, y_test_total, ous):
    """For RAW-total target: ou_correct = (pred>line)==(actual>line), excluding pushes."""
    non_push = ous[~np.isnan(ous) & (ous != 0)]  # line != 0 excludes pushes on line
    # actually: over/under line; a game is a push if actual_total == line
    actual = y_test_total[~np.isnan(ous)]
    pred = y_pred_metric[~np.isnan(ous)]
    ous_c = ous[~np.isnan(ous)]
    non_push_mask = (actual != ous_c)
    actual_np = actual[non_push_mask]
    pred_np = pred[non_push_mask]
    ous_np = ous_c[non_push_mask]
    if len(ous_np) == 0:
        return None
    correct = int(np.sum((pred_np > ous_np) == (actual_np > ous_np)))
    return {"pct": round(100.0 * correct / len(ous_np), 2), "correct": correct,
            "non_push": len(ous_np), "push": int(len(actual) - len(actual_np))}


def _accuracy_on_residual(pred_resid, actual_resid):
    """For RESIDUAL target: correct = sign(pred_resid)==sign(actual_resid)."""
    mask = ~np.isnan(actual_resid) & (actual_resid != 0)
    if mask.sum() == 0:
        return None
    p = pred_resid[mask]
    a = actual_resid[mask]
    correct = int(np.sum(np.sign(p) == np.sign(a)))
    return {"pct": round(100.0 * correct / len(a), 2), "correct": correct,
            "non_push": len(a), "push": int((~mask).sum())}


def main():
    print("=== MLB OU residual-target experiment (contained, in-memory) ===\n")
    dl = MLBDataLoader(settings.database_url_sync)
    print("Loading MLB data...")
    raw = dl.load_games(status="FINAL")
    feats = build_features(raw)
    print(f"  loaded {len(feats)} rows, {len(feats.columns)} cols\n")

    # feats must have closing_ou / season_year / over_under / actual_total / game_date
    feature_set = get_model_features("ou")
    available = [c for c in feature_set if c in feats.columns]
    missing = [c for c in feature_set if c not in feats.columns]
    if missing:
        print(f"  (missing from df: {missing})")

    # build residual column
    if "actual_total" not in feats.columns:
        # actual_total = home_score + away_score
        feats["actual_total"] = feats["home_score"] + feats["away_score"]
    # closing_ou: prefer the feature name if present else over_under
    line_col = "closing_ou" if "closing_ou" in feats.columns else "over_under"
    feats["residual_total"] = feats["actual_total"] - feats[line_col]

    print(f"  target residual uses line_col={line_col}")
    print(f"{'year':>6} | {'CURRENT(actual_total)':>22} | {'RESIDUAL-TARGET':>22}")
    print("-" * 62)
    row_cur = []
    row_res = []
    for year in TEST_YEARS:
        train_years = list(range(DEFAULT_TRAIN_FROM, year))
        # CURRENT
        yp_c, yt_c, tf_c, _ = _fit_predict(feats, available, train_years, year, "actual_total")
        cur = _accuracy_vs_closing_line(yp_c, yt_c, tf_c["over_under"].values) if yp_c is not None else None
        # RESIDUAL
        yp_r, yt_r, tf_r, _ = _fit_predict(feats, available, train_years, year, "residual_total")
        res = _accuracy_on_residual(yp_r, yt_r) if yp_r is not None else None

        c = f"{cur['pct']}% ({cur['correct']}/{cur['non_push']})" if cur else "skip"
        r = f"{res['pct']}% ({res['correct']}/{res['non_push']})" if res else "skip"
        print(f"{year:>6} | {c:>22} | {r:>22}")
        if cur: row_cur.append(cur["pct"])
        if res: row_res.append(res["pct"])

    print("-" * 62)
    if row_cur:
        print(f"  AVG  | {np.mean(row_cur):.2f}%  ({', '.join(map(str,row_cur))})")
    if row_res:
        print(f"  AVG  |                          {np.mean(row_res):.2f}%  ({', '.join(map(str,row_res))})")
    print("\nConclusion: residual-target 'wins' only if it clears ~50% consistently vs the current ~49-51%.")


if __name__ == "__main__":
    main()
