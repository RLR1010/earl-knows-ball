#!/usr/bin/env python3
"""
MLB lines + picks refresh — standalone subprocess job.

Runs via the Earl task scheduler as a `subprocess` task (previously an
`api_call` hitting /ingest/mlb/lines-and-picks). Moved off the granian event
loop so it can never block a request-serving worker.

Logic is a faithful verbatim move of `ingest_mlb_lines_and_picks` from
`app/routers/ingest.py`:

    1. Fetches current odds from The Odds API
    2. Runs incremental consolidation
    3. Batch-loads model & features ONCE, predicts ALL upcoming games,
       and saves predictions to mlb.game_predictions
    4. Regenerates a premium writeup for any game whose pick FLIPPED SIDE

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_mlb_lines_and_picks.py

Exit code 0 on success, non-zero on failure.
"""

import asyncio
import logging
import os
import sys

from sqlalchemy import text as sa_text
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
logger = logging.getLogger("earl.mlb_lines_and_picks")


def _run_in_thread(fn, *args, **kwargs):
    """Existing ingest.py helper: run a sync fn off the event loop."""
    import asyncio as _aio
    loop = _aio.get_running_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def run(api_key: str, db: AsyncSession):
    """Verbatim body of ingest_mlb_lines_and_picks (minus FastAPI deps)."""
    import logging as _logging
    logger = _logging.getLogger("earl.mlb_lines_and_picks")

    from app.ingestion.mlb_betting_lines import snapshot_mlb_opening_lines
    from app.handicapping.mlb.mlb_engine import (
        batch_predict_upcoming_games,
        CURRENT_YEAR,
    )

    if not api_key:
        from app.core.config import settings as _mlb_settings
        api_key = os.environ.get("ODDS_API_KEY", "") or _mlb_settings.odds_api_key

    results = {"lines": None, "consolidated": None, "predictions": None, "errors": []}

    if not api_key:
        return {"status": "error", "message": "No API key"}

    try:
        # ── Step 1: Fetch lines ──────────────────────────────────────
        lines_result = await snapshot_mlb_opening_lines(
            db=db,
            api_key=api_key,
            days_from_now=3,
        )
        results["lines"] = lines_result
        updated_game_ids = lines_result.get("updated_game_ids", [])

        # ── Step 2: Consolidate ──────────────────────────────────────
        if updated_game_ids:
            try:
                from app.ingestion.mlb_betting_lines_consolidate import run as consolidate_mlb
                await _run_in_thread(consolidate_mlb, set(updated_game_ids))
                results["consolidated"] = {"status": "ok", "games": len(updated_game_ids)}
            except Exception as exc:
                logger.error(f"Consolidation failed: {exc}")
                results["errors"].append(f"consolidation_failed: {exc}")
        else:
            results["consolidated"] = {"status": "ok", "note": "no_lines_to_consolidate"}

        # ── Step 3: Batch predictions ───────────────────────────────

        # 3a – Find all future-scheduled games to generate/refresh picks
        result = await db.execute(
            sa_text("""
                SELECT g.id
                FROM mlb.games g
                JOIN mlb.betting_lines_consolidated blc ON blc.game_id = g.id
                WHERE g.status = 'SCHEDULED'
                  AND g.date > NOW()
                  AND blc.closing_spread IS NOT NULL
                  AND blc.closing_ou IS NOT NULL
                ORDER BY g.date
            """)
        )
        game_ids_needing_picks = [row[0] for row in result.fetchall()]

        # Snapshot existing picks BEFORE they are overwritten so we can detect
        # whether OU / ML / ATS changed (batch_predict deletes+reinserts source='api').
        old_picks: dict[int, dict] = {}
        if game_ids_needing_picks:
            old_res = await db.execute(
                sa_text("""
                    SELECT game_id, ou_pick, ml_pick, run_line_pick
                    FROM mlb.game_predictions
                    WHERE source = 'api'
                      AND game_id = ANY(:gids)
                """),
                {"gids": game_ids_needing_picks},
            )
            for gid, ou, ml, rl in old_res.fetchall():
                old_picks[gid] = {"ou_pick": ou, "ml_pick": ml, "run_line_pick": rl}

        if not game_ids_needing_picks:
            results["predictions"] = {"picks_generated": 0, "note": "No future scheduled games with consolidated lines"}
        else:
            pick_results = await batch_predict_upcoming_games(
                db=db,
                game_ids=game_ids_needing_picks,
                _logger=logger,
                year=CURRENT_YEAR,
            )
            results["predictions"] = {
                "picks_generated": len([p for p in pick_results if "error" not in p]),
                "games_attempted": len(game_ids_needing_picks),
                "game_results": pick_results,
            }

            # ── Step 4: Regenerate premium writeups when a pick changed ──
            # Picks are refreshed throughout the day until game time. The morning
            # writeup uses picks as a guide, so if OU / ML / ATS changed on a game
            # that already has a premium writeup, regenerate it to stay in sync.
            regenerated: list[int] = []
            regen_failures: list[dict] = []
            if game_ids_needing_picks:
                try:
                    wu_rows = await db.execute(
                        sa_text("""
                            SELECT game_id
                            FROM mlb.game_writeups
                            WHERE game_id = ANY(:gids)
                              AND premium_content IS NOT NULL
                              AND premium_content != ''
                        """),
                        {"gids": game_ids_needing_picks},
                    )
                    games_with_premium = {r[0] for r in wu_rows.fetchall()}

                    new_res = await db.execute(
                        sa_text("""
                            SELECT game_id, ou_pick, ml_pick, run_line_pick
                            FROM mlb.game_predictions
                            WHERE source = 'api'
                              AND game_id = ANY(:gids)
                        """),
                        {"gids": game_ids_needing_picks},
                    )
                    new_picks: dict[int, dict] = {}
                    for gid, ou, ml, rl in new_res.fetchall():
                        new_picks[gid] = {"ou_pick": ou, "ml_pick": ml, "run_line_pick": rl}

                    from app.writeups.mlb.generator import MLBWriteupGenerator
                    gen = MLBWriteupGenerator()

                    # Only regenerate when a pick FLIPS SIDE — not when a margin/
                    # line just drifts. OU/ML are already side-only (Over/Under, home/away).
                    # ATS run_line_pick is "<team> <+/-val>"; side = team token only, so
                    # spread movement (e.g. +1.5 → +2.5 on the same team) does NOT fire.
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

                    for gid in game_ids_needing_picks:
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
                                _ats_side(old.get("run_line_pick")),
                                _ats_side(new.get("run_line_pick")),
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
            logger.info(
                f"Lines+picks: {lines_result.get('loaded', 0)} new lines, "
                f"{len(game_ids_needing_picks)} games, "
                f"{len([p for p in pick_results if 'error' not in p])} picks"
            )

    except Exception as e:
        import traceback
        results["errors"].append(str(e))
        logger.error(f"Lines+picks refresh failed: {e}\n{traceback.format_exc()}")

    return {"status": "ok", "results": results}


async def main() -> int:
    api_key = os.environ.get("ODDS_API_KEY", "")
    async with async_session() as db:
        result = await run(api_key=api_key, db=db)
    logger.info(f"MLB lines+picks result: {result['results'] if result.get('status')=='ok' else result}")
    if result.get("status") != "ok":
        return 1
    results = result.get("results", {})
    if results.get("errors"):
        logger.warning(f"MLB lines+picks had {len(results['errors'])} errors: {results['errors']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("MLB lines+picks fatal error")
        sys.exit(2)
