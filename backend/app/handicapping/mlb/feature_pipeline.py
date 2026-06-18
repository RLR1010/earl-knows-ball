"""
feature_pipeline.py — Generic file-backed cache for MLB raw game data.

This module provides a simple file-backed cache that any model (ATS, OU, ML)
can optionally use to avoid re-querying the DB on every prediction.

The cache stores raw game data (pre-featurization) as JSON on disk.
Each model file runs its own build_features() on the cached data.

Usage:
    from .feature_pipeline import get_cache

    cache = get_cache()
    if not cache.is_warm:
        from sqlalchemy.ext.asyncio import create_async_engine
        eng = create_async_engine(DATABASE_URL)
        await cache.refresh(eng)   # loads from DB, saves to disk
        await eng.dispose()

    df = cache.get_raw_df()        # returns cached DataFrame
    feats = my_build_features(df)  # model-specific feature engineering

Lifecycle:
    - Morning cron:  refresh()  → loads fresh data from DB, saves to disk
    - API startup:   warm()     → loads from disk in <50ms
    - First predict: warm()     → loads from disk, or falls back to DB
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

CACHE_DIR = Path(os.environ.get("MODEL_CACHE_DIR", "/home/rich/.openclaw/workspace/earl-knows-football/data/models"))
CACHE_FILE = CACHE_DIR / "mlb_team_feature_cache.json"

_DEFAULT_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)


class TeamFeatureCache:
    """File-backed cache for MLB raw game data.

    Loads from disk in <50ms on API startup. Seeded by morning cron.
    Survives container restarts and rebuilds.
    """

    def __init__(self, cache_file: str | Path = CACHE_FILE):
        self.cache_file = Path(cache_file)
        self._cache_raw_df: Optional[pd.DataFrame] = None

    @property
    def is_warm(self) -> bool:
        return self._cache_raw_df is not None and len(self._cache_raw_df) > 0

    async def warm(self, engine: Optional[AsyncEngine] = None) -> None:
        """Warm from disk, or fall back to DB load."""
        if self._load_from_disk():
            logger.info(
                "TeamFeatureCache: warm from disk (%d games)",
                len(self._cache_raw_df),
            )
            return
        logger.info("TeamFeatureCache: no cache file, loading from DB...")
        await self.refresh(engine)

    async def refresh(
        self,
        engine: Optional[AsyncEngine] = None,
        loader=None,
    ) -> None:
        """Full rebuild via loader callable → save to disk.

        Args:
            engine: DB engine (only used when loader is provided).
            loader: Optional async callable(engine) -> pd.DataFrame.
                    If None, requires a fresh cache file already on disk
                    (from a previous seed). If no cache file and no loader,
                    callers should provide a loader that does load_data().
        """
        if loader is not None:
            self._cache_raw_df = await loader(engine)
        else:
            logger.warning(
                "TeamFeatureCache.refresh() called without loader. "
                "Use refresh(engine, loader=load_data_fn) to seed from DB. "
                "Trying disk cache file as fallback..."
            )
            if not self._load_from_disk():
                logger.error("TeamFeatureCache: no loader and no disk cache")
                return
        self._save_to_disk()
        logger.info("TeamFeatureCache: refreshed %d games", len(self._cache_raw_df))

    def get_raw_df(self) -> Optional[pd.DataFrame]:
        """Return the raw (pre-featurization) game DataFrame."""
        return self._cache_raw_df

    # ── Disk I/O ──

    def _save_to_disk(self) -> None:
        if self._cache_raw_df is None or self._cache_raw_df.empty:
            logger.warning("TeamFeatureCache: nothing to save")
            return

        subset = self._cache_raw_df.copy()
        for col in subset.select_dtypes(include=["datetime64", "datetime64[ns]"]).columns:
            subset[col] = subset[col].astype(str)

        data = {
            "cached_at": datetime.now().isoformat(),
            "game_count": len(subset),
            "columns": list(subset.columns),
            "games": subset.to_dict(orient="records"),
        }

        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.cache_file, "w") as f:
            json.dump(data, f, default=str)

        logger.info("TeamFeatureCache: saved %d games → %s", len(subset), self.cache_file)

    def _load_from_disk(self) -> bool:
        if not self.cache_file.exists():
            return False
        try:
            with open(self.cache_file) as f:
                data = json.load(f)
            df = pd.DataFrame(data["games"])
            if df.empty:
                return False
            self._cache_raw_df = df
            logger.info("TeamFeatureCache: loaded %d games from %s", len(df), self.cache_file)
            return True
        except (json.JSONDecodeError, KeyError, ValueError, FileNotFoundError) as e:
            logger.warning("TeamFeatureCache: disk cache error: %s", e)
            return False

    def invalidate(self) -> None:
        self._cache_raw_df = None
        if self.cache_file.exists():
            self.cache_file.unlink()
            logger.info("TeamFeatureCache: invalidated")


# ── Module-level convenience ────────────────────────────────────────────────

_cache_instance: Optional[TeamFeatureCache] = None


def get_cache() -> TeamFeatureCache:
    """Get the module-level TeamFeatureCache singleton."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = TeamFeatureCache()
    return _cache_instance


async def warm_cache(engine: Optional[AsyncEngine] = None) -> TeamFeatureCache:
    """Warm the cache and return it."""
    cache = get_cache()
    await cache.warm(engine)
    return cache


# ── Line helpers (shared utility, not model-specific) ───────────────────────


def ml_to_implied_prob(ml_value: Optional[float]) -> Optional[float]:
    """Convert American moneyline to implied probability."""
    if ml_value is None:
        return None
    try:
        ml = float(ml_value)
        return 100.0 / (ml + 100.0) if ml > 0 else abs(ml) / (abs(ml) + 100.0)
    except (ValueError, TypeError):
        return None
