"""
NFL handicapping engine — load models, predict games, build pick cards,
run backtests, and save results to the database.

Mirrors ``mlb/mlb_engine.py`` but adapted for NFL (spread-based betting,
no run line, scores instead of runs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import xgboost as xgb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.handicapping.nfl.data_loader import NFLDataLoader, get_data_loader, get_model_features

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────────
# Match the same directory nfl_xgb_model_ats.py / nfl_xgb_model_ou.py save to:
MODELS_DIR = Path.home() / ".openclaw" / "workspace" / "earl-knows-football" / "data" / "models" / "nfl"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

_now = datetime.now()
CURRENT_SEASON = _now.year if _now.month >= 4 else _now.year - 1
DB_DSN: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)
NFL_SCHEMA = "nfl"

# ── Async DB setup ───────────────────────────────────────────────────────────────
ASYNC_DSN: str = DB_DSN.replace("postgresql://", "postgresql+asyncpg://")
_async_engine = None
_async_sessionmaker = None


# ── Pick-card feature extraction ────────────────────────────────────────────────


def _extract_pick_card_features(row, feature_names: set) -> str:
    """Return JSON string of pick_card feature values from a DataFrame row.
    Ensures NaN/Inf floats are converted to None to keep stored JSON valid.
    """

    def _sanitize(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    features = {
        name: _sanitize(row.get(name))
        for name in feature_names
        if name in row.index or name in row
    }
    return json.dumps(features, default=str)


def _get_async_session() -> async_sessionmaker:
    global _async_engine, _async_sessionmaker
    if _async_sessionmaker is None:
        _async_engine = create_async_engine(ASYNC_DSN, echo=False, pool_pre_ping=True)
        _async_sessionmaker = async_sessionmaker(_async_engine, expire_on_commit=False)
    return _async_sessionmaker


# ── Model resolution helpers ─────────────────────────────────────────────────────


def _resolve_year_pkl_paths(model_type: str) -> Dict[int, Path]:
    """Return {year: Path} for all per-year model pickle files.

    ``model_type`` is ``'ats'`` or ``'ou'``.

    Loads the pkl_filename list from the current live training run
    (``nfl.training_runs WHERE is_live = 't'``), parses
    ``<uuid>-<year>.pkl`` entries, and resolves them under
    ``backend/app/handicapping/nfl/models/xgboost/``.

    Returns an empty dict when no live run exists or no pkl files found.
    """
    from app.handicapping.db_training import get_live_training_run
    run = get_live_training_run("nfl", model_type)
    if run is None:
        logger.warning("  No live training_run for nfl/%s", model_type)
        return {}

    raw = run.get("pkl_filename", "")
    if not raw:
        logger.warning("  training_run for nfl/%s has empty pkl_filename", model_type)
        return {}

    parts = [s.strip() for s in raw.split(",") if s.strip()]
    out: Dict[int, Path] = {}
    for fname in parts:
        # Expect pattern: <uuid>-<year>.pkl
        stem = fname.rsplit(".", 1)[0]   # remove .pkl
        if "-" in stem:
            year_str = stem.rsplit("-", 1)[-1]
            try:
                year = int(year_str)
            except ValueError:
                continue
            p = MODELS_DIR / fname
            if p.exists():
                out[year] = p
            else:
                logger.warning("  pkl file not found on disk: %s", p)
    if out:
        logger.info("  Year pkl files for nfl/%s: %s", model_type, out)
    else:
        logger.warning("  No year pkl files found for nfl/%s", model_type)
    return out


def _load_model_for_year(model_type: str, year: int) -> Optional[xgb.Booster]:
    """Load a per-year model for a specific calendar year."""
    paths = _resolve_year_pkl_paths(model_type)
    p = paths.get(year)
    if p and p.exists():
        with open(p, "rb") as f:
            model = pickle.load(f)
        logger.info("Loaded %s model for year %d from %s", model_type, year, p)
        return model
    logger.warning("No %s model found for year %d", model_type, year)
    return None


# ── Feature helpers ──────────────────────────────────────────────────────────────

_FEATURES_CACHE_ATS: Optional[List[str]] = None
_FEATURES_CACHE_OU: Optional[List[str]] = None


def _get_features(model_type: str) -> List[str]:
    """Return feature columns from ``nfl.features`` for the given model type.

    Queries for features flagged ``live_ats = TRUE`` or ``live_ou = TRUE``
    (the flags that ``set_training_run_as_current`` marks).
    """
    global _FEATURES_CACHE_ATS, _FEATURES_CACHE_OU
    if model_type == "ats":
        if _FEATURES_CACHE_ATS is not None:
            return _FEATURES_CACHE_ATS
    elif model_type == "ou":
        if _FEATURES_CACHE_OU is not None:
            return _FEATURES_CACHE_OU

    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            if model_type == "ats":
                feats = get_model_features(cur, live_ats_only=True)
                _FEATURES_CACHE_ATS = feats
            else:
                feats = get_model_features(cur, live_ou_only=True)
                _FEATURES_CACHE_OU = feats
    finally:
        conn.close()
    return feats


def _extract_feature_vector(
    row: pd.Series,
    model_type: str,
) -> Tuple[np.ndarray, List[str]]:
    """Build a feature vector (values, names) from a DataFrame row."""
    feature_names = _get_features(model_type)
    values = []
    names = []
    for feat in feature_names:
        val = row.get(feat)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            val = 0.0
        values.append(float(val))
        names.append(feat)
    return np.array([values], dtype=np.float32), names


# ── Main handicapper class ───────────────────────────────────────────────────────


class NFLHandicapper:
    """Handicap NFL games: predict ATS cover and over/under."""

    def __init__(self):
        self.ats_model: Optional[xgb.Booster] = None
        self.ou_model: Optional[xgb.Booster] = None
        self.ats_model_year: Optional[int] = None
        self.ou_model_year: Optional[int] = None
        self._load_models()

    def _load_models(self) -> None:
        """Load ATS and OU models from the current (live) training runs.

        Delegates to ``_resolve_year_pkl_paths`` and ``_load_model_for_year``
        so it stays consistent with the MLB engine pattern.
        """
        ats_paths = _resolve_year_pkl_paths("ats")
        ou_paths = _resolve_year_pkl_paths("ou")
        if ats_paths:
            latest_ats_year = max(ats_paths)
            self.ats_model = _load_model_for_year("ats", latest_ats_year)
            self.ats_model_year = latest_ats_year
            logger.info("Loaded ATS model for year %d", latest_ats_year)
        if ou_paths:
            latest_ou_year = max(ou_paths)
            self.ou_model = _load_model_for_year("ou", latest_ou_year)
            self.ou_model_year = latest_ou_year
            logger.info("Loaded OU model for year %d", latest_ou_year)

    def _ensure_models_for_year(self, year: int) -> None:
        """Load per-year models if they exist and differ from current."""
        ats_paths = _resolve_year_pkl_paths("ats")
        ou_paths = _resolve_year_pkl_paths("ou")
        if year in ats_paths and self.ats_model_year != year:
            self.ats_model = _load_model_for_year("ats", year)
            self.ats_model_year = year
        if year in ou_paths and self.ou_model_year != year:
            self.ou_model = _load_model_for_year("ou", year)
            self.ou_model_year = year

    # ── Core handicap: single game ──────────────────────────────────────────────

    async def handicap_game(
        self,
        game_id: int,
        home_abbr: str,
        away_abbr: str,
        year: Optional[int] = None,
        save_to_db: bool = True,
        source: str = "api",
    ) -> Dict[str, Any]:
        """Produce a full handicap for a single game.

        Returns a dict with predicted scores, spreads, confidence,
        picks, and handicapper info.
        """
        dl = get_data_loader()
        df = dl.load_inference_data(game_ids=[game_id])

        if df.empty:
            return {"error": f"no features for game {game_id}"}

        row = df.iloc[0]

        # ATS prediction
        ats_proba = 0.5
        ats_features_used: List[str] = []
        if self.ats_model:
            feats, names = _extract_feature_vector(row, "ats")
            ats_features_used = names
            try:
                dmat = xgb.DMatrix(feats, feature_names=names)
                ats_proba = float(self.ats_model.predict(dmat)[0])
            except Exception:
                ats_proba = 0.5

        # OU prediction
        ou_pred_total = None
        ou_features_used: List[str] = []
        if self.ou_model:
            feats_ou, names_ou = _extract_feature_vector(row, "ou")
            ou_features_used = names_ou
            try:
                dmat_ou = xgb.DMatrix(feats_ou, feature_names=names_ou)
                ou_pred_total = float(self.ou_model.predict(dmat_ou)[0])
            except Exception:
                ou_pred_total = None

        # Extract game info
        spread = float(row.get("closing_spread", 0) or 0)
        closing_ou = float(row.get("closing_ou", 0) or 0)

        # Pick determination
        spread_pick: Optional[str] = None
        if ats_proba > 0.5:
            spread_pick = f"{home_abbr} {spread:+.1f}" if spread < 0 else f"{home_abbr} {-spread:+.1f}"
        elif ats_proba < 0.5:
            spread_pick = f"{away_abbr} {spread:+.1f}" if spread > 0 else f"{away_abbr} {-spread:+.1f}"

        ou_pick: Optional[str] = None
        if ou_pred_total is not None and closing_ou > 0:
            ou_pick = "Over" if ou_pred_total > closing_ou else "Under"

        ou_edge = None
        if ou_pred_total is not None and closing_ou > 0:
            ou_edge = abs(ou_pred_total - closing_ou)

        # Build result
        # Moneyline odds from data
        home_ml = _float_safe(row.get("closing_home_ml"))
        away_ml = _float_safe(row.get("closing_away_ml"))
        spread_home_odds = _float_safe(row.get("closing_spread_home_odds"))
        spread_away_odds = _float_safe(row.get("closing_spread_away_odds"))
        over_odds = _float_safe(row.get("closing_over_odds"))
        under_odds = _float_safe(row.get("closing_under_odds"))

        # Moneyline pick (favorite from odds)
        ml_pick_team = None
        ml_prob = None
        if home_ml is not None and away_ml is not None and home_ml != 0 and away_ml != 0:
            home_implied = 1 / abs(home_ml) * 100 if home_ml < 0 else 100 / (home_ml + 100)
            away_implied = 1 / abs(away_ml) * 100 if away_ml < 0 else 100 / (away_ml + 100)
            total_implied = home_implied + away_implied
            home_prob = home_implied / total_implied if total_implied > 0 else 0.5
            ml_prob = home_prob
            ml_pick_team = home_abbr if home_prob > 0.5 else away_abbr

        result: Dict[str, Any] = {
            "game_id": game_id,
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "spread": spread,
            "closing_ou": closing_ou,
            "home_ml": home_ml,
            "away_ml": away_ml,
            "spread_home_odds": spread_home_odds,
            "spread_away_odds": spread_away_odds,
            "over_odds": over_odds,
            "under_odds": under_odds,
            "ats_prediction": round(ats_proba, 4),
            "ats_edge": round(abs(ats_proba - 0.5) * 2, 4),
            "ou_predicted_total": round(ou_pred_total, 2) if ou_pred_total else None,
            "ou_edge": round(ou_edge, 2) if ou_edge else None,
            "ou_pick": ou_pick,
            "spread_pick": spread_pick,
            "ml_pick": ml_pick_team,
            "ml_edge": round(abs(ml_prob - 0.5) * 2, 4) if ml_prob else None,
            "ats_features_used": ats_features_used,
            "ou_features_used": ou_features_used,
            "home_stats": _build_nfl_home_stats(row),
            "away_stats": _build_nfl_away_stats(row),
            "situational": _build_nfl_situational(row),
            "splits": _build_nfl_splits(row),
            "source": source,
        }

        # Save to DB
        if save_to_db:
            try:
                await _save_api_prediction(result)
            except Exception as e:
                logger.warning("Failed to save prediction to DB: %s", e)

        return result

    # ── Handicap a set of games ────────────────────────────────────────────────

    async def handicap_games(
        self,
        game_ids: List[int],
        year: Optional[int] = None,
        save_to_db: bool = True,
        source: str = "api",
    ) -> List[Dict[str, Any]]:
        """Handicap multiple games in sequence."""
        dl = get_data_loader()
        df = dl.load_inference_data(game_ids=game_ids)

        results: List[Dict[str, Any]] = []
        for game_id in game_ids:
            row_df = df[df["game_id"] == game_id] if "game_id" in df.columns else df
            if row_df.empty:
                results.append({"game_id": game_id, "error": "no features"})
                continue

            home_abbr = str(row_df.iloc[0].get("home_abbr", ""))
            away_abbr = str(row_df.iloc[0].get("away_abbr", ""))

            result = await self.handicap_game(
                game_id=game_id,
                home_abbr=home_abbr,
                away_abbr=away_abbr,
                year=year,
                save_to_db=save_to_db,
                source=source,
            )
            results.append(result)

        return results

    # ── Backtest ────────────────────────────────────────────────────────────────

    async def backtest(
        self,
        years: Optional[List[int]] = None,
        limit: Optional[int] = None,
        save_results: bool = True,
    ) -> Dict[str, Any]:
        """Backtest NFL models over one or more seasons using pre-saved year-specific pkl files.

        Loads per-year pkl paths from the live training run's ``pkl_filename``
        (stored in ``nfl.training_runs WHERE is_live = 't'``).  No training is
        performed.

        Parameters
        ----------
        years : list of int, optional
            Season years to test.  Defaults to all with pkl files available.
        limit : int, optional
            Max rows to load from the data loader (for quick tests).
        save_results : bool
            Whether to write per-game predictions to ``nfl.game_predictions``.

        Returns
        -------
        dict with ``ats_results``, ``ou_results``, ``test_years``.
        """
        ats_paths = _resolve_year_pkl_paths("ats")
        ou_paths = _resolve_year_pkl_paths("ou")

        if not ats_paths and not ou_paths:
            return {"error": "no live training run found with pkl files"}

        if years is None:
            # Default: 2024 and 2025 (must have pkl files)
            years = [y for y in [2024, 2025] if y in ats_paths or y in ou_paths]

        logger.info("Backtest years (from pkl): %s", years)

        dl = get_data_loader()
        df = dl.load_data(limit=limit)
        if df.empty:
            return {"error": "no data"}

        df["total_points"] = df["home_score"] + df["away_score"]

        # Data is already feature-engineered from load_data()
        pass
        df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

        ats_results: List[Dict[str, Any]] = []
        ou_results: List[Dict[str, Any]] = []

        for test_year in years:
            # ── ATS evaluation for this year ──
            if test_year in ats_paths:
                model = _load_model_for_year("ats", test_year)
                if model is not None:
                    year_df = df[df["season_year"] == test_year].copy()
                    if year_df.empty:
                        logger.warning("  No data for %d ATS test", test_year)
                        ats_results.append({"year": test_year, "error": "no data"})
                    else:
                        ats_res = _evaluate_year_model(year_df, model, "ats")
                        ats_res["year"] = test_year
                        ats_results.append(ats_res)
                else:
                    ats_results.append({"year": test_year, "error": "model load failed"})
            else:
                logger.info("  No ATS pkl for year %d, skipping", test_year)

            # ── OU evaluation for this year ──
            if test_year in ou_paths:
                model = _load_model_for_year("ou", test_year)
                if model is not None:
                    year_df = df[df["season_year"] == test_year].copy()
                    if year_df.empty:
                        logger.warning("  No data for %d OU test", test_year)
                        ou_results.append({"year": test_year, "error": "no data"})
                    else:
                        ou_res = _evaluate_year_model(year_df, model, "ou")
                        ou_res["year"] = test_year
                        ou_results.append(ou_res)
                else:
                    ou_results.append({"year": test_year, "error": "model load failed"})
            else:
                logger.info("  No OU pkl for year %d, skipping", test_year)

        # Build per-game pick cards
        if save_results:
            for test_year in years:
                year_df = df[df["season_year"] == test_year].copy()
                if year_df.empty:
                    continue

                ats_model = _load_model_for_year("ats", test_year) if test_year in ats_paths else None
                ou_model = _load_model_for_year("ou", test_year) if test_year in ou_paths else None
                if ats_model is None and ou_model is None:
                    continue

                for idx, row in year_df.iterrows():
                    game_key = (row["season_year"], row["week"], row["home_abbr"], row["away_abbr"])

                    ats_proba = None
                    ou_pred = None

                    if ats_model is not None:
                        feat_vals, feat_names = _extract_feature_vector(row, "ats")
                        dmat = xgb.DMatrix(feat_vals, feature_names=feat_names)
                        ats_proba = float(ats_model.predict(dmat)[0])

                    if ou_model is not None:
                        feat_vals, feat_names = _extract_feature_vector(row, "ou")
                        dmat = xgb.DMatrix(feat_vals, feature_names=feat_names)
                        ou_pred_db = float(ou_model.predict(dmat)[0])
                        ou_pred = max(0, min(100, ou_pred_db))

                    ats_feats = _get_features("ats") if ats_model is not None else None
                    ou_feats = _get_features("ou") if ou_model is not None else None
                    await _save_backtest_prediction(
                        row.get("game_id", 0), row,
                        ats_model=ats_model,
                        ou_model=ou_model,
                        ats_features=ats_feats,
                        ou_features=ou_feats,
                    )

        return {
            "ats_results": ats_results,
            "ou_results": ou_results,
            "n_years": len([r for r in ats_results if "error" not in r]) + len([r for r in ou_results if "error" not in r]),
            "test_years": years,
        }


def _evaluate_year_model(year_df: pd.DataFrame, model: xgb.Booster, model_type: str) -> Dict[str, Any]:
    """Evaluate a single per-year model on a full season's games.

    Returns dict with accuracy (ATS) or MAE/RMSE (OU), AUC, and game-level
    predictions (list of dicts).  Matches the MLB ``backtest_season`` pattern.
    """
    from sklearn.metrics import roc_auc_score, mean_absolute_error, mean_squared_error

    labels = []
    probs = []
    total = 0
    correct = 0

    for idx, row in year_df.iterrows():
        feat_vals, feat_names = _extract_feature_vector(row, model_type)
        dmat = xgb.DMatrix(feat_vals, feature_names=feat_names)
        prob = float(model.predict(dmat)[0])
        probs.append(prob)

        if model_type == "ats":
            spread = row.get("spread", 0) or 0
            home_score = row.get("home_score", 0) or 0
            away_score = row.get("away_score", 0) or 0
            actual_cover = int((home_score - away_score + spread) > 0)
            labels.append(actual_cover)
            total += 1
            if int(prob > 0.5) == actual_cover:
                correct += 1
        else:
            total_points = row.get("total_points", 0) or 0
            labels.append(total_points)

    result: Dict[str, Any] = {
        "total_games": total,
    }

    if model_type == "ats" and total > 0:
        result["accuracy"] = correct / total
        result["correct"] = correct
        result["n_train"] = 0
        result["n_test"] = total
        if len(set(labels)) > 1 and len(probs) > 1:
            try:
                result["auc"] = round(float(roc_auc_score(labels, probs)), 4)
            except Exception:
                result["auc"] = None
        else:
            result["auc"] = None
    elif model_type == "ou" and len(labels) > 0:
        result["mae"] = round(float(mean_absolute_error(labels, probs)), 2)
        result["rmse"] = round(float(np.sqrt(mean_squared_error(labels, probs))), 2)
        result["n_train"] = 0
        result["n_test"] = len(labels)

    return result


# ── Save API prediction ─────────────────────────────────────────────────────────


async def _save_api_prediction(result: Dict[str, Any]) -> None:
    """Save a single game prediction to ``nfl.game_predictions`` (async).

    Mirrors the MLB engine pattern: writes moneyline pick, odds, EV, and stats.
    """
    game_id = result.get("game_id")
    if not game_id:
        return

    now = datetime.now(timezone.utc)
    source = result.get("source", "api")

    predicted_total = result.get("ou_predicted_total")
    ats_proba = result.get("ats_prediction", 0.5)
    spread = result.get("spread", 0) or 0

    pred_margin = 2 * spread * (ats_proba - 0.5) if spread != 0 else 0
    pred_home_score = None
    pred_away_score = None
    if predicted_total is not None:
        pred_home_score = max(0, round((predicted_total + pred_margin) / 2.0))
        pred_away_score = max(0, round((predicted_total - pred_margin) / 2.0))

    ou_pick = result.get("ou_pick")
    spread_pick = result.get("spread_pick")
    ml_pick = result.get("ml_pick")

    # Map odds to the pick side
    ats_odds_value = None
    sp_pick = spread_pick or ""
    home_abbr = result.get("home_abbr", "")
    away_abbr = result.get("away_abbr", "")
    if sp_pick:
        pick_team = sp_pick.split(" ")[0]  # "BAL -6.0" -> "BAL"
        if pick_team == home_abbr:
            ats_odds_value = result.get("spread_home_odds")
        elif pick_team == away_abbr:
            ats_odds_value = result.get("spread_away_odds")

    ou_odds_value = None
    if ou_pick == "Over":
        ou_odds_value = result.get("over_odds")
    elif ou_pick == "Under":
        ou_odds_value = result.get("under_odds")

    ml_odds_value = None
    if ml_pick == home_abbr:
        ml_odds_value = result.get("home_ml")
    elif ml_pick == away_abbr:
        ml_odds_value = result.get("away_ml")

    home_stats = result.get("home_stats")
    away_stats = result.get("away_stats")
    situational = result.get("situational")
    splits = result.get("splits")

    session_maker = _get_async_session()
    async with session_maker() as session:
        await session.execute(
            text(f"DELETE FROM {NFL_SCHEMA}.game_predictions WHERE game_id = :gid AND source = :src"),
            {"gid": game_id, "src": source},
        )

        insert_sql = text(f"""
            INSERT INTO {NFL_SCHEMA}.game_predictions
                (game_id, predicted_home_score, predicted_away_score,
                 predicted_total, predicted_margin, margin_conf,
                 ou_conf, ou_pick, spread_pick, ml_pick,
                 ats_odds, ou_odds, ml_odds,
                 ats_ev, ou_ev, ml_ev,
                 home_stats_json, away_stats_json, situational_json, splits_json,
                 source, created_at)
            VALUES
                (:gid, :phs, :pas, :pt, :pm, :mc,
                 :oc, :op, :spick, :mlp,
                 :ats_odds, :ou_odds, :ml_odds,
                 :ats_ev, :ou_ev, :ml_ev,
                 :hs, :aws, :sit, :spl, :src, :ca)
            ON CONFLICT (game_id, source)
            DO UPDATE SET
                predicted_home_score = EXCLUDED.predicted_home_score,
                predicted_away_score = EXCLUDED.predicted_away_score,
                predicted_total = EXCLUDED.predicted_total,
                predicted_margin = EXCLUDED.predicted_margin,
                margin_conf = EXCLUDED.margin_conf,
                ou_conf = EXCLUDED.ou_conf,
                ou_pick = EXCLUDED.ou_pick,
                spread_pick = EXCLUDED.spread_pick,
                ml_pick = EXCLUDED.ml_pick,
                ats_odds = EXCLUDED.ats_odds,
                ou_odds = EXCLUDED.ou_odds,
                ml_odds = EXCLUDED.ml_odds,
                ats_ev = EXCLUDED.ats_ev,
                ou_ev = EXCLUDED.ou_ev,
                ml_ev = EXCLUDED.ml_ev,
                home_stats_json = EXCLUDED.home_stats_json,
                away_stats_json = EXCLUDED.away_stats_json,
                situational_json = EXCLUDED.situational_json,
                splits_json = EXCLUDED.splits_json,
                created_at = EXCLUDED.created_at
        """)

        await session.execute(insert_sql, {
            "gid": game_id,
            "phs": pred_home_score,
            "pas": pred_away_score,
            "pt": predicted_total,
            "pm": round(pred_margin, 2),
            "mc": round(abs(ats_proba - 0.5) * 2, 4),
            "oc": round(result.get("ou_edge", 0), 2),
            "op": ou_pick,
            "spick": spread_pick,
            "mlp": ml_pick,
            "ats_odds": round(ats_odds_value) if ats_odds_value else None,
            "ou_odds": round(ou_odds_value) if ou_odds_value else None,
            "ml_odds": round(ml_odds_value) if ml_odds_value else None,
            "ats_ev": round(result.get("ats_edge", 0), 4),
            "ou_ev": round(result.get("ou_edge", 0), 4),
            "ml_ev": round(result.get("ml_edge", 0), 4),
            "hs": json.dumps(home_stats) if home_stats else None,
            "aws": json.dumps(away_stats) if away_stats else None,
            "sit": json.dumps(situational) if situational else None,
            "spl": json.dumps(splits) if splits else None,
            "src": source,
            "ca": now,
        })
        await session.commit()


# ── Save backtest prediction ─────────────────────────────────────────────────────


async def _save_backtest_prediction(
    game_id: int,
    row: pd.Series,
    ats_model: Optional[xgb.Booster] = None,
    ou_model: Optional[xgb.Booster] = None,
    ats_features: Optional[List[str]] = None,
    ou_features: Optional[List[str]] = None,
) -> None:
    """Compute and save a prediction for a single game during backtest."""
    # ATS
    ats_proba = 0.5
    if ats_model is not None and ats_features:
        vals = []
        for feat in ats_features:
            v = row.get(feat, 0.0)
            vals.append(float(v) if not (isinstance(v, float) and np.isnan(v)) else 0.0)
        try:
            dmat = xgb.DMatrix(np.array([vals], dtype=np.float32), feature_names=ats_features)
            ats_proba = float(ats_model.predict(dmat)[0])
        except Exception:
            ats_proba = 0.5

    # OU
    ou_total = None
    if ou_model is not None and ou_features:
        vals = []
        for feat in ou_features:
            v = row.get(feat, 0.0)
            vals.append(float(v) if not (isinstance(v, float) and np.isnan(v)) else 0.0)
        try:
            dmat = xgb.DMatrix(np.array([vals], dtype=np.float32), feature_names=ou_features)
            ou_total = float(ou_model.predict(dmat)[0])
        except Exception:
            ou_total = None

    spread = float(row.get("closing_spread", 0) or 0)
    closing_ou = float(row.get("closing_ou", 0) or 0)
    home_abbr = str(row.get("home_abbr", ""))
    away_abbr = str(row.get("away_abbr", ""))
    home_score = int(row.get("home_score", 0) or 0)
    away_score = int(row.get("away_score", 0) or 0)

    pred_margin = 2 * spread * (ats_proba - 0.5) if spread != 0 else 0
    pred_home_score = None
    pred_away_score = None
    if ou_total is not None:
        pred_home_score = max(0, round((ou_total + pred_margin) / 2.0))
        pred_away_score = max(0, round((ou_total - pred_margin) / 2.0))

    if ats_proba > 0.5:
        sp_pick = f"{home_abbr} {spread:+.1f}" if spread < 0 else f"{home_abbr} {-spread:+.1f}"
    elif ats_proba < 0.5:
        sp_pick = f"{away_abbr} {spread:+.1f}" if spread > 0 else f"{away_abbr} {-spread:+.1f}"
    else:
        sp_pick = None

    ou_pick = None
    if ou_total is not None and closing_ou > 0:
        ou_pick = "Over" if ou_total > closing_ou else "Under"

    actual_total = home_score + away_score
    actual_margin = home_score - away_score

    ats_profit = 0.0
    ats_result = None
    if spread != 0:
        covered = (home_score - away_score + spread) > 0
        ats_result = "Win" if covered else "Loss"
        if ats_result == "Win":
            ats_profit = 100.0 if covered else -110.0
        else:
            ats_profit = -110.0

    ou_profit = 0.0
    ou_result = None
    if closing_ou > 0:
        ou_result = "Win" if actual_total > closing_ou else "Loss"
        ou_profit = 100.0 if ou_result == "Win" else -110.0

    # ── Moneyline ──────────────────────────────────────────────────────────────
    home_ml = _float_safe(row.get("closing_home_ml"))
    away_ml = _float_safe(row.get("closing_away_ml"))
    ml_result = None
    ml_profit = 0.0

    if home_ml is not None and away_ml is not None and home_ml != 0 and away_ml != 0:
        # Implied probabilities from odds (no-vig estimate)
        home_implied = 1 / abs(home_ml) * 100 if home_ml < 0 else 100 / (home_ml + 100)
        away_implied = 1 / abs(away_ml) * 100 if away_ml < 0 else 100 / (away_ml + 100)
        total_implied = home_implied + away_implied
        home_prob = home_implied / total_implied if total_implied > 0 else 0.5

        # Pick the team with higher implied probability (the favorite)
        if home_prob > 0.5:
            ml_pick = home_abbr
            pick_odds = home_ml
            did_win = home_score > away_score
            ml_edge = home_prob - 0.5
        else:
            ml_pick = away_abbr
            pick_odds = away_ml
            did_win = away_score > home_score
            ml_edge = (1 - home_prob) - 0.5

        ml_conf = abs(ml_edge) * 2
        ml_result = "Win" if did_win else "Loss"
        # Profit: +odds means profit on 100 stake; -odds means stake to win 100
        if ml_result == "Win":
            ml_profit = float(pick_odds) if pick_odds > 0 else 100.0
        else:
            ml_profit = -100.0 if pick_odds > 0 else -float(abs(pick_odds))
    else:
        ml_pick = None
        ml_conf = None
        ml_edge = 0.0

    # ── Odds from row ──────────────────────────────────────────────────────────
    spread_home_odds = _float_safe(row.get("closing_spread_home_odds"))
    spread_away_odds = _float_safe(row.get("closing_spread_away_odds"))
    over_odds = _float_safe(row.get("closing_over_odds"))
    under_odds = _float_safe(row.get("closing_under_odds"))

    # ── Map odds to pick side ───────────────────────────────────────────────────
    ats_odds_value = None
    if sp_pick:
        pick_team = sp_pick.split(" ")[0]
        if pick_team == home_abbr:
            ats_odds_value = spread_home_odds
        else:
            ats_odds_value = spread_away_odds

    ou_odds_value = None
    if ou_pick == "Over":
        ou_odds_value = over_odds
    elif ou_pick == "Under":
        ou_odds_value = under_odds

    ml_odds_value = None
    if ml_pick == home_abbr:
        ml_odds_value = home_ml
    elif ml_pick == away_abbr:
        ml_odds_value = away_ml

    # ── EV from edge * odds ─────────────────────────────────────────────────────
    ats_ev = round(abs(ats_proba - 0.5) * 2, 4)
    ou_ev = abs(ou_total - closing_ou) / closing_ou if ou_total and closing_ou else None
    ml_ev = round(ml_edge * 2, 4) if ml_edge else None

    home_stats = _build_nfl_home_stats(row)
    away_stats = _build_nfl_away_stats(row)
    situational = _build_nfl_situational(row)
    splits = _build_nfl_splits(row)

    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {NFL_SCHEMA}.game_predictions
                    (game_id, predicted_home_score, predicted_away_score,
                     predicted_total, predicted_margin, margin_conf,
                     ou_conf, ou_pick, spread_pick, ml_pick,
                     actual_home_score, actual_away_score, actual_total, actual_margin,
                     ats_result, ou_result, ml_result,
                     ats_profit, ou_profit, ml_profit,
                     ats_odds, ou_odds, ml_odds,
                     ats_ev, ou_ev, ml_ev,
                     home_stats_json, away_stats_json, situational_json, splits_json,
                     source, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s)
                ON CONFLICT (game_id, source)
                DO UPDATE SET
                    predicted_home_score = EXCLUDED.predicted_home_score,
                    predicted_away_score = EXCLUDED.predicted_away_score,
                    predicted_total = EXCLUDED.predicted_total,
                    predicted_margin = EXCLUDED.predicted_margin,
                    margin_conf = EXCLUDED.margin_conf,
                    ou_conf = EXCLUDED.ou_conf,
                    ou_pick = EXCLUDED.ou_pick,
                    spread_pick = EXCLUDED.spread_pick,
                    ml_pick = EXCLUDED.ml_pick,
                    actual_home_score = EXCLUDED.actual_home_score,
                    actual_away_score = EXCLUDED.actual_away_score,
                    actual_total = EXCLUDED.actual_total,
                    actual_margin = EXCLUDED.actual_margin,
                    ats_result = EXCLUDED.ats_result,
                    ou_result = EXCLUDED.ou_result,
                    ml_result = EXCLUDED.ml_result,
                    ats_profit = EXCLUDED.ats_profit,
                    ou_profit = EXCLUDED.ou_profit,
                    ml_profit = EXCLUDED.ml_profit,
                    ats_odds = EXCLUDED.ats_odds,
                    ou_odds = EXCLUDED.ou_odds,
                    ml_odds = EXCLUDED.ml_odds,
                    ats_ev = EXCLUDED.ats_ev,
                    ou_ev = EXCLUDED.ou_ev,
                    ml_ev = EXCLUDED.ml_ev,
                    home_stats_json = EXCLUDED.home_stats_json,
                    away_stats_json = EXCLUDED.away_stats_json,
                    situational_json = EXCLUDED.situational_json,
                    splits_json = EXCLUDED.splits_json,
                    created_at = EXCLUDED.created_at
                """,
                (
                    game_id, pred_home_score, pred_away_score,
                    round(ou_total, 2) if ou_total else None,
                    round(pred_margin, 2),
                    round(abs(ats_proba - 0.5) * 2, 4),
                    round(abs(ou_total - closing_ou), 2) if ou_total else None,
                    ou_pick, sp_pick, ml_pick,
                    home_score, away_score, actual_total, actual_margin,
                    ats_result, ou_result, ml_result,
                    ats_profit, ou_profit, ml_profit,
                    round(ats_odds_value) if ats_odds_value else None,
                    round(ou_odds_value) if ou_odds_value else None,
                    round(ml_odds_value) if ml_odds_value else None,
                    round(ats_ev, 4),
                    round(ou_ev, 4) if ou_ev else None,
                    round(ml_ev, 4) if ml_ev else None,
                    json.dumps(home_stats) if home_stats else None,
                    json.dumps(away_stats) if away_stats else None,
                    json.dumps(situational) if situational else None,
                    json.dumps(splits) if splits else None,
                    "api", datetime.now(timezone.utc),
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ── Pick card builders ────────────────────────────────────────────────────────────


def _build_nfl_home_stats(row: pd.Series) -> Dict[str, Any]:
    """Build home team stats summary from a feature vector row."""
    return {
        "abbreviation": str(row.get("home_abbr", "")),
        "points_for": _float_safe(row.get("hpf")),
        "points_against": _float_safe(row.get("hpa")),
        "win_pct_r5": _float_safe(row.get("home_win_pct_r5")),
        "margin_r5": _float_safe(row.get("home_margin_r3")),
        "margin_r10": _float_safe(row.get("home_margin_r10")),
        "cover_pct_r5": _float_safe(row.get("home_cover_pct_r5")),
        "season_ats_pct": _float_safe(row.get("home_season_ats_pct")),
        "embarrassed": bool(row.get("home_embarrassed", 0)),
        "rest_days": _float_safe(row.get("home_rest_days", 7)),
    }


def _build_nfl_away_stats(row: pd.Series) -> Dict[str, Any]:
    """Build away team stats summary."""
    return {
        "abbreviation": str(row.get("away_abbr", "")),
        "points_for": _float_safe(row.get("apf")),
        "points_against": _float_safe(row.get("apa")),
        "win_pct_r5": _float_safe(row.get("away_win_pct_r5")),
        "margin_r5": _float_safe(row.get("away_margin_r3")),
        "margin_r10": _float_safe(row.get("away_margin_r10")),
        "cover_pct_r5": _float_safe(row.get("away_cover_pct_r5")),
        "season_ats_pct": _float_safe(row.get("away_season_ats_pct")),
        "embarrassed": bool(row.get("away_embarrassed", 0)),
        "rest_days": _float_safe(row.get("away_rest_days", 7)),
    }


def _build_nfl_situational(row: pd.Series) -> Dict[str, Any]:
    """Build situational data summary."""
    return {
        "travel_miles": _float_safe(row.get("travel_miles")),
        "is_dome": bool(row.get("is_dome", 0)),
        "rest_diff": _int_safe(row.get("rest_diff")),
        "tz_diff": _int_safe(row.get("tz_diff")),
        "is_short_week": bool(row.get("is_short", 0)),
        "temp": _float_safe(row.get("temp")),
        "wind": _float_safe(row.get("wind")),
        "venue": str(row.get("venue", "")),
        "surface": str(row.get("surface", "")),
        "roof_type": str(row.get("roof_type", "")),
    }


def _build_nfl_splits(row: pd.Series) -> Dict[str, Any]:
    """Build splits / betting trends summary."""
    return {
        "spread": _float_safe(row.get("closing_spread")),
        "opening_spread": _float_safe(row.get("opening_spread")),
        "spread_movement": _float_safe(row.get("spread_movement")),
        "closing_ou": _float_safe(row.get("closing_ou")),
        "opening_ou": _float_safe(row.get("opening_ou")),
        "ou_movement": _float_safe(row.get("ou_movement")),
        "sp_h_odds_mvmt": _float_safe(row.get("sp_h_odds_mvmt")),
        "sp_a_odds_mvmt": _float_safe(row.get("sp_a_odds_mvmt")),
        "home_implied": _float_safe(row.get("himp")),
        "away_implied": _float_safe(row.get("aimp")),
        "diff_implied": _float_safe(row.get("dimp")),
        "season_avg_pts": _float_safe(row.get("season_avg_pts")),
    }


# ── Safe casting helpers ─────────────────────────────────────────────────────────


def _int_safe(val: Any) -> Optional[int]:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _float_safe(val: Any) -> Optional[float]:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        return round(float(val), 2)
    except (ValueError, TypeError):
        return None


def _str_safe(val: Any) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return ""
    return str(val)


# ── CLI / smoke test ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"

    if mode == "backtest":
        handicapper = NFLHandicapper()
        results = asyncio.run(handicapper.backtest())
        print("\n=== NFL Backtest ===")
        if "ats_results" in results:
            print(f"ATS: {len(results['ats_results'])} years")
            for r in results["ats_results"]:
                if "error" in r:
                    print(f"  {r['year']}: ERROR — {r['error']}")
                else:
                    print(f"  {r['year']}: acc={r['accuracy']:.4f} auc={r['auc']:.4f} n={r['n_train']}+{r['n_test']}")
        if "ou_results" in results:
            print(f"OU: {len(results['ou_results'])} years")
            for r in results["ou_results"]:
                if "error" in r:
                    print(f"  {r['year']}: ERROR — {r['error']}")
                else:
                    print(f"  {r['year']}: MAE={r['mae']:.2f} RMSE={r['rmse']:.2f} n={r['n_train']}+{r['n_test']}")

    elif mode == "handicap":
        if len(sys.argv) < 3:
            print("Usage: python engine.py handicap <game_id>")
            sys.exit(1)
        game_id = int(sys.argv[2])
        handicapper = NFLHandicapper()
        result = asyncio.run(handicapper.handicap_game(game_id, "", "", save_to_db=False))
        print(json.dumps(result, indent=2, default=str))

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python engine.py [backtest|handicap]")
        sys.exit(1)
