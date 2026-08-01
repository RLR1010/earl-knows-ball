"""Shared SHAP attribution helper for per-game feature explanations.

Every sport engine (NFL / NBA / MLB) uses per-year ``xgboost.Booster``
pickles for ATS/OU predictions.  This module computes per-prediction
feature contributions via ``shap.TreeExplainer`` so pick cards can show
*which statistics most influenced the model's result* and in which
direction.

Design notes:
- Works on the raw ``xgb.Booster`` objects the engines already load —
  no retraining, no wrapper model.
- ``shap_values`` are in the model's raw output space (margin for
  regression).  ``base + sum(shap) == model.predict`` exactly.
- Bounded LRU cache for TreeExplainers keyed by ``id(model)`` — a batch
  of games reuses one explainer per model, and the cache stays small.
- Failure is non-fatal: callers get ``None`` and logging if anything
  goes wrong, so predictions never break because attribution failed.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Bounded LRU cache: id(Booster) -> TreeExplainer
_EXPLAINER_CACHE: "OrderedDict[int, Any]" = OrderedDict()
_EXPLAINER_CACHE_MAX = 32


def _get_explainer(model: Any) -> Any:
    """Return a cached ``shap.TreeExplainer`` for a Booster (bounded LRU)."""
    import shap

    key = id(model)
    if key in _EXPLAINER_CACHE:
        _EXPLAINER_CACHE.move_to_end(key)
        return _EXPLAINER_CACHE[key]
    explainer = shap.TreeExplainer(model)
    _EXPLAINER_CACHE[key] = explainer
    _EXPLAINER_CACHE.move_to_end(key)
    while len(_EXPLAINER_CACHE) > _EXPLAINER_CACHE_MAX:
        _EXPLAINER_CACHE.popitem(last=False)
    return explainer


def compute_attribution(
    model: Any,
    feats: Sequence[Sequence[float]],
    feature_names: Sequence[str],
    feature_meta: Optional[Dict[str, Dict[str, str]]] = None,
    top_n: int = 8,
) -> Optional[Dict[str, Any]]:
    """Compute SHAP attribution for a single prediction row.

    Args:
        model: trained ``xgb.Booster`` whose ``feature_names`` match
            ``feature_names`` (engines already align before predicting).
        feats: 1xN feature values exactly as passed to ``model.predict``.
        feature_names: N names in model order (used for the DMatrix).
        feature_meta: optional ``{name: {"display_name", "description"}}``
            from the sport's ``features`` table.
        top_n: number of highest-|contribution| features to include.

    Returns:
        JSON-serializable dict::

            {
              "expected_value": float,          # model baseline (bias)
              "predicted_value": float,         # what the model output
              "contributions": [                # sorted by |contribution| desc
                {
                  "name": "himp",
                  "display_name": "Home implied win prob",
                  "description": "...",
                  "value": 0.62,                # raw feature value
                  "contribution": 1.33,         # signed effect on output
                  "direction": "up"             # up | down
                }, ...
              ],
              "story": "plain-english summary string"
            }

        Returns ``None`` if anything fails (shap unavailable, bad shapes,
        model mismatch) — callers must treat None as "no explanation".
    """
    if model is None or feats is None or not len(feats) or not feature_names:
        return None
    if len(feature_names) != len(feats[0]):
        logger.warning("shap: feature_names %d != feats %d — skipping attribution",
                       len(feature_names), len(feats[0]))
        return None
    try:
        import numpy as np
        import xgboost as xgb

        arr = np.asarray(feats, dtype=np.float32).reshape(1, -1)
        # sklearn wrappers (XGBRegressor/XGBClassifier) take plain arrays;
        # raw xgb.Booster takes a DMatrix.
        is_booster = isinstance(model, xgb.Booster)
        if is_booster:
            dmat = xgb.DMatrix(arr, feature_names=list(feature_names))
            inp = dmat
        else:
            inp = arr
        explainer = _get_explainer(model)
        raw = explainer.shap_values(inp)
        sv = np.asarray(raw).reshape(-1)
        expected = float(np.asarray(explainer.expected_value).reshape(-1)[0])
        pred = float(model.predict(inp)[0])

        meta = feature_meta or {}
        contribs: List[Dict[str, Any]] = []
        for i, name in enumerate(feature_names):
            c = float(sv[i])
            contribs.append(
                {
                    "name": str(name),
                    "display_name": meta.get(str(name), {}).get(
                        "display_name", str(name)
                    ),
                    "description": meta.get(str(name), {}).get("description", ""),
                    "value": float(np.asarray(feats[0]).reshape(-1)[i]),
                    "contribution": round(c, 4),
                    "direction": "up" if c >= 0 else "down",
                }
            )
        contribs.sort(key=lambda x: abs(x["contribution"]), reverse=True)
        top = contribs[:top_n]

        # Plain-english story: top mover + next couple + baseline
        if top:
            top1 = top[0]
            story = (
                f"Biggest factor: {top1['display_name']} "
                f"({top1['contribution']:+.2f}). "
            )
            rest = ", ".join(
                f"{c['display_name']} ({c['contribution']:+.2f})" for c in top[1:4]
            )
            if rest:
                story += f"Also: {rest}. "
            story += f"Model baseline: {expected:.2f}."
        else:
            story = f"Model baseline: {expected:.2f}."

        return {
            "expected_value": round(expected, 4),
            "predicted_value": round(pred, 4),
            "contributions": top,
            "story": story,
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("shap attribution failed: %s", exc, exc_info=True)
        return None


def attribution_json(
    model: Any,
    feats: Sequence[Sequence[float]],
    feature_names: Sequence[str],
    feature_meta: Optional[Dict[str, Dict[str, str]]] = None,
    top_n: int = 8,
) -> Optional[str]:
    """Like :func:`compute_attribution` but returns a JSON string (for DB
    ``shap_json`` Text columns), or ``None`` on failure."""
    import json

    att = compute_attribution(model, feats, feature_names, feature_meta, top_n)
    if att is None:
        return None
    return json.dumps(att, default=str)
