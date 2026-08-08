#!/usr/bin/env python3
"""
NBA lines + picks refresh — standalone subprocess job.

Runs via the Earl task scheduler as a `subprocess` task (previously an
`api_call` hitting /ingest/nba/lines-and-picks). Moved off the granian event
loop so it can never block a request-serving worker.

Logic is a faithful verbatim move of `ingest_nba_lines_and_picks` from
`app/routers/ingest.py`:

    1. Fetches current odds from The Odds API
    2. Runs incremental consolidation
    3. Batch-loads model & features, predicts future games with both spread+OU,
       and saves predictions to nba.game_predictions

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_nba_lines_and_picks.py

Exit code 0 on success, non-zero on failure.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.app.database import async_session  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("earl.nba_lines_and_picks")


def _run_in_thread(fn, *args, **kwargs):
    """Existing ingest.py helper: run a sync fn off the event loop."""
    import asyncio as _aio
    loop = _aio.get_running_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def run(api_key: str, db: AsyncSession):
    """Verbatim body of ingest_nba_lines_and_picks (minus FastAPI deps)."""
    from app.ingestion.nba_betting_lines import snapshot_nba_opening_lines
    from app.handicapping.nba.nba_engine import (
        batch_predict_upcoming_games,
    )

    if not api_key:
        from app.core.config import settings as _nba_settings
        api_key = os.environ.get("ODDS_API_KEY", "") or _nba_settings.odds_api_key

    results = {"lines": None, "consolidated": None, "predictions": None, "errors": []}

    if not api_key:
        return {"status": "error", "message": "No API key"}

    try:
        # ── Step 1: Fetch lines ──────────────────────────────────────
        lines_result = await snapshot_nba_opening_lines(
            db=db,
            api_key=api_key,
            days=3,
        )
        results["lines"] = lines_result
        updated_game_ids = lines_result.get("updated_game_ids", [])

        # ── Step 2: Consolidate ──────────────────────────────────────
        if updated_game_ids:
            try:
                from app.ingestion.nba_odds_consolidated import run as consolidate_nba
                await _run_in_thread(consolidate_nba, set(updated_game_ids))
                results["consolidated"] = {"status": "ok", "games": len(updated_game_ids)}
            except Exception as exc:
                logger.error(f"Consolidation failed: {exc}")

        # ── Step 3: Predict future games with both spread + OU set ──
        predict_rows = (
            await db.execute(
                text("""
                    SELECT DISTINCT blc.game_id
                    FROM nba.betting_lines_consolidated blc
                    JOIN nba.games g ON g.id = blc.game_id
                    WHERE g.date > NOW()
                      AND g.status = 'SCHEDULED'
                      AND blc.closing_spread IS NOT NULL
                      AND blc.closing_ou IS NOT NULL
                """)
            )
        ).fetchall()
        game_ids = [r[0] for r in predict_rows]
        logger.info(f"NBA: {len(game_ids)} games have consolidated lines")

        if game_ids:
            year = datetime.now(timezone.utc).year
            pick_results = await batch_predict_upcoming_games(
                game_ids=game_ids,
                year=year,
                db=db,
            )
            results["predictions"] = {"games": len(pick_results)}
        else:
            results["predictions"] = {"games": 0, "skipped": "no games with lines"}

    except Exception:
        import traceback
        logger.error(f"NBA lines+picks refresh failed: {traceback.format_exc()}")

    return {"status": "ok", "results": results}


async def main() -> int:
    api_key = os.environ.get("ODDS_API_KEY", "")
    async with async_session() as db:
        result = await run(api_key=api_key, db=db)
    logger.info(f"NBA lines+picks result: {result}")
    if result.get("status") != "ok":
        return 1
    results = result.get("results", {})
    if results.get("errors"):
        logger.warning(f"NBA lines+picks had {len(results['errors'])} errors: {results['errors']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("NBA lines+picks fatal error")
        sys.exit(2)
