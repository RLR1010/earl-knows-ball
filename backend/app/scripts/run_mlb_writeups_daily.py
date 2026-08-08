#!/usr/bin/env python3
"""
Daily scheduled task: generate MLB write-ups for the current day's games.

Runs via the Earl task scheduler as a `subprocess` task (scheduled for 8:00 AM
Eastern, cron "0 8 * * *", tz America/New_York).

"Today's games" = the current **ET calendar day** (6:00a–~2:00a next day local
for night games), which is why the window is computed in America/New_York and
converted to the UTC-stored timestamps. For each such game that has no
write-up yet, the generator is called DIRECTLY in this subprocess (mirrors the
frontend admin/content "Generate Day" behavior, which regenerates only games
with no write-up at all). No API round-trip: write-up generation runs entirely
off the granian worker loop.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD/backend <repo>/venv/bin/python app/scripts/run_mlb_writeups_daily.py

Exit code 0 on success, non-zero if any generation failed.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.app.database import async_session  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("mlb_writeups_daily")

EASTERN = ZoneInfo("America/New_York")
REASONING = os.environ.get("EARL_WRITEUP_REASONING", "minimal")

def et_day_window() -> tuple[datetime, datetime]:
    """Return [start, end) aware datetimes (UTC) for the current ET day."""
    now_et = datetime.now(EASTERN)
    day_start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_et = day_start_et + timedelta(days=1)
    utc = ZoneInfo("UTC")
    return (
        day_start_et.astimezone(utc),
        day_end_et.astimezone(utc),
    )


async def list_todays_games() -> list[dict]:
    start, end = et_day_window()
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT
                    g.id,
                    g.date,
                    ht.abbreviation AS home_abbr,
                    at.abbreviation AS away_abbr,
                    w.status AS writeup_status
                FROM mlb.games g
                JOIN mlb.teams ht ON ht.id = g.home_team_id
                JOIN mlb.teams at ON at.id = g.away_team_id
                LEFT JOIN mlb.game_writeups w ON w.game_id = g.id
                WHERE g.date >= :start AND g.date < :end
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
    # event loop, blocking the API for all other traffic for the ~1-2h daily
    # generation window. Same pattern as lines-and-picks / stats-refresh.
    from app.writeups.mlb.generator import MLBWriteupGenerator

    async with async_session() as db:
        gen = MLBWriteupGenerator()
        writeup, qc_results = await gen.generate(
            db,
            game_id=game_id,
            is_historical=False,
            reasoning=REASONING,
        )
        # qc_results is already a JSON-safe list[dict]
        return {"writeup": writeup, "qc": qc_results}



async def run() -> int:
    start, end = et_day_window()
    games = await list_todays_games()
    if not games:
        logger.info("No MLB games in today's ET window — nothing to do.")
        return 0

    complete = sum(
        1 for g in games
        if not g.get("writeup_status")
    )
    logger.info(
        "ET window %s..%s → %d game(s); %d with existing writeups.",
        start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"),
        len(games), complete,
    )

    # Mirror frontend admin content "Generate Day": regenerate only games
    # that have NO write-up at all (no status). Existing drafts are kept.
    to_generate = [g for g in games if not g.get("writeup_status")]

    if not to_generate:
        logger.info("All %d game(s) already have writeups — nothing to do.", len(games))
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
        "total_games": len(games),
        "existing_writeups": complete,
        "attempted": len(to_generate),
        "successes": len(to_generate) - failures,
        "failures": failures,
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 1 if failures else 0


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fatal error in MLB writeups daily task")
        return 2


if __name__ == "__main__":
    sys.exit(main())
