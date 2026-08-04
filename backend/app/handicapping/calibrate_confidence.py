"""
MLB MLB per-market confidence calibration with numpy/sklearn interpolation.

Builds an isq-fit or linear interpolation curve from raw confidence → empirical win rate,
using all available buckets (even sparse ones), then saves to a JSON file.

For the live API, `calibrate()` reads the curve and interpolates any raw confidence value.

Usage:
    python -m app.handicapping.calibrate_confidence  # standalone
"""

import json
import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np

from sqlalchemy import text as _t

logger = logging.getLogger("earl.calibrate")

MIN_BUCKET = 20  # minimum games per bucket to include
BIN_COUNT = 20  # 2.5% per bin from 0.50 to 1.00

# ── Map sport → (schema, conf_cols for rl/ou/ml, result_cols) ──
SPORT_CONFIG = {
    "nfl": {"schema": "nfl", "use_per_model": False},
    "nba": {"schema": "nba", "use_per_model": True},
    "mlb": {"schema": "mlb", "use_per_model": True, "ou_result_is_outcome": True},
}
CAL_DIR = Path(__file__).parent.resolve()


def _is_nan_value(v) -> bool:
    """Check if a value is NaN (float('nan') or np.nan), safely."""
    if v is None:
        return False
    try:
        return math.isnan(float(v))
    except (ValueError, TypeError):
        return False


def _cal_path(sport: str) -> Path:
    return CAL_DIR / f"{sport}_confidence_calibration.json"


def calibrate(raw_conf: float, pick_type: str, sport: str = "nfl") -> float:
    """
    Interpolate raw confidence → calibrated win rate using saved curve.
    Falls back to raw_conf if no calibration exists.
    """
    path = _cal_path(sport)
    if not path.exists():
        cache[sport] = None

    try:
        data = json.loads(path.read_text())
    except Exception:
        return raw_conf

    curve = data.get(pick_type, data.get("overall", {}))
    xs = np.array(curve.get("x", []))
    ys = np.array(curve.get("y", []))

    if len(xs) < 2 or len(ys) < 2:
        return raw_conf

    # Guard against NaN in curve arrays
    if _is_nan_value(raw_conf):
        return raw_conf

    # Clip raw confidence to the range of the curve
    x = np.clip(raw_conf, xs.min(), xs.max())

    # Linear interpolation
    idx = np.searchsorted(xs, x)
    if idx == 0:
        return float(ys[0])
    if idx >= len(xs):
        return float(ys[-1])

    x_l, x_r = xs[idx - 1], xs[idx]
    y_l, y_r = ys[idx - 1], ys[idx]

    # Guard against NaN in curve endpoints
    if _is_nan_value(x_r) or _is_nan_value(x_l):
        return raw_conf
    if x_r == x_l:
        return float(y_l)

    t = (x - x_l) / (x_r - x_l)
    result = y_l + t * (y_r - y_l)
    if _is_nan_value(result):
        return raw_conf
    return float(round(result, 3))


# Simple module-level cache
cache: dict[str, dict | None] = {"nfl": None, "nba": None, "mlb": None}


def _load_cache(sport: str):
    """Load calibration into cache if not present."""
    if cache.get(sport) is None:
        path = _cal_path(sport)
        if path.exists():
            try:
                cache[sport] = json.loads(path.read_text())
            except Exception:
                cache[sport] = None


def calibrate(raw_conf: float, pick_type: str, sport: str = "nfl", curve_data: dict = None) -> float:
    """
    Map raw confidence -> calibrated empirical win rate.

    If curve_data is provided (pre-built curve dict), use it directly.
    Otherwise load the global curve from cache/file.
    """
    if curve_data is not None:
        data = curve_data
    else:
        _load_cache(sport)
        data = cache.get(sport)
    if data is None:
        return raw_conf

    curve = data.get(pick_type, data.get("overall", {}))
    xs = np.array(curve.get("x", []))
    ys = np.array(curve.get("y", []))

    if len(xs) < 2 or len(ys) < 2:
        return raw_conf

    # Guard against NaN in curve arrays (can happen if build_calibration wrote NaN)
    if _is_nan_value(raw_conf):
        return raw_conf

    x = np.clip(raw_conf, xs.min(), xs.max())
    idx = np.searchsorted(xs, x)
    if idx == 0:
        return float(ys[0])
    if idx >= len(xs):
        return float(ys[-1])

    x_l, x_r = xs[idx - 1], xs[idx]
    y_l, y_r = ys[idx - 1], ys[idx]

    # Guard against NaN in curve endpoints causing NaN interpolation
    if _is_nan_value(x_r) or _is_nan_value(x_l):
        return raw_conf
    if x_r == x_l:
        return float(y_l)

    t = (x - x_l) / (x_r - x_l)
    result = y_l + t * (y_r - y_l)
    if _is_nan_value(result):
        return raw_conf  # fallback if interpolation gave NaN
    return float(round(result, 3))


async def build_calibration(db, sport: str = "nfl", max_exclusive_season: int = None, skip_file_save: bool = False):
    """
    Build calibration curve for a sport by bucketting raw confidence → empirical win rate.

    Queries all predictions, groups into 20 confidence buckets (2.5% each),
    computes win rate per bucket, and saves to JSON.

    If max_exclusive_season is provided, only uses seasons < that value.
    This enables honest per-season calibration during backtesting.

    If skip_file_save is True, the curve dict is returned without writing
    to the global JSON file (used for per-season curves in backtesting).
    """
    if sport not in SPORT_CONFIG:
        logger.warning("Unknown sport: %s", sport)
        return

    cfg = SPORT_CONFIG[sport]
    schema = cfg["schema"]
    use_per_model = cfg["use_per_model"]

    season_filter = ""
    params = {}
    if max_exclusive_season is not None:
        season_filter = "  AND s.year < :max_season"
        params["max_season"] = max_exclusive_season

    def _season_join():
        if max_exclusive_season is None:
            return ""
        return f"  JOIN {schema}.games g ON gp.game_id = g.id\n  JOIN {schema}.seasons s ON g.season_id = s.id"

    if use_per_model:
        # ── MLB, NBA: per-model confidence columns ──
        rl_col = "run_line_result" if sport == "mlb" else "ats_result"

        # Collect data for each market
        # Map market to confidence column (NBA uses margin_conf, not rl_conf)
        conf_cols = {
            "rl": "rl_conf" if sport != "nba" else "margin_conf",
            "ou": "ou_conf",
            "ml": "ml_conf",
        }
        # Map market to result filter condition. All sports now store the OU
        # OUTCOME (Win/Loss/Push) in ou_result, so use the same filter everywhere.
        result_filters = {
            "rl": f"LOWER(gp.{rl_col}) IN ('win','loss')",
            "ou": "LOWER(gp.ou_result) IN ('win','loss')",
            "ml": "LOWER(gp.ml_result) IN ('win','loss')",
        }

        curve_data = {}
        for market in ("rl", "ou", "ml"):
            col = conf_cols[market]
            res_filter = result_filters[market]
            res_col = rl_col if market == "rl" else ("ou_result" if market == "ou" else "ml_result")

            # Per-market win condition (sport-aware for result column names)
            if market == "rl":
                res_table = rl_col  # 'ats_result' for NBA, 'run_line_result' for MLB
                win_sql = f"LOWER(gp.{res_table}) IN ('win','Win')"
            elif market == "ml":
                win_sql = "gp.ml_result = 'Win'"
            else:
                # ou: all sports store the bet OUTCOME (Win/Loss/Push) in ou_result
                win_sql = "LOWER(gp.ou_result) = 'win'"

            raw_rows = await db.execute(_t(f"""
                SELECT FLOOR(gp.{col} * {BIN_COUNT}) / {BIN_COUNT} as bucket,
                       COUNT(*) as n,
                       ROUND(AVG(gp.{col})::numeric, 3) as avg_raw,
                       COUNT(*) FILTER (WHERE {win_sql}) as wins
                FROM {schema}.game_predictions gp
                {_season_join()}
                WHERE gp.source IN ('api', 'backtest')
                  AND gp.{col} IS NOT NULL
                  AND gp.{col}::text <> 'NaN'
                  AND {res_filter}
                {season_filter}
                GROUP BY bucket
                ORDER BY bucket
            """), params)
            buckets = []
            for r in raw_rows.fetchall():
                n = r.n
                buckets.append({
                    "bucket": float(r.bucket),
                    "n": n,
                    "avg_raw": float(r.avg_raw),
                    "win_pct": float(r.wins) / max(n, 1) if n >= MIN_BUCKET else None,
                })

            # Filter reliable buckets and build curve
            reliable = [b for b in buckets if b["win_pct"] is not None]
            if len(reliable) < 2:
                reliable = buckets  # use all if not enough reliable

            curve_data[market] = {
                "x": [b["avg_raw"] for b in reliable],
                "y": [b["win_pct"] for b in reliable],
            }

        meta = {
            "generated_at": str(np.datetime64("now")),
            "total_games": sum(b["n"] for b in buckets) if buckets else 0,
            "sport": sport,
        }

        data = {
            "meta": meta,
            "ats": curve_data["rl"],
            "ou": curve_data["ou"],
            "ml": curve_data["ml"],
        }
    else:
        # ── NFL: single margin_conf ──
        rows = await db.execute(_t(f"""
            SELECT
                FLOOR(gp.margin_conf * {BIN_COUNT}) / {BIN_COUNT} as bucket,
                COUNT(*) as n,
                ROUND(AVG(gp.margin_conf)::numeric, 3) as avg_raw,
                COUNT(*) FILTER (WHERE gp.ats_result IN ('Win','Loss')) as rl_games,
                COUNT(*) FILTER (WHERE gp.ats_result='Win') as rl_w,
                COUNT(*) FILTER (WHERE gp.ou_result IN ('Win','Loss')) as ou_games,
                COUNT(*) FILTER (WHERE gp.ou_result='Win') as ou_w,
                COUNT(*) FILTER (WHERE gp.ml_result IN ('Win','Loss')) as ml_games,
                COUNT(*) FILTER (WHERE gp.ml_result='Win') as ml_w
            FROM {schema}.game_predictions gp
            {_season_join()}
            WHERE gp.source IN ('api', 'backtest')
              AND gp.margin_conf IS NOT NULL
              AND gp.margin_conf::text <> 'NaN'
              {season_filter}
            GROUP BY bucket
            ORDER BY bucket
        """), params)

        raw_buckets = []
        for r in rows.fetchall():
            raw_buckets.append({
                "bucket": float(r.bucket),
                "n": r.n,
                "avg_raw": float(r.avg_raw),
                "rl_pct": float(r.rl_w) / max(r.rl_games, 1) if r.rl_games >= MIN_BUCKET else None,
                "ou_pct": float(r.ou_w) / max(r.ou_games, 1) if r.ou_games >= MIN_BUCKET else None,
                "ml_pct": float(r.ml_w) / max(r.ml_games, 1) if r.ml_games >= MIN_BUCKET else None,
            })

        def _curve(key: str) -> dict:
            reliable = [b for b in raw_buckets if b[key] is not None]
            if not reliable:
                return {"x": [], "y": []}
            return {"x": [b["avg_raw"] for b in reliable], "y": [b[key] for b in reliable]}

        data = {
            "meta": {
                "generated_at": str(np.datetime64("now")),
                "total_games": sum(b["n"] for b in raw_buckets),
                "sport": sport,
            },
            "ats": _curve("rl_pct"),
            "ou": _curve("ou_pct"),
            "ml": _curve("ml_pct"),
        }

    total = data["meta"]["total_games"]

    if not skip_file_save:
        cal_path = _cal_path(sport)
        cal_path.write_text(json.dumps(data, indent=2))
        logger.info(
            "Saved %s calibration (%d buckets %s, %d games)",
            sport,
            len(data.get("ats", {}).get("x", [])),
            "per-model" if use_per_model else "margin_conf",
            total,
        )
        # Reload cache
        _load_cache(sport)
    else:
        logger.info(
            "Built %s calibration (%d buckets, %d games) — file save skipped",
            sport,
            len(data.get("ats", {}).get("x", [])),
            total,
        )

    return data


# ── Standalone ──
if __name__ == "__main__":
    import asyncio
    from app.database import get_db

    async def _main():
        async for db in get_db():
            await build_calibration(db, sport="nfl")
            await build_calibration(db, sport="mlb")
            await build_calibration(db, sport="nba")
            break

        print("\nSample calibration lookups:")
        for conf in [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]:
            mlb_at = calibrate(conf, "ats", sport="mlb")
            mlb_ou = calibrate(conf, "ou", sport="mlb")
            mlb_ml = calibrate(conf, "ml", sport="mlb")
            nfl_at = calibrate(conf, "ats", sport="nfl")
            print(
                f"  raw={conf:.2f} → "
                f"mlb: ats={mlb_at:.3f} ou={mlb_ou:.3f} ml={mlb_ml:.3f}  "
                f"nfl: ats={nfl_at:.3f}"
            )

    asyncio.run(_main())
