"""
handicap_mlb.py — MLB handicapping API routes.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text as sa_text

from app.database import get_db
from app.handicapping.mlb.mlb_engine import MLBHandicapper

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mlb", tags=["mlb"])


@router.post("/odds")
async def handle_ingest_lines(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Ingest current MLB betting lines — query current games from DB."""
    r = await db.execute(
        sa_text("""
            SELECT DISTINCT blc.game_id
            FROM mlb.betting_lines_consolidated blc
            JOIN mlb.games g ON g.id = blc.game_id
            WHERE g.date >= NOW() - INTERVAL '48 hours'
              AND g.date <= NOW() + INTERVAL '48 hours'
              AND blc.has_verified_ou = true
            ORDER BY blc.game_id
        """)
    )
    game_ids = [row[0] for row in r.fetchall()]
    return {"updated_game_ids": game_ids}


@router.post("/game/{game_id}/predict")
async def predict_game(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Generate a pick card for one MLB game."""
    handicapper = MLBHandicapper(db)
    await handicapper.handicap_game(game_id)
    return {"status": "ok", "game_id": game_id}


@router.post("/lines-and-picks")
async def mlb_lines_and_picks(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Ingest current lines and generate predictions.

    Runs every 15 minutes via the task scheduler.
    """
    start = datetime.now()
    results: dict[str, Any] = {"lines_ingested": 0, "predictions_generated": 0, "errors": [], "timing": {}}

    # Find games with current odds
    try:
        r = await db.execute(
            sa_text("""
                SELECT DISTINCT blc.game_id
                FROM mlb.betting_lines_consolidated blc
                JOIN mlb.games g ON g.id = blc.game_id
                WHERE g.date >= NOW() - INTERVAL '48 hours'
                  AND g.date <= NOW() + INTERVAL '48 hours'
                  AND blc.has_verified_ou = true
                ORDER BY blc.game_id
            """)
        )
        game_ids = [row[0] for row in r.fetchall()]
        results["lines_ingested"] = len(game_ids)
    except Exception as e:
        logger.error(f"line query failed: {e}")
        results["errors"].append(str(e))
        game_ids = []

    # Predict for each game
    handicapper = MLBHandicapper(db)
    predicted = 0
    for gid in game_ids:
        try:
            await handicapper.handicap_game(gid)
            predicted += 1
        except Exception as e:
            logger.error(f"handicap_game({gid}) failed: {e}")
            results["errors"].append(f"game {gid}: {e}")
    results["predictions_generated"] = predicted
    results["timing"]["total"] = (datetime.now() - start).total_seconds()

    logger.info(f"Lines-and-picks complete: {results}")
    return results
