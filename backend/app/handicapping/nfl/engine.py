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
from app.handicapping.nfl.nfl_xgb_model_ats import run_backtest as ats_backtest
from app.handicapping.nfl.nfl_xgb_model_ou import run_backtest as ou_backtest

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────────
MODELS_DIR = Path(__file__).parent / "models" / "xgboost"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

ATS_MODEL_PATH = MODELS_DIR / "nfl_ats_best.pkl"
OU_MODEL_PATH = MODELS_DIR / "nfl_ou_best.pkl"

CURRENT_YEAR = datetime.now().year
DB_DSN: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)
NFL_SCHEMA = "nfl"

# ── Async DB setup ───────────────────────────────────────────────────────────────
ASYNC_DSN: str = DB_DSN.replace("postgresql://", "postgresql+asyncpg://")
_async_engine = None
_async_sessionmaker = None


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
    """
    glob_pattern = f"nfl_{model_type}_*.pkl"
    paths: Dict[int, Path] = {}
    for p in sorted(MODELS_DIR.glob(glob_pattern)):
        try:
            year = int(p.stem.split("_")[-1])
            paths[year] = p
        except (ValueError, IndexError):
            continue
    return paths


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
    """Return feature columns from ``nfl.features`` for the given model type."""
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
                feats = get_model_features(cur, ats_only=True)
                _FEATURES_CACHE_ATS = feats
            else:
                feats = get_model_features(cur, ou_only=True)
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
        """Load the latest ATS and OU models."""
        if ATS_MODEL_PATH.exists():
            with open(ATS_MODEL_PATH, "rb") as f:
                self.ats_model = pickle.load(f)
            logger.info("Loaded ATS model from %s", ATS_MODEL_PATH)
        if OU_MODEL_PATH.exists():
            with open(OU_MODEL_PATH, "rb") as f:
                self.ou_model = pickle.load(f)
            logger.info("Loaded OU model from %s", OU_MODEL_PATH)

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
        result: Dict[str, Any] = {
            "game_id": game_id,
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "spread": spread,
            "closing_ou": closing_ou,
            "ats_prediction": round(ats_proba, 4),
            "ats_edge": round(abs(ats_proba - 0.5) * 2, 4),
            "ou_predicted_total": round(ou_pred_total, 2) if ou_pred_total else None,
            "ou_edge": round(ou_edge, 2) if ou_edge else None,
            "ou_pick": ou_pick,
            "spread_pick": spread_pick,
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
        train_from: int = 2021,
        ats_hyperparams: Optional[Dict[str, Any]] = None,
        ou_hyperparams: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        save_results: bool = True,
    ) -> Dict[str, Any]:
        """Run a full backtest across all years.

        1. Train/evaluate ATS model year-by-year (via ``ats_backtest``).
        2. Train/evaluate OU model year-by-year (via ``ou_backtest``).
        3. If ``save_results``, rebuild per-game pick cards for each season
           using the models trained *before* that season.
        4. Return aggregated metrics.
        """
        dl = get_data_loader()
        df = dl.load_data(limit=limit)

        if df.empty:
            return {"error": "no data"}

        df["total_points"] = df["home_score"] + df["away_score"]

        from app.handicapping.nfl.nfl_xgb_model_ats import _ensure_ats_features
        from app.handicapping.nfl.nfl_xgb_model_ou import _ensure_ou_features

        df = _ensure_ats_features(df)
        df = _ensure_ou_features(df)

        df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

        test_years = sorted(df["season_year"].unique())
        test_years = [y for y in test_years if y >= train_from]
        logger.info("Backtest years: %s", test_years)

        ats_results: List[Dict[str, Any]] = []
        ou_results: List[Dict[str, Any]] = []
        ats_models_per_year: Dict[int, Any] = {}
        ou_models_per_year: Dict[int, Any] = {}

        for test_year in test_years:
            if test_year == min(test_years):
                continue

            # ATS
            ats_res = ats_backtest(
                df, test_year,
                ats_only=True,
                hyperparams=ats_hyperparams,
                return_model=True,
            )
            if "model" in ats_res:
                ats_models_per_year[test_year] = ats_res.pop("model")
            ats_results.append(ats_res)

            # OU
            ou_res = ou_backtest(
                df, test_year,
                hyperparams=ou_hyperparams,
                return_model=True,
            )
            if "model" in ou_res:
                ou_models_per_year[test_year] = ou_res.pop("model")
            ou_results.append(ou_res)

        # Build per-game pick cards
        if save_results:
            for test_year in test_years:
                if test_year == min(test_years):
                    continue
                ats_model = ats_models_per_year.get(test_year)
                ou_model = ou_models_per_year.get(test_year)
                if ats_model is None and ou_model is None:
                    continue

                year_df = df[df["season_year"] == test_year].copy()
                for _, row in year_df.iterrows():
                    gid = int(row.get("game_id", 0))
                    if not gid:
                        continue
                    await _save_backtest_prediction(
                        game_id=gid,
                        row=row,
                        ats_model=ats_model,
                        ou_model=ou_model,
                        ats_features=_get_features("ats"),
                        ou_features=_get_features("ou"),
                    )

        return {
            "ats_results": ats_results,
            "ou_results": ou_results,
            "n_years": len([r for r in ats_results if "error" not in r]),
            "test_years": test_years[1:],
        }


# ── Save API prediction ─────────────────────────────────────────────────────────


async def _save_api_prediction(result: Dict[str, Any]) -> None:
    """Save a single game prediction to ``nfl.game_predictions`` (async)."""
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
                 ou_conf, ou_pick, spread_pick,
                 home_stats_json, away_stats_json, situational_json, splits_json,
                 source, created_at)
            VALUES
                (:gid, :phs, :pas, :pt, :pm, :mc,
                 :oc, :op, :spick,
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

    ats_result = None
    if spread != 0:
        covered = (home_score - away_score + spread) > 0
        ats_result = "Win" if covered else "Loss"

    ou_result = None
    if closing_ou > 0:
        ou_result = "Win" if actual_total > closing_ou else "Loss"

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
                     ou_conf, ou_pick, spread_pick,
                     actual_home_score, actual_away_score, actual_total, actual_margin,
                     ats_result, ou_result,
                     home_stats_json, away_stats_json, situational_json, splits_json,
                     source, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    actual_home_score = EXCLUDED.actual_home_score,
                    actual_away_score = EXCLUDED.actual_away_score,
                    actual_total = EXCLUDED.actual_total,
                    actual_margin = EXCLUDED.actual_margin,
                    ats_result = EXCLUDED.ats_result,
                    ou_result = EXCLUDED.ou_result,
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
                    ou_pick, sp_pick,
                    home_score, away_score, actual_total, actual_margin,
                    ats_result, ou_result,
                    json.dumps(home_stats) if home_stats else None,
                    json.dumps(away_stats) if away_stats else None,
                    json.dumps(situational) if situational else None,
                    json.dumps(splits) if splits else None,
                    "backtest", datetime.now(timezone.utc),
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
        results = asyncio.run(handicapper.backtest(train_from=2021, limit=200))
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
