#!/usr/bin/env python3
"""
Daily scheduled task: generate NFL write-ups for upcoming games (look-ahead).

Runs via the Earl task scheduler as a `subprocess` task.

Unlike the MLB daily task (which only covers today's ET calendar day), this NFL
task **looks ahead 6 days** from now and generates write-ups for every games in
that window — but **only for games that have a betting line** present in
`nfl.betting_lines_consolidated`. Games without a consolidated line yet (e.g.
preseason / PBP-only games, or games the odds ingest hasn't posted lines for)
are skipped, so we never spend generation tokens on a game that can't be
handicapped.

For each scheduled game in the window that has a line and no write-up yet, the
generator is called DIRECTLY in this subprocess (mirrors the frontend
admin/content "Generate Day" behavior, which regenerates only games with no
write-up at all). No API round-trip: write-up generation runs entirely off the
granian worker loop.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD/backend <repo>/venv/bin/python app/scripts/run_nfl_writeups_daily.py

Exit code 0 on success, non-zero if any generation failed.
"""

import asyncio
import json
import logging
import os
from typing import Any
import sys
from datetime import datetime, timedelta

from sqlalchemy import text

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.app.database import async_session  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("nfl_writeups_daily")

LOOKAHEAD_DAYS = 6
REASONING = os.environ.get("EARL_WRITEUP_REASONING", "minimal")


async def list_upcoming_games() -> list[dict]:
    """Return scheduled games in the next 6 days that have a betting line.

    Only games with a row in `nfl.betting_lines_consolidated` are returned —
    a game without a line can't be handicapped, so we skip it entirely.
    """
    start = datetime.utcnow()
    end = start + timedelta(days=LOOKAHEAD_DAYS)
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT
                    g.id,
                    g.date,
                    g.week,
                    ht.abbreviation AS home_abbr,
                    at.abbreviation AS away_abbr,
                    w.status AS writeup_status
                FROM nfl.games g
                JOIN nfl.teams ht ON ht.id = g.home_team_id
                JOIN nfl.teams at ON at.id = g.away_team_id
                LEFT JOIN nfl.game_writeups w ON w.game_id = g.id
                WHERE g.status = 'SCHEDULED'
                  AND g.date >= :start AND g.date < :end
                  AND EXISTS (
                      SELECT 1
                      FROM nfl.betting_lines_consolidated bl
                      WHERE bl.game_id = g.id
                  )
                ORDER BY g.date ASC
                """
            ),
            {"start": start, "end": end},
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def generate_writeup(game_id: int) -> dict:
    # Generate DIRECTLY in this subprocess rather than POSTing back into the
    # granian API. Writeup generation is heavy (multiple DeepSeek calls +
    # research + QC, minutes per game) and previously ran on a granian worker
    # event loop, blocking the API for all other traffic. Same pattern as
    # lines-and-picks / stats-refresh.
    from app.writeups.nfl.generator import NFLWriteupGenerator

    async with async_session() as db:
        gen = NFLWriteupGenerator()
        # Pass a mutable usage_log accumulator so base_generator populates
        # parsed["total_tokens"] and nested research_brief _usage_log.
        # Without it (default None), generate() records 0 tokens / a blank
        # Usage Log on the admin content page. Mirrors the API route.
        usage_log: list[dict] = []
        writeup, qc_results = await gen.generate(
            db,
            game_id=game_id,
            is_historical=False,
            reasoning=REASONING,
            usage_log=usage_log,
        )
        # qc_results is already a JSON-safe list[dict]
        return {"writeup": writeup, "qc": qc_results}


async def run() -> int:
    start = datetime.utcnow()
    end = start + timedelta(days=LOOKAHEAD_DAYS)
    games = await list_upcoming_games()
    if not games:
        logger.info(
            "No scheduled NFL games in the next %d days with betting lines "
            "— nothing to do.", LOOKAHEAD_DAYS
        )
        return 0

    have_writeup = sum(1 for g in games if g.get("writeup_status"))
    logger.info(
        "Look-ahead window %s..%s → %d game(s) with lines; %d already have "
        "writeups.",
        start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"),
        len(games), have_writeup,
    )

    # Mirror frontend admin content "Generate Day": regenerate only games
    # that have NO write-up at all (no status). Existing drafts are kept.
    to_generate = [g for g in games if not g.get("writeup_status")]

    if not to_generate:
        logger.info(
            "All %d game(s) with lines already have writeups — nothing to do.",
            len(games),
        )
        return 0

    results = []
    failures = 0
    for g in to_generate:
        matchup = f"{g['away_abbr']} @ {g['home_abbr']}"
        gid = g["id"]
        try:
            result = await generate_writeup(gid)
            logger.info("✔️  Game %s (%s): %s", gid, matchup, result)
            results.append({"game_id": gid, "matchup": matchup, "result": result})
        except Exception as e:  # noqa: BLE001
            failures += 1
            logger.error("✖️  Game %s (%s) FAILED: %s", gid, matchup, e)
            results.append({"game_id": gid, "matchup": matchup, "error": str(e)})

    summary = {
        "window_start": start.isoformat(timespec="minutes"),
        "window_end": end.isoformat(timespec="minutes"),
        "lookahead_days": LOOKAHEAD_DAYS,
        "total_with_lines": len(games),
        "existing_writeups": have_writeup,
        "attempted": len(to_generate),
        "successes": len(to_generate) - failures,
        "failures": failures,
        "results": results,
    }
    print(json.dumps(summary, indent=2, default=_json_default))
    return 1 if failures else 0


def _json_default(o: Any) -> Any:
    """Tolerate non-serializable types (Decimal, etc.) in the summary blob.
    NFL writeup payloads can carry Decimal model values (e.g. prop/probability
    figures) that aren't JSON-safe; stringify them rather than letting the whole
    daily run exit non-zero after articles are already persisted."""
    from decimal import Decimal
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fatal error in NFL writeups daily task")
        return 2


if __name__ == "__main__":
    sys.exit(main())
