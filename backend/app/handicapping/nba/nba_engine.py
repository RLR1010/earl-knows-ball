"""
NBA handicapping engine — load models, predict games, build pick cards,
run backtests, and save results to the database.

Mirrors ``nfl/engine.py`` but adapted for NBA (point-spread betting,
basketball stats instead of football).
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
from sklearn.metrics import roc_auc_score, mean_absolute_error, mean_squared_error

from app.handicapping.nba.data_loader import NBADataLoader, get_data_loader, get_model_features

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────────
MODELS_DIR = Path.home() / ".openclaw" / "workspace" / "earl-knows-football" / "data" / "models" / "nba"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

_now = datetime.now()
CURRENT_SEASON = _now.year if _now.month >= 10 else _now.year - 1
DB_DSN: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)
NBA_SCHEMA = "nba"

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


# ── Sync DB helpers ──────────────────────────────────────────────────────────────


def _get_sync_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(DB_DSN)


# ── Per-year model loaders (from pickle files) ────────────────────────────────


def _resolve_year_pkl_paths(model_type: str) -> Dict[int, Path]:
    """Return {year: Path} for all per-year model pickle files.

    ``model_type`` is ``'ats'`` or ``'ou'``.

    Loads the pkl_filename list from the current live training run
    (``nba.training_runs WHERE is_live = 't'``), parses
    ``<uuid>-<year>.pkl`` entries, and resolves them under ``MODELS_DIR``.

    Returns an empty dict when no live run exists or no pkl files found.
    """
    from app.handicapping.db_training import get_live_training_run
    run = get_live_training_run("nba", model_type)
    if run is None:
        logger.warning("  No live training_run for nba/%s", model_type)
        return {}

    raw = run.get("pkl_filename", "")
    if not raw:
        logger.warning("  training_run for nba/%s has empty pkl_filename", model_type)
        return {}

    parts = [s.strip() for s in raw.split(",") if s.strip()]
    out: Dict[int, Path] = {}
    for fname in parts:
        stem = fname.rsplit(".", 1)[0]
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
        logger.info("  Year pkl files for nba/%s: %s", model_type, out)
    else:
        logger.warning("  No year pkl files found for nba/%s", model_type)
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
    """Return feature columns from ``nba.features`` for the given model type.

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


# ── Engine class ─────────────────────────────────────────────────────────────────


class NBAHandicapper:
    """NBA handicapping engine — XGBoost models for ATS and OU prediction.

    Loads year-specific pickled models, predicts games from the data loader,
    and saves results to ``nba.game_predictions``.
    """

    def __init__(
        self,
        schema: str = "nba",
        model_prefix: str | None = None,
    ):
        self.schema = schema
        self.model_prefix = model_prefix

    # ── Backtest ─────────────────────────────────────────────────────────────────

    async def backtest(
        self,
        years: list[int] | None = None,
        limit: int | None = None,
        save_results: bool = True,
    ) -> Dict[str, Any]:
        """Run a full backtest across all available NBA years.

        For each year with a pickled model, load pre-computed features via
        the data loader, evaluate ATS and OU models, and optionally save
        per-game predictions to ``nba.game_predictions``.

        Returns
        -------
        dict with ``ats_results``, ``ou_results``, ``test_years``.
        """
        ats_paths = _resolve_year_pkl_paths("ats")
        ou_paths = _resolve_year_pkl_paths("ou")

        if not ats_paths and not ou_paths:
            return {"error": "no live training run found with pkl files"}

        if years is None:
            # Default: years available in both ATS and OU paths
            all_years = set(ats_paths.keys()) | set(ou_paths.keys())
            years = sorted(all_years)

        logger.info("Backtest years (from pkl): %s", years)

        dl = get_data_loader()
        df = dl.load_data(limit=limit)
        if df.empty:
            return {"error": "no data"}

        df["total_points"] = df["home_score"] + df["away_score"]
        df = df.sort_values(["season_year", "game_id"]).reset_index(drop=True)

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

        # ── Save per-game predictions (async) ──
        if save_results:
            ats_model: Optional[xgb.Booster] = None
            ou_model: Optional[xgb.Booster] = None
            for test_year in years:
                if test_year in ats_paths:
                    ats_model = _load_model_for_year("ats", test_year)
                if test_year in ou_paths:
                    ou_model = _load_model_for_year("ou", test_year)

                year_df = df[df["season_year"] == test_year].copy()
                if year_df.empty:
                    continue

                ats_feats = _get_features("ats") if ats_model is not None else None
                ou_feats = _get_features("ou") if ou_model is not None else None

                for _, row in year_df.iterrows():
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

    # ── Single-game handicap ────────────────────────────────────────────────────

    async def handicap_game(
        self,
        game_id: int,
        home_team: str = "",
        away_team: str = "",
        year: int | None = None,
        save_to_db: bool = True,
    ) -> Dict[str, Any]:
        """Handicap a single NBA game.

        Loads the game from the data loader, runs ATS and OU models,
        and returns a pick card dict with predictions, confidence, and
        handicapping info.
        """
        dl = get_data_loader()
        df = dl.load_data(limit=None)

        if df.empty:
            return {"error": "No data loaded"}

        row = df[df["game_id"] == game_id]
        if row.empty:
            return {"error": f"Game {game_id} not found in NBA data"}

        row = row.iloc[0]

        if year is None:
            year = int(row.get("season_year", CURRENT_SEASON))

        ats_model = _load_model_for_year("ats", year)
        ou_model = _load_model_for_year("ou", year)

        pick_card = self._build_pick_card(
            row, ats_model, ou_model, year, game_id,
        )

        if save_to_db:
            await _save_api_prediction(pick_card)

        return pick_card

    def _build_pick_card(
        self,
        row: pd.Series,
        ats_model: xgb.Booster | None,
        ou_model: xgb.Booster | None,
        year: int,
        game_id: int | None = None,
    ) -> Dict[str, Any]:
        """Build the full pick card dict with predictions and handicapping info."""
        # Feature extraction
        ats_feats = _get_features("ats") if ats_model is not None else []
        ou_feats = _get_features("ou") if ou_model is not None else []

        ats_vec, _ = _extract_feature_vector(row, "ats") if ats_feats else (np.array([[]], dtype=np.float32), [])
        ou_vec, _ = _extract_feature_vector(row, "ou") if ou_feats else (np.array([[]], dtype=np.float32), [])

        home_prob = float(ats_model.predict(xgb.DMatrix(ats_vec))[0]) if ats_model is not None and ats_vec.size > 0 else 0.5
        predicted_total = float(ou_model.predict(xgb.DMatrix(ou_vec))[0]) if ou_model is not None and ou_vec.size > 0 else 0.0

        spread = float(row.get("spread", 0))
        ou_line = float(row.get("total", 0))

        # ATS pick
        if home_prob > 0.5:
            ats_pick = str(row.get("home_abbr", row.get("home_team", "HOME")))
            implied_cover = home_prob
        else:
            ats_pick = str(row.get("away_abbr", row.get("away_team", "AWAY")))
            implied_cover = 1 - home_prob

        confidence = self._confidence_from_prob(implied_cover)

        # OU pick
        ou_pick = "OVER" if predicted_total > ou_line else "UNDER"
        ou_conf = abs(predicted_total - ou_line) / max(ou_line, 1)
        ou_conf = min(max(ou_conf, 0.51), 0.95)
        ou_confidence = self._confidence_from_prob(ou_conf)

        # Moneyline
        home_ml_odds = int(row.get("home_ml", row.get("home_ml_odds", 0)))
        away_ml_odds = int(row.get("away_ml", row.get("away_ml_odds", 0)))
        if home_prob > 0.5:
            ml_pick = str(row.get("home_abbr", row.get("home_team", "HOME")))
            ml_conf = home_prob
        else:
            ml_pick = str(row.get("away_abbr", row.get("away_team", "AWAY")))
            ml_conf = 1 - home_prob

        opening_spread = float(row.get("opening_spread", spread))

        # Implied lines / EV
        fair_ats_odds = self._prob_to_moneyline(implied_cover)
        ml_prob = self._ml_prob_from_spread(home_prob, spread)
        ev_spread = row.get("spread_movement", 0)
        ev_total = row.get("ou_movement", 0)

        pick_card: Dict[str, Any] = {
            "game_id": int(row.get("game_id", game_id or 0)),
            "season": year,
            "home_team": str(row.get("home_abbr", row.get("home_team", ""))),
            "away_team": str(row.get("away_abbr", row.get("away_team", ""))),
            "ats_prediction": round(home_prob, 4),
            "ou_predicted_total": round(predicted_total, 1),
            "spread": spread,
            "total": ou_line,
            "predicted": {
                "home_score": None,
                "away_score": None,
                "total": round(predicted_total, 1),
                "margin": round(spread * (2 * home_prob - 1), 1),
                "spread": spread,
            },
            "picks": {
                "ats": {
                    "pick": ats_pick,
                    "confidence": confidence,
                    "probability": round(implied_cover, 3),
                    "spread": spread,
                },
                "over_under": {
                    "pick": ou_pick,
                    "confidence": ou_confidence,
                    "probability": round(ou_conf, 3),
                    "line": ou_line,
                    "predicted_total": round(predicted_total, 1),
                },
                "moneyline": {
                    "pick": ml_pick,
                    "confidence": self._confidence_from_prob(ml_conf),
                    "probability": round(ml_conf, 3),
                    "home_odds": home_ml_odds,
                    "away_odds": away_ml_odds,
                },
            },
            "line_movement": {
                "opening_spread": round(opening_spread, 1),
                "current_spread": round(spread, 1),
                "spread_movement": round(float(ev_spread), 1),
                "fair_ats_odds": fair_ats_odds,
                "ats_ev_points": round(float(ev_spread), 2),
            },
            "ats_edge": round(float(ev_spread), 4),
            "ou_edge": round(float(ev_total), 4),
            "ml_edge": 0.0,
            "handicap_info": {
                "team_stats": {
                    "home": _build_nba_home_stats(row),
                    "away": _build_nba_away_stats(row),
                },
                "betting_lines": {
                    "spread": {
                        "opening": round(opening_spread, 1),
                        "current": round(spread, 1),
                        "movement": round(float(ev_spread), 2),
                    },
                    "over_under": {
                        "opening": round(float(row.get("opening_ou", 0)), 1),
                        "current": round(ou_line, 1),
                        "movement": round(float(ev_total), 2),
                    },
                    "moneyline": {
                        "home": home_ml_odds,
                        "away": away_ml_odds,
                    },
                },
                "situational": _build_nba_situational(row),
                "splits": _build_nba_splits(row),
                "venue": {
                    "name": str(row.get("venue", "")),
                    "roof_type": "indoor",
                },
            },
        }

        return pick_card

    def _confidence_from_prob(self, prob: float) -> str:
        """Map a probability to a confidence label."""
        if prob >= 0.75:
            return "HIGH"
        if prob >= 0.62:
            return "MEDIUM"
        return "LOW"

    def _prob_to_moneyline(self, prob: float) -> int:
        """Convert win probability to American moneyline odds."""
        if prob <= 0 or prob >= 1:
            return 0
        if prob > 0.5:
            return int(-100 * prob / (1 - prob))
        else:
            return int(100 * (1 - prob) / prob)

    def _ml_prob_from_spread(self, cover_prob: float, spread: float) -> float:
        """Estimate ML win probability from ATS cover probability and spread size."""
        if cover_prob >= 0.5:
            return min(cover_prob + 0.08, 0.95)
        else:
            return max(cover_prob - 0.08, 0.05)


# ── Year evaluation ──────────────────────────────────────────────────────────────


def _evaluate_year_model(year_df: pd.DataFrame, model: xgb.Booster, model_type: str) -> Dict[str, Any]:
    """Evaluate a single per-year model on a full season's games.

    Returns dict with accuracy (ATS) or MAE/RMSE (OU), AUC, and game-level
    predictions (list of dicts).  Matches the NFL ``backtest_season`` pattern.
    """
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
    """Save a single game prediction to ``nba.game_predictions`` (async).

    Mirrors the NFL engine pattern: writes moneyline pick, odds, EV, and stats.
    """
    game_id = result.get("game_id")
    if not game_id:
        return

    now = datetime.now(timezone.utc)
    source = result.get("source", "api")

    predicted_total = result.get("ou_predicted_total")
    ats_proba = result.get("ats_prediction", 0.5)
    spread = result.get("spread", 0) or 0

    picks = result.get("picks", {})
    ats_pick = picks.get("ats", {}).get("pick")
    ats_prob = picks.get("ats", {}).get("probability")
    ou_pick = picks.get("over_under", {}).get("pick")
    ou_prob = picks.get("over_under", {}).get("probability")
    ml_pick = picks.get("moneyline", {}).get("pick")
    ml_prob = picks.get("moneyline", {}).get("probability")
    ml_home_odds = picks.get("moneyline", {}).get("home_odds")
    ml_away_odds = picks.get("moneyline", {}).get("away_odds")

    hi = result.get("handicap_info", {})
    home_stats = hi.get("team_stats", {}).get("home", {})
    away_stats = hi.get("team_stats", {}).get("away", {})
    situational = hi.get("situational", {})
    splits = hi.get("splits", {})
    betting_lines = hi.get("betting_lines", {})
    spread_info = betting_lines.get("spread", {})
    ou_info = betting_lines.get("over_under", {})

    predicted_margin = result.get("predicted", {}).get("margin")
    ou_line = result.get("total", 0)

    ats_odds_value = spread_info.get("current")
    ou_odds_value = ou_info.get("current")
    ml_odds_value = ml_home_odds

    data_loader_getter = get_data_loader()
    season = data_loader_getter.get_season(game_id) if hasattr(data_loader_getter, "get_season") else None

    session_maker = _get_async_session()
    async with session_maker() as session:
        session.add(type("Prediction", (), {
            "__table__": None,
            "__tablename__": "game_predictions",
            "game_id": game_id,
            "season": season,
            "predicted_home_score": None,
            "predicted_away_score": None,
            "predicted_total": predicted_total,
            "predicted_margin": predicted_margin,
            "margin_conf": ats_prob,
            "ou_conf": ou_prob,
            "ou_pick": ou_pick,
            "pace_adjustment_pts": 0.0,
            "ats_result": None,
            "ou_result": None,
            "ml_result": None,
            "spread_pick": ats_pick,
            "ml_pick": ml_pick,
            "ml_conf": ml_prob,
            "ats_odds": round(ats_odds_value) if ats_odds_value else None,
            "ou_odds": round(ou_odds_value) if ou_odds_value else None,
            "ml_odds": round(ml_odds_value) if ml_odds_value else None,
            "ats_ev": round(result.get("ats_edge", 0), 4),
            "ou_ev": round(result.get("ou_edge", 0), 4),
            "ml_ev": round(result.get("ml_edge", 0), 4),
            "home_stats_json": json.dumps(home_stats) if home_stats else None,
            "away_stats_json": json.dumps(away_stats) if away_stats else None,
            "situational_json": json.dumps(situational) if situational else None,
            "splits_json": json.dumps(splits) if splits else None,
            "source": source,
            "created_at": now,
        }))
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
    """Save a backtest prediction row to ``nba.game_predictions`` (async).

    Extracts feature vectors from the row, runs ATS and OU models,
    generates picks, and persists everything including actual results
    for later analysis.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # ── ATS prediction ──
    ats_proba = 0.5
    if ats_model is not None and ats_features:
        ats_vec, ats_names = _extract_feature_vector(row, "ats")
        if ats_vec.size > 0:
            dmat = xgb.DMatrix(ats_vec, feature_names=ats_names)
            ats_proba = float(ats_model.predict(dmat)[0])

    # ── OU prediction ──
    ou_pred = None
    if ou_model is not None and ou_features:
        ou_vec, ou_names = _extract_feature_vector(row, "ou")
        if ou_vec.size > 0:
            dmat = xgb.DMatrix(ou_vec, feature_names=ou_names)
            ou_pred = float(ou_model.predict(dmat)[0])

    spread = float(row.get("spread", 0))
    total = float(row.get("total", 0))

    if ats_proba >= 0.5:
        spread_pick = str(row.get("home_abbr", row.get("home_team", "HOME")))
        margin_conf = ats_proba
    else:
        spread_pick = str(row.get("away_abbr", row.get("away_team", "AWAY")))
        margin_conf = 1 - ats_proba

    ou_pick = "OVER" if ou_pred is not None and ou_pred > total else ("UNDER" if ou_pred is not None else None)
    ou_conf = abs(ou_pred - total) / max(total, 1) if ou_pred is not None else 0.5

    # Moneyline
    ml_pick = str(row.get("home_abbr", row.get("home_team", "HOME"))) if ats_proba >= 0.5 else str(row.get("away_abbr", row.get("away_team", "AWAY")))
    ml_conf = max(ats_proba, 1 - ats_proba)
    home_ml = row.get("home_ml", 0) or 0
    away_ml = row.get("away_ml", 0) or 0
    ml_prob = ats_proba + 0.08 if ats_proba >= 0.5 else (1 - ats_proba) - 0.08
    ml_prob = min(max(ml_prob, 0.05), 0.95)

    # Actual results
    home_score = row.get("home_score", 0) or 0
    away_score = row.get("away_score", 0) or 0
    actual_total = int(home_score) + int(away_score)
    actual_margin = int(home_score) - int(away_score)

    if spread == 0:
        ats_result = "PUSH" if actual_margin == 0 else ("WIN" if actual_margin > 0 else "LOSS")
    else:
        margin_vs_spread = actual_margin + spread
        ats_result = "PUSH" if margin_vs_spread == 0 else ("WIN" if margin_vs_spread > 0 else "LOSS")

    if actual_total == total:
        ou_result = "PUSH"
    elif actual_total > total:
        ou_result = "OVER"
    else:
        ou_result = "UNDER"

    if actual_margin > 0:
        ml_result = "HOME"
    elif actual_margin < 0:
        ml_result = "AWAY"
    else:
        ml_result = "PUSH"

    # Profit (simplified: assume -110 odds for cover)
    ats_profit = 0.0
    if ats_result == "WIN":
        ats_profit = 0.91  # -110 => profit of 0.91 units
    elif ats_result == "LOSS":
        ats_profit = -1.0

    ou_profit = 0.0
    if ou_result in ("OVER", "UNDER"):
        ou_profit = 0.91 if ou_result == ou_pick else -1.0
    elif ou_result == "PUSH":
        ou_profit = 0.0

    ml_odds_value = home_ml if ml_pick == str(row.get("home_abbr", row.get("home_team", ""))) else away_ml
    ml_profit = 0.0
    if ml_result == "HOME":
        ml_profit = 100.0 / ml_odds_value if ml_odds_value < 0 else ml_odds_value / 100.0
    elif ml_result == "AWAY":
        ml_profit = 100.0 / ml_odds_value if ml_odds_value < 0 else ml_odds_value / 100.0

    ats_ev = 2 * ats_proba - 1  # very rough
    ou_ev = (ou_pred - total) / max(total, 1) if ou_pred else 0.0
    ml_ev = ml_conf - (1.0 / (ml_odds_value / 100.0 + 1)) if ml_odds_value else 0.0

    predicted_margin = spread * (2 * ats_proba - 1)

    home_stats = _build_nba_home_stats(row)
    away_stats = _build_nba_away_stats(row)
    situational = _build_nba_situational(row)
    splits = _build_nba_splits(row)

    session_maker = _get_async_session()
    async with session_maker() as session:
        # Check if prediction already exists
        sql_check = text(f"SELECT id FROM {NBA_SCHEMA}.game_predictions WHERE game_id = :gid")
        result = await session.execute(sql_check, {"gid": game_id})
        existing = result.scalar()

        if existing:
            sql = text(f"""
                UPDATE {NBA_SCHEMA}.game_predictions
                SET predicted_total = :pt, predicted_margin = :pm,
                    margin_conf = :mc, ou_conf = :oc, ou_pick = :op,
                    spread_pick = :sp, ml_pick = :mp, ml_conf = :mlc,
                    actual_home_score = :ahs, actual_away_score = :aas,
                    actual_total = :at, actual_margin = :am,
                    ats_result = :ar, ou_result = :our, ml_result = :mr,
                    ats_odds = :ao, ou_odds = :oo, ml_odds = :mo,
                    ats_profit = :ap, ou_profit = :oup, ml_profit = :mlp,
                    home_stats_json = :hs, away_stats_json = :aws,
                    situational_json = :sit, splits_json = :spl,
                    pace_adjustment_pts = :pap,
                    ats_ev = :aev, ou_ev = :oev, ml_ev = :mev,
                    updated_at = :ua
                WHERE game_id = :gid
            """)
            params = {
                "pt": ou_pred, "pm": predicted_margin,
                "mc": margin_conf, "oc": ou_conf, "op": ou_pick,
                "sp": spread_pick, "mp": ml_pick, "mlc": ml_conf,
                "ahs": int(home_score), "aas": int(away_score),
                "at": actual_total, "am": actual_margin,
                "ar": ats_result, "our": ou_result, "mr": ml_result,
                "ao": spread, "oo": total, "mo": 0,
                "ap": ats_profit, "oup": ou_profit, "mlp": ml_profit,
                "hs": json.dumps(home_stats), "aws": json.dumps(away_stats),
                "sit": json.dumps(situational), "spl": json.dumps(splits),
                "pap": 0.0, "aev": ats_ev, "oev": ou_ev, "mev": ml_ev,
                "ua": now, "gid": game_id,
            }
        else:
            sql = text(f"""
                INSERT INTO {NBA_SCHEMA}.game_predictions
                    (game_id, predicted_total, predicted_margin, margin_conf,
                     ou_conf, ou_pick, spread_pick, ml_pick, ml_conf,
                     actual_home_score, actual_away_score, actual_total, actual_margin,
                     ats_result, ou_result, ml_result,
                     ats_odds, ou_odds, ml_odds,
                     ats_profit, ou_profit, ml_profit,
                     home_stats_json, away_stats_json, situational_json, splits_json,
                     pace_adjustment_pts, ats_ev, ou_ev, ml_ev,
                     source, created_at)
                VALUES (:pt, :pm, :mc, :oc, :op, :sp, :mp, :mlc,
                        :ahs, :aas, :at, :am,
                        :ar, :our, :mr,
                        :ao, :oo, :mo,
                        :ap, :oup, :mlp,
                        :hs, :aws, :sit, :spl,
                        :pap, :aev, :oev, :mev,
                        :src, :ca)
            """)
            params = {
                "pt": ou_pred, "pm": predicted_margin,
                "mc": margin_conf, "oc": ou_conf, "op": ou_pick,
                "sp": spread_pick, "mp": ml_pick, "mlc": ml_conf,
                "ahs": int(home_score), "aas": int(away_score),
                "at": actual_total, "am": actual_margin,
                "ar": ats_result, "our": ou_result, "mr": ml_result,
                "ao": spread, "oo": total, "mo": 0,
                "ap": ats_profit, "oup": ou_profit, "mlp": ml_profit,
                "hs": json.dumps(home_stats), "aws": json.dumps(away_stats),
                "sit": json.dumps(situational), "spl": json.dumps(splits),
                "pap": 0.0, "aev": ats_ev, "oev": ou_ev, "mev": ml_ev,
                "src": "engine_backtest", "ca": now,
            }

        await session.execute(sql, params)
        await session.commit()

    logger.info("Saved NBA backtest prediction for game %s", game_id)


# ── Handicap info builders ─────────────────────────────────────────────────────


def _build_nba_home_stats(row: pd.Series) -> Dict[str, Any]:
    """Build home team stats summary from a feature vector row (NBA)."""
    return {
        "abbreviation": str(row.get("home_abbr", row.get("home_team", ""))),
        "points_for": _float_safe(row.get("hpf")),
        "points_against": _float_safe(row.get("hpa")),
        "win_pct_r5": _float_safe(row.get("home_win_pct_r5")),
        "margin_r5": _float_safe(row.get("home_margin_r3")),
        "margin_r10": _float_safe(row.get("home_margin_r10")),
        "cover_pct_r5": _float_safe(row.get("home_cover_pct_r5")),
        "season_ats_pct": _float_safe(row.get("home_season_ats_pct")),
        "pace_rating": _float_safe(row.get("home_pace_rating")),
        "offensive_rating": _float_safe(row.get("home_offensive_rating")),
        "defensive_rating": _float_safe(row.get("home_defensive_rating")),
        "rest_days": _float_safe(row.get("home_rest_days", 1)),
    }


def _build_nba_away_stats(row: pd.Series) -> Dict[str, Any]:
    """Build away team stats summary (NBA)."""
    return {
        "abbreviation": str(row.get("away_abbr", row.get("away_team", ""))),
        "points_for": _float_safe(row.get("apf")),
        "points_against": _float_safe(row.get("apa")),
        "win_pct_r5": _float_safe(row.get("away_win_pct_r5")),
        "margin_r5": _float_safe(row.get("away_margin_r3")),
        "margin_r10": _float_safe(row.get("away_margin_r10")),
        "cover_pct_r5": _float_safe(row.get("away_cover_pct_r5")),
        "season_ats_pct": _float_safe(row.get("away_season_ats_pct")),
        "pace_rating": _float_safe(row.get("away_pace_rating")),
        "offensive_rating": _float_safe(row.get("away_offensive_rating")),
        "defensive_rating": _float_safe(row.get("away_defensive_rating")),
        "rest_days": _float_safe(row.get("away_rest_days", 1)),
    }


def _build_nba_situational(row: pd.Series) -> Dict[str, Any]:
    """Build situational data summary (NBA)."""
    return {
        "travel_miles": _float_safe(row.get("travel_miles")),
        "rest_diff": _int_safe(row.get("rest_diff")),
        "tz_diff": _int_safe(row.get("tz_diff")),
        "is_back_to_back": bool(row.get("is_b2b", 0)),
        "is_division": bool(row.get("is_division", 0)),
        "is_conference": bool(row.get("is_conference", 0)),
        "venue": str(row.get("venue", "")),
        "altitude_ft": _int_safe(row.get("altitude_ft")),
    }


def _build_nba_splits(row: pd.Series) -> Dict[str, Any]:
    """Build splits / betting trends summary (NBA)."""
    return {
        "spread": _float_safe(row.get("closing_spread", row.get("spread"))),
        "opening_spread": _float_safe(row.get("opening_spread")),
        "spread_movement": _float_safe(row.get("spread_movement")),
        "closing_ou": _float_safe(row.get("closing_ou", row.get("total"))),
        "opening_ou": _float_safe(row.get("opening_ou")),
        "ou_movement": _float_safe(row.get("ou_movement")),
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
        handicapper = NBAHandicapper()
        results = asyncio.run(handicapper.backtest())
        print("\n=== NBA Backtest ===")
        for key in ("ats_results", "ou_results"):
            items = results.get(key, [])
            print(f"{key.upper()}: {len(items)} years")
            for r in items:
                if "error" in r:
                    print(f"  {r['year']}: ERROR — {r['error']}")
                else:
                    if key == "ats_results":
                        print(f"  {r['year']}: acc={r['accuracy']:.4f} auc={r['auc']} n={r['n_test']}")
                    else:
                        print(f"  {r['year']}: MAE={r['mae']:.2f} RMSE={r['rmse']:.2f} n={r['n_test']}")

    elif mode == "handicap":
        if len(sys.argv) < 3:
            print("Usage: python nba_engine.py handicap <game_id>")
            sys.exit(1)
        game_id = int(sys.argv[2])
        handicapper = NBAHandicapper()
        result = asyncio.run(handicapper.handicap_game(game_id, "", "", save_to_db=False))
        print(json.dumps(result, indent=2, default=str))

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python nba_engine.py [backtest|handicap]")
        sys.exit(1)
