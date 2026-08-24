#!/usr/bin/env python3
"""
NFL lines + picks refresh — standalone subprocess job.

Runs via the Earl task scheduler as a `subprocess` task (previously an
`api_call` hitting /ingest/nfl/lines-and-picks). Moved off the granian event
loop so it can never block a request-serving worker.

Logic is a faithful verbatim move of `ingest_nfl_lines_and_picks` from
`app/routers/ingest.py`:

    1. Fetches current odds from The Odds API
    2. Runs incremental consolidation
    3. Batch-loads model & features, predicts future games with both spread+OU,
       and saves predictions to nfl.game_predictions

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_nfl_lines_and_picks.py

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
logger = logging.getLogger("earl.nfl_lines_and_picks")


def _run_in_thread(fn, *args, **kwargs):
    """Existing ingest.py helper: run a sync fn off the event loop."""
    import asyncio as _aio
    loop = _aio.get_running_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def run(api_key: str, db: AsyncSession):
    """Verbatim body of ingest_nfl_lines_and_picks (minus FastAPI deps)."""
    from app.ingestion.nfl_betting_lines import snapshot_nfl_opening_lines
    from app.handicapping.nfl.engine import (batch_predict_upcoming_games, CURRENT_NFL_YEAR)

    if not api_key:
        from app.core.config import settings as _nfl_settings
        api_key = os.environ.get("ODDS_API_KEY", "") or _nfl_settings.odds_api_key

    results = {"lines": None, "consolidated": None, "predictions": None, "errors": []}

    if not api_key:
        return {"status": "error", "message": "No API key"}

    try:
        # ── Step 1: Fetch lines ──────────────────────────────────────
        lines_result = await snapshot_nfl_opening_lines(
            db=db,
            api_key=api_key,
            days=3,
        )
        results["lines"] = lines_result
        updated_game_ids = lines_result.get("updated_game_ids", [])

        # ── Step 2: Consolidate ──────────────────────────────────────
        if updated_game_ids:
            try:
                from app.ingestion.nfl_betting_lines_consolidate import run as consolidate_nfl
                # Pass game_ids as game_ids_filter (keyword) — NEVER positionally, or it
                # lands on rebuild_full and TRUNCATES + rebuilds from the live table,
                # wiping migrated historical lines (2016-2020).
                await _run_in_thread(consolidate_nfl, game_ids_filter=set(updated_game_ids))
                results["consolidated"] = {"status": "ok", "games": len(updated_game_ids)}
            except Exception as exc:
                logger.error(f"Consolidation failed: {exc}")

        # ── Step 3: Predict future games with both spread + OU set ──
        predict_rows = (
            await db.execute(
                text("""
                    SELECT DISTINCT blc.game_id
                    FROM nfl.betting_lines_consolidated blc
                    JOIN nfl.games g ON g.id = blc.game_id
                    WHERE g.date > NOW()
                      AND g.status = 'SCHEDULED'
                      AND blc.closing_spread IS NOT NULL
                      AND blc.closing_ou IS NOT NULL
                """)
            )
        ).fetchall()
        game_ids = [r[0] for r in predict_rows]
        logger.info(f"NFL: {len(game_ids)} games have consolidated lines")

        # Snapshot existing picks BEFORE they are overwritten so we can detect
        # whether OU / ML / ATS changed (batch_predict deletes+reinserts source='api').
        old_picks: dict[int, dict] = {}
        if game_ids:
            old_res = await db.execute(
                text("""
                    SELECT game_id, ou_pick, ml_pick, spread_pick
                    FROM nfl.game_predictions
                    WHERE source = 'api'
                      AND game_id = ANY(:gids)
                """),
                {"gids": game_ids},
            )
            for gid, ou, ml, sld in old_res.fetchall():
                old_picks[gid] = {"ou_pick": ou, "ml_pick": ml, "spread_pick": sld}

        if game_ids:
            # Use the engine's resolved live model year (max trained year), NOT the
            # calendar year — no NFL model exists for the upcoming season until it's
            # trained. Using calendar year silently left models unloaded.
            year = CURRENT_NFL_YEAR
            pick_results = await batch_predict_upcoming_games(
                game_ids=game_ids,
                year=year,
                db=db,
            )
            results["predictions"] = {"games": len(pick_results)}

            # ── Step 4: Regenerate premium writeups when a pick flipped side ──
            # Same behavior as mlb-lines-and-picks: picks refresh throughout the
            # day until game time, and the writeup is written with those picks as
            # a guide. If OU / ML / ATS changed on a game that already has a
            # premium writeup, regenerate it to stay in sync.
            regenerated: list[int] = []
            regen_failures: list[dict] = []
            if game_ids:
                try:
                    wu_rows = await db.execute(
                        text("""
                            SELECT game_id
                            FROM nfl.game_writeups
                            WHERE game_id = ANY(:gids)
                              AND premium_content IS NOT NULL
                              AND premium_content != ''
                        """),
                        {"gids": game_ids},
                    )
                    games_with_premium = {r[0] for r in wu_rows.fetchall()}

                    new_res = await db.execute(
                        text("""
                            SELECT game_id, ou_pick, ml_pick, spread_pick
                            FROM nfl.game_predictions
                            WHERE source = 'api'
                              AND game_id = ANY(:gids)
                        """),
                        {"gids": game_ids},
                    )
                    new_picks: dict[int, dict] = {}
                    for gid, ou, ml, sp in new_res.fetchall():
                        new_picks[gid] = {"ou_pick": ou, "ml_pick": ml, "spread_pick": sp}

                    from app.writeups.nfl.generator import NFLWriteupGenerator
                    gen = NFLWriteupGenerator()

                    # Only regenerate when a pick FLIPS SIDE — not when a margin/
                    # line just drifts. OU/ML are already side-only (Over/Under,
                    # home/away). ATS spread_pick is "<team> <+/-val>"; side = team
                    # token only, so spread movement (e.g. +1.5 → +2.5 on the same
                    # team) does NOT fire.
                    def _ats_side(val):
                        if not val:
                            return None
                        return str(val).split()[0].strip()

                    def _pick_flipped(old_v, new_v):
                        # normalize empties; flip = different non-empty side
                        a = (old_v or "").strip()
                        b = (new_v or "").strip()
                        if a == b:
                            return False
                        return bool(a) and bool(b)

                    for gid in game_ids:
                        if gid not in games_with_premium:
                            continue
                        old = old_picks.get(gid)
                        new = new_picks.get(gid)
                        if old is None or new is None:
                            continue
                        flipped = (
                            _pick_flipped(old.get("ou_pick"), new.get("ou_pick"))
                            or _pick_flipped(old.get("ml_pick"), new.get("ml_pick"))
                            or _pick_flipped(
                                _ats_side(old.get("spread_pick")),
                                _ats_side(new.get("spread_pick")),
                            )
                        )
                        if not flipped:
                            continue
                        try:
                            writeup, _qc = await gen.generate(
                                db, gid, is_historical=False,
                                as_of_date=None, reasoning="minimal",
                            )
                            if "error" in writeup:
                                raise RuntimeError(writeup["error"])
                            regenerated.append(gid)
                            logger.info(f"Pick flipped side for game {gid} — regenerated premium writeup")
                        except Exception as exc:
                            regen_failures.append({"game_id": gid, "error": str(exc)[:200]})
                            logger.warning(f"Writeup regen failed for game {gid}: {exc}")
                except Exception as exc:
                    logger.warning(f"Writeup regeneration pass failed: {exc}")
                    regen_failures.append({"game_id": None, "error": f"pass_failed: {exc}"})

                results["writeup_regen"] = {
                    "regenerated_count": len(regenerated),
                    "regenerated_game_ids": regenerated,
                    "failures": regen_failures,
                }
        else:
            results["predictions"] = {"games": 0, "skipped": "no games with lines"}

    except Exception:
        import traceback
        logger.error(f"NFL lines+picks refresh failed: {traceback.format_exc()}")

    return {"status": "ok", "results": results}


async def main() -> int:
    api_key = os.environ.get("ODDS_API_KEY", "")
    async with async_session() as db:
        result = await run(api_key=api_key, db=db)
    logger.info(f"NFL lines+picks result: {result}")
    if result.get("status") != "ok":
        return 1
    results = result.get("results", {})
    if results.get("errors"):
        logger.warning(f"NFL lines+picks had {len(results['errors'])} errors: {results['errors']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("NFL lines+picks fatal error")
        sys.exit(2)
