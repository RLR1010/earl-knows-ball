"""
Two-Model Fusion ATS Experiment — MLB XGBoost.

EXPERIMENT — standalone. Does NOT touch production ``mlb_xgb_model_ats.py`` /
``ats_model.pkl``. Writes its own output under ``output/``.

Idea
----
The single margin-regressor can be *confidently wrong on the side* in near-push
games (small |margin|), where a tiny prediction error flips the ATS result. This
experiment trains a SECOND model — a winner classifier (P(home wins)) — and fuses
it with the margin regressor. We compare four decision strategies on the SAME
held-out test sets:

    BASE  : regressor only (control = current production decision)           ATS = sign(pred+spread)
    F1    : classifier decides winner, regressor decides cover               pick = p_home>0.5; ATS = sign(pred+spread)
    F2    : shrink regressor margin toward 0 by classifier disagreement       pred_mixed = w*pred + (1-w)*conf_shrink*flip
    F3    : on near-push spreads only, trust the classifier                 if |pred+spread|<THRESH: ATS = classifier cover sign

"Cover sign" from classifier: P(cover) ~ sigmoid(k*(2*p_home-1)) used as hedge in
F3 (see implementation). All four use the same features and the same train/eval
split so the comparison is apples-to-apples.

Usage
-----
    cd backend
    PYTHONPATH=$PWD ../venv/bin/python \\
      app/scripts/experiments/mlb_xgb_ats_fusion_experiment.py \\
        --test-years 2021 2022 2023 2024 2025 2026 --train-from 2016 --push-thresh 1.5
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import mean_absolute_error

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.handicapping.mlb.data_loader import (
    get_data_loader,
    build_features as mlb_build_features,
    get_model_features,
)
from app.handicapping.mlb.mlb_xgb_model_ats import (
    _compute_decay_weights,
    DEFAULT_TIME_DECAY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
log = logging.getLogger("earl.mlb_ats_fusion")
log.setLevel(logging.INFO)

OUT_DIR = Path(__file__).resolve().parent / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_JSON = OUT_DIR / "fusion_results.json"


def _build_splits(feats: pd.DataFrame, test_year: int, train_from: int, present: list[str]):
    train_feats = feats[(feats["season_year"] < test_year) & (feats["season_year"] >= train_from)].copy()
    test_feats = feats[feats["season_year"] == test_year].copy()

    m = "spread"
    train_feats = train_feats[train_feats[m].notna()].copy()
    test_feats = test_feats[test_feats[m].notna()].copy()

    # time-ordered eval split (most recent ~15%)
    idx = train_feats["game_date"].argsort().to_numpy()
    tr = train_feats.iloc[idx]
    X = tr[present].values
    y = tr["actual_margin"].values
    ew = _compute_decay_weights(tr, max(tr["season_year"]))
    n_eval = max(int(len(X) * 0.15), 50)
    X_tr, y_tr, ew_tr = X[:-n_eval], y[:-n_eval], ew[:-n_eval]
    X_ev, y_ev, ew_ev = X[-n_eval:], y[-n_eval:], ew[-n_eval:]

    X_test = test_feats[present].values
    y_test = test_feats["actual_margin"].values
    spread_test = test_feats["spread"].values
    return X_tr, y_tr, ew_tr, X_ev, y_ev, ew_ev, X_test, y_test, spread_test, test_feats


def _train_regressor(X_tr, y_tr, ew_tr, X_ev, y_ev, ew_ev):
    dtrain = xgb.DMatrix(X_tr, label=y_tr, weight=ew_tr)
    deval = xgb.DMatrix(X_ev, label=y_ev, weight=ew_ev)
    params = {
        "max_depth": 5, "eta": 0.04, "subsample": 0.8, "colsample_bytree": 0.6,
        "reg_lambda": 1.0, "gamma": 0.1, "min_child_weight": 3, "objective": "reg:squarederror",
    }
    evals_result = {}
    model = xgb.train(
        params, dtrain, num_boost_round=600,
        evals=[(deval, "eval")], evals_result=evals_result,
        early_stopping_rounds=30, verbose_eval=False,
    )
    best = model.best_iteration if model.best_iteration is not None else 600
    return model, best


def _train_classifier(X_tr, y_tr, ew_tr, X_ev, y_ev, ew_ev):
    ybin_tr = (y_tr > 0).astype(int)
    ybin_ev = (y_ev > 0).astype(int)
    dtrain = xgb.DMatrix(X_tr, label=ybin_tr, weight=ew_tr)
    deval = xgb.DMatrix(X_ev, label=ybin_ev, weight=ew_ev)
    params = {
        "max_depth": 5, "eta": 0.05, "subsample": 0.8, "colsample_bytree": 0.6,
        "reg_lambda": 1.0, "gamma": 0.1, "min_child_weight": 3, "objective": "binary:logistic",
    }
    evals_result = {}
    model = xgb.train(
        params, dtrain, num_boost_round=600,
        evals=[(deval, "eval")], evals_result=evals_result,
        early_stopping_rounds=30, verbose_eval=False,
    )
    best = model.best_iteration if model.best_iteration is not None else 600
    return model, best


def _acc(mask, correct):
    mask = mask.astype(bool)
    n = int(np.sum(mask))
    if n == 0:
        return {"total": 0, "correct": 0, "pct": 0.0}
    return {"total": n, "correct": int(np.sum(correct[mask])), "pct": round(float(np.mean(correct[mask])) * 100, 2)}


def _run_year(feats, test_year, train_from, present, push_thresh):
    (X_tr, y_tr, ew_tr, X_ev, y_ev, ew_ev, X_test, y_test, spread_test, _tf) = _build_splits(
        feats, test_year, train_from, present
    )
    if len(X_tr) < 50 or len(X_test) < 10:
        return {"test_year": test_year, "skipped": True}

    reg, reg_best = _train_regressor(X_tr, y_tr, ew_tr, X_ev, y_ev, ew_ev)
    clf, clf_best = _train_classifier(X_tr, y_tr, ew_tr, X_ev, y_ev, ew_ev)

    pred_margin = reg.predict(xgb.DMatrix(X_test), iteration_range=(0, reg_best + 1))
    phome = clf.predict(xgb.DMatrix(X_test), iteration_range=(0, clf_best + 1))  # P(home wins)
    margin = pred_margin
    y = y_test
    spread = spread_test

    # ---- decisions ----
    n = len(y)
    # BASE: pure regressor
    ats_cov_base = (margin + spread) > 0
    ats_act = (y + spread) > 0
    base_correct = ats_cov_base == ats_act
    ml_correct_base = (margin > 0) == (y > 0)

    # F1: classifier decides winner; regressor decides cover given winner
    #   home predicted winner if phome>0.5; away if phome<=0.5.
    #   cover = (pred winner == actual cover sign) — we use regressor margin for
    #   cover magnitude but force the *winner side* to match the classifier.
    pred_home_win = phome > 0.5
    # F1 cover: if classifier says home wins, cover if margin+spread>0 (regressor's
    # magnitude on the home side); if away wins, cover if -(margin)+spread? Simplify:
    # ATS on the team the classifier picks. Home ATS = margin+spread; Away ATS = -(margin)+spread.
    # Classifier picks home -> bet home ATS = sign(margin+spread).
    # Classifier picks away -> bet away ATS = sign(-(margin)+spread) = spread>margin.
    home_ats_val = margin + spread
    # AWAY side: the away team's betting line is the NEGATIVE of the home-perspective
    # spread. Away covers iff away_margin + away_line > 0, i.e. (-margin) + (-spread) > 0
    # = -(margin + spread) > 0  <==>  (margin + spread) < 0.
    # (Equivalent and simpler: home covers iff margin+spread>0; away covers iff the
    #  OPPOSITE, i.e. margin+spread < 0, since one side always covers the same spread.)
    away_ats_val = -(margin + spread)  # away covers iff this > 0
    f1_cover = np.where(pred_home_win, home_ats_val > 0, away_ats_val > 0)
    # Ground truth: home covers iff y+spread>0; away covers iff y+spread<0.
    actual_home_covered = (y + spread) > 0
    actual_away_covered = (y + spread) < 0
    f1_correct = np.where(pred_home_win, (f1_cover == actual_home_covered), (f1_cover == actual_away_covered))

    # F2: shrink regressor margin toward 0 when classifier strongly disagrees on winner.
    # fusion_weight w in [~0.35, 1]; when classifier is very confident of a winner the
    # regressor "missed" (y>0 vs phome>0.5), pull margin toward that winner.
    conf = np.abs(2 * phome - 1.0)  # 0..1 classifier confidence
    agree = (margin > 0) == (phome > 0.5)  # regressor side agrees with classifier
    # when they agree, trust regressor; when disagree, pull toward classifier's sign scaled by conf
    sign_clf = np.where(phome > 0.5, 1.0, -1.0)
    w = np.where(agree, 1.0, 0.4 * (1.0 - conf) + 0.1 * conf)  # pull margin toward classifier on disagreement
    margin_f2 = w * margin + (1 - w) * sign_clf * np.abs(margin) * 1.2
    f2_correct = ((margin_f2 + spread) > 0) == ((y + spread) > 0)

    # moneyline for each strategy (winner pick vs actual winner)
    ml_correct_f1 = pred_home_win == (y > 0)
    ml_correct_f2 = (margin_f2 > 0) == (y > 0)
    ml_correct_f3 = ml_correct_base.copy()  # F3 only alters cover, not winner side

    # F3: on near-push spreads only, defer to classifier cover signal.
    near_push = np.abs(home_ats_val) <= push_thresh
    # classifier's cover proxy: if confident home, trust home_ats sign; else away.
    clf_cover_proxy = np.where(phome > 0.5, phome, 1 - phome)  # confidence in picked side
    clf_cover_correct = np.where(pred_home_win, actual_home_covered, actual_away_covered)
    f3_correct = base_correct.copy()
    f3_correct[near_push] = clf_cover_correct[near_push]

    mae = float(mean_absolute_error(y, margin))

    def agg(correct_full, subset_mask=None):
        mask = np.ones(n, dtype=bool) if subset_mask is None else subset_mask
        return _acc(mask, correct_full)

    return {
        "test_year": test_year,
        "rows_test": n,
        "mae": round(mae, 3),
        "reg_iter": int(reg_best),
        "clf_iter": int(clf_best),
        "base": {"ats": agg(base_correct), "ml": agg(ml_correct_base)},
        "f1": {"ats": agg(f1_correct), "ml": agg(ml_correct_f1)},
        "f2": {"ats": agg(f2_correct), "ml": agg(ml_correct_f2)},
        "f3": {"ats": agg(f3_correct), "ml": agg(ml_correct_f3)},
        "near_push": {"total": int(np.sum(near_push)), "base_ats": agg(base_correct, near_push), "f3_ats": agg(f3_correct, near_push)},
    }


async def run_experiment(test_years, train_from, push_thresh):
    raw = get_data_loader().load_games(status="FINAL")
    feats = mlb_build_features(raw)
    fcols = get_model_features("ats")
    col_map = {"ha_tz": "tz_diff", "aa_tz": "tz_diff"}
    fcols = [col_map.get(c, c) for c in fcols]
    present = [c for c in fcols if c in feats.columns]
    missing = [c for c in fcols if c not in feats.columns]
    if missing:
        log.warning(f"Missing features: {missing}")
    log.info(f"Using {len(present)}/{len(fcols)} features; {len(raw)} games")

    results = []
    for year in test_years:
        log.info(f"=== Fusion backtest {year} ===")
        r = _run_year(feats, year, train_from, present, push_thresh)
        results.append(r)
        if not r.get("skipped"):
            b = r["base"]["ats"]["pct"]
            f1 = r["f1"]["ats"]["pct"]
            f2 = r["f2"]["ats"]["pct"]
            f3 = r["f3"]["ats"]["pct"]
            log.info(f"  {year}: BASE={b}% F1={f1}% F2={f2}% F3={f3}%  (n={r['rows_test']})")
    return results


def main():
    ap = argparse.ArgumentParser(description="MLB ATS two-model fusion experiment")
    ap.add_argument("--test-years", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025, 2026])
    ap.add_argument("--train-from", type=int, default=2016)
    ap.add_argument("--push-thresh", type=float, default=1.5, help="|margin+spread| <= thresh => near-push (F3)")
    args = ap.parse_args()

    log.info(f"TestYears={args.test_years} TrainFrom={args.train_from} PushThresh={args.push_thresh}")
    results = asyncio_run(run_experiment(args.test_years, args.train_from, args.push_thresh))

    payload = {
        "experiment": "fusion_ats",
        "test_years": args.test_years,
        "train_from": args.train_from,
        "push_thresh": args.push_thresh,
        "ts": time.time(),
        "results": results,
    }
    RESULTS_JSON.write_text(json.dumps(payload, indent=2, default=str))
    log.info(f"Wrote -> {RESULTS_JSON}")

    print("\n=== Summary (ATS%  |  ML%) ===")
    print(f"{'year':>6} {'n':>5} | {'BASE':>14} {'F1':>14} {'F2':>14} {'F3':>14}")
    for r in results:
        if r.get("skipped"):
            print(f"{r['test_year']:>6}  skipped")
        else:
            def fmt(s):
                return f"{s['ats']['pct']:.1f}/{s['ml']['pct']:.1f}"
            print(
                f"{r['test_year']:>6} {r['rows_test']:>5} | "
                f"{fmt(r['base']):>14} {fmt(r['f1']):>14} "
                f"{fmt(r['f2']):>14} {fmt(r['f3']):>14}"
            )
    print("\n  format: ATS%/ML%  (ATS = sign(pred+spread) vs actual; ML = winner pick vs actual)")


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


if __name__ == "__main__":
    main()
