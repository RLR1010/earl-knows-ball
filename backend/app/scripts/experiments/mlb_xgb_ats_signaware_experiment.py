"""
Sign-Aware ATS Experiment — MLB XGBoost backtester.

EXPERIMENT — this is a standalone exploratory script. It does NOT touch or
overwrite the production training script ``mlb_xgb_model_ats.py`` or the
production model artifact (``ats_model.pkl``). It trains its own model with a
custom **sign-aware objective** and writes results/pkl under this file's own
``output/`` directory.

Goal
----
The production ATS model optimizes RMSE on run differential (margin), and derives
winner/cover from ``sign(margin)``. Here we keep the SAME regressor but replace
the objective with a convex blend of margin error and *winner (sign) error*, so
the model is directly pressured to get the side right — not just the magnitude.

Custom objective
----------------
Loss = alpha * L2_margin + (1 - alpha) * BCE_winner

where L2_margin  = 0.5 * (pred - y)^2  (mean squared margin error)
      BCE_winner = log(1 + exp(-y * pred))  (logistic loss on winner sign)

So alpha=1.0  -> identical to plain RMSE regression (baseline sanity check)
   alpha=0.0  -> pure winner classification
   alpha=0.5  -> blend (default)

We derive grad/hess analytically so it plugs straight into xgb.train's custom
objective. NOTE: XGBoost 3.x custom objectives receive the CURRENT model output
``preds`` (which for reg:squarederror is in raw margin space). We fit on raw
margin space everywhere, so no eta/link rescaling is needed.

Usage
-----
    cd backend
    PYTHONPATH=$PWD ../venv/bin/python \
      app/scripts/experiments/mlb_xgb_ats_signaware_experiment.py \
        --alpha 0.5 --test-years 2024 2025 2026 --train-from 2016

Flags
-----
    --alpha FLOAT        blend weight (1.0=BLA, 0.0=winner-only, default 0.5)
    --test-years LIST    seasons to backtest (default 2024 2025 2026)
    --train-from INT     first training season (default 2016)
    --baseline           also run the plain-RMSE (alpha=1.0) model for comparison
"""

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

import sys

# Make backend package importable when run as a plain script
REPO_ROOT = Path(__file__).resolve().parents[4]  # backend/../../ -> earl-knows-football
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.handicapping.mlb.data_loader import (
    get_data_loader,
    build_features as mlb_build_features,
    get_model_features,
)
from app.handicapping.mlb.mlb_xgb_model_ats import (
    MLB_PKL_DIR,
    _compute_decay_weights,
    DEFAULT_TIME_DECAY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
log = logging.getLogger("earl.mlb_ats_signaware")
log.setLevel(logging.INFO)

# ── Output directory (isolated from production artifacts) ──
OUT_DIR = Path(__file__).resolve().parent / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_JSON = OUT_DIR / "signaware_results.json"
PKL_DIR = OUT_DIR / "pkls"
PKL_DIR.mkdir(parents=True, exist_ok=True)


def _sig(x: np.ndarray) -> np.ndarray:
    """Numerically safe sigmoid."""
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30.0, 30.0)))


def sign_aware_objective(alpha: float):
    """
    Custom objective factory.

    loss = alpha * 0.5*(p-y)^2 + (1-alpha) * log(1 + exp(-y*p))

    Returns a callable ``obj(preds, dtrain) -> (grad, hess)`` for xgb.train.
    """
    def obj(preds: np.ndarray, dtrain: xgb.DMatrix):
        y = dtrain.get_label()          # actual margin (home perspective)
        p = preds.astype(np.float64)
        yf = y.astype(np.float64)

        sig = _sig(p)

        # ── margin (L2) component ──
        grad_l2 = (p - yf)
        hess_l2 = np.ones_like(p)

        # ── winner (BCE) component, using logistic-link chain rule ──
        # loss = log(1 + exp(-y*p)); model output p is the raw margin, and we
        # parameterize P(home wins) = sigmoid(p). d/dp = -y*sigmoid(-y*p).
        s_yp = _sig(-yf * p)
        grad_bce = -yf * s_yp
        hess_bce = (yf ** 2) * s_yp * (1.0 - s_yp)

        grad = alpha * grad_l2 + (1.0 - alpha) * grad_bce
        hess = alpha * hess_l2 + (1.0 - alpha) * hess_bce
        return grad, hess

    return obj


def _rmse_metric(preds: np.ndarray, dtrain: xgb.DMatrix):
    """RMSE on raw margin space, used as the eval metric for early stopping."""
    y = dtrain.get_label()
    preds = np.asarray(preds, dtype=np.float64).reshape(y.shape[0])
    return "rmse", float(np.sqrt(np.mean((preds - y) ** 2)))


def _run_one_year(
    feats: pd.DataFrame,
    test_year: int,
    train_from: int,
    alpha: float,
    present: list[str],
) -> dict:
    train_feats = feats[feats["season_year"] < test_year].copy()
    test_feats = feats[feats["season_year"] == test_year].copy()

    # ATS training requires betting lines
    train_mask = train_feats["spread"].notna() & train_feats["home_moneyline"].notna()
    test_mask = test_feats["spread"].notna() & test_feats["home_moneyline"].notna()
    train_feats = train_feats[train_mask].copy()
    test_feats = test_feats[test_mask].copy()

    if len(train_feats) < 50 or len(test_feats) < 10:
        return {"test_year": test_year, "skipped": True, "reason": "insufficient data"}

    # Time-ordered early-stopping split (most recent ~15% as eval set), matching
    # production so the experiment is directly comparable.
    idx = train_feats["game_date"].argsort().to_numpy()
    tr = train_feats.iloc[idx]
    X_all = tr[present].values
    y_all = tr["actual_margin"].values
    ew_all = _compute_decay_weights(tr, max(tr["season_year"]))

    n_eval = max(int(len(X_all) * 0.15), 50)
    X_train, y_train, ew_train = X_all[:-n_eval], y_all[:-n_eval], ew_all[:-n_eval]
    X_eval, y_eval, ew_eval = X_all[-n_eval:], y_all[-n_eval:], ew_all[-n_eval:]

    dtrain = xgb.DMatrix(X_train, label=y_train, weight=ew_train)
    deval = xgb.DMatrix(X_eval, label=y_eval, weight=ew_eval)

    params = {
        "max_depth": 5,
        "eta": 0.04,
        "subsample": 0.8,
        "colsample_bytree": 0.6,
        "reg_lambda": 1.0,
        "gamma": 0.1,
        "min_child_weight": 3,
        "disable_default_eval_metric": 1,
    }

    # eval_metric is defined on raw margin space
    evals_result = {}
    model = xgb.train(
        params,
        dtrain,
        num_boost_round=600,
        obj=sign_aware_objective(alpha),
        evals=[(deval, "eval")],
        evals_result=evals_result,
        custom_metric=_rmse_metric,
        early_stopping_rounds=30,
        verbose_eval=False,
    )

    best_iter = model.best_iteration if model.best_iteration is not None else 600

    # ── Evaluate on test set ──
    X_test = test_feats[present].values
    y_test = test_feats["actual_margin"].values
    raw_pred = model.predict(xgb.DMatrix(X_test), iteration_range=(0, best_iter + 1))

    # Decode raw margin -> predicted margin (identity link here since we fit
    # on raw space; kept explicit so it's easy to swap a logistic tail later).
    y_pred = raw_pred

    # Winner accuracy from predicted margin sign
    ml_pred_home = y_pred > 0
    ml_actual_home = test_feats["home_score"].values > test_feats["away_score"].values
    ml_acc = float(np.mean(ml_pred_home == ml_actual_home)) if len(ml_actual_home) else 0.5

    # ATS accuracy (home covers if margin + spread > 0)
    spread = test_feats["spread"].values
    valid = ~np.isnan(spread) & ~np.isnan(y_test)
    pred_ats = y_pred[valid]
    test_ats = y_test[valid]
    spread_ats = spread[valid]
    n_ats = int(np.sum(valid))
    ats_correct = np.sign(pred_ats + spread_ats) == np.sign(test_ats + spread_ats)
    ats_acc = float(np.mean(ats_correct)) if n_ats else 0.5

    from sklearn.metrics import mean_absolute_error
    mae = float(mean_absolute_error(y_test, y_pred))

    imp = sorted(
        zip(present, model.get_score(importance_type="gain")),
        key=lambda x: x[1],
        reverse=True,
    )
    imp = [f for f in present if f in dict(imp)]
    feature_importance = [
        {"feature": f, "importance": round(float(model.get_score(importance_type="gain").get(f, 0.0)), 6)}
        for f in present
    ]

    return {
        "test_year": test_year,
        "alpha": alpha,
        "best_iter": int(best_iter),
        "rows": {"train": int(len(X_train)), "eval": int(len(X_eval)), "test": int(len(test_feats))},
        "n_ats_with_data": n_ats,
        "mae": round(mae, 3),
        "ml": {"total": len(test_feats), "correct": int(np.sum(ml_pred_home == ml_actual_home)), "pct": round(ml_acc * 100, 2)},
        "ats": {"total": n_ats, "correct": int(np.sum(ats_correct)), "pct": round(ats_acc * 100, 2)},
        "feature_importance": feature_importance,
    }


async def run_experiment(alpha: float, test_years: list[int], train_from: int) -> list[dict]:
    raw = get_data_loader().load_games(status="FINAL")
    feats = mlb_build_features(raw)
    fcols = get_model_features("ats")
    # Column alias mapping identical to production
    col_map = {"ha_tz": "tz_diff", "aa_tz": "tz_diff"}
    fcols = [col_map.get(c, c) for c in fcols]
    present = [c for c in fcols if c in feats.columns]
    missing = [c for c in fcols if c not in feats.columns]
    if missing:
        log.warning(f"Missing features: {missing}")
    log.info(f"Using {len(present)}/{len(fcols)} features; {len(raw)} games loaded")

    results = []
    for year in test_years:
        log.info(f"=== Sign-aware backtest {year} (alpha={alpha}) ===")
        r = _run_one_year(feats, year, train_from, alpha, present)
        results.append(r)
        if not r.get("skipped"):
            log.info(
                f"  {year}: MAE={r['mae']}  ML={r['ml']['pct']}%  ATS={r['ats']['pct']}%  "
                f"(iter={r['best_iter']}, train={r['rows']['train']})"
            )
    return results


def main():
    ap = argparse.ArgumentParser(description="MLB ATS sign-aware objective experiment")
    ap.add_argument("--alpha", type=float, default=0.5, help="blend weight: 1.0=margin-only, 0.0=winner-only")
    ap.add_argument("--test-years", type=int, nargs="+", default=[2024, 2025, 2026])
    ap.add_argument("--train-from", type=int, default=2016)
    args = ap.parse_args()

    log.info(f"Alpha={args.alpha}  TestYears={args.test_years}  TrainFrom={args.train_from}")
    results = asyncio.run(run_experiment(args.alpha, args.test_years, args.train_from))

    # Persist
    payload = {
        "experiment": "sign_aware_ats",
        "alpha": args.alpha,
        "test_years": args.test_years,
        "train_from": args.train_from,
        "ts": time.time(),
        "results": results,
    }
    RESULTS_JSON.write_text(json.dumps(payload, indent=2, default=str))
    log.info(f"Wrote results -> {RESULTS_JSON}")

    # Summary table
    print("\n=== Summary ===")
    print(f"{'year':>6} {'alpha':>5} {'MAE':>7} {'ML%':>6} {'ATS%':>6} {'n_test':>7} {'iter':>5}")
    for r in results:
        if r.get("skipped"):
            print(f"{r['test_year']:>6}  skipped ({r.get('reason')})")
        else:
            print(
                f"{r['test_year']:>6} {r['alpha']:>5} {r['mae']:>7} "
                f"{r['ml']['pct']:>6} {r['ats']['pct']:>6} {r['rows']['test']:>7} {r['best_iter']:>5}"
            )
    print(f"\nResults JSON: {RESULTS_JSON}")


if __name__ == "__main__":
    main()
