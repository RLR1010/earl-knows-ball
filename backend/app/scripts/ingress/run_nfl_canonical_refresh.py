"""Scheduler runner: refresh the nflverse CANONICAL tables + situational v2 tables.

Does two things, idempotently:
  1. Canonical nflverse ingest for the CURRENT season (newest-first, per the
     workspace cardinal rule) — pbp, stats_*, ngs_*, participation, snap_counts,
     roster_weekly, ftn_charting, advstats_*, injuries + the non-yearly refs
     (schedules, players, teams, draft, officials, combine, contracts).
  2. Rebuild the situational v2 tables from those canonical tables:
     nfl.team_splits, nfl.player_splits_v2, nfl.def_vs_position.

Intended to be run by the task scheduler as a `subprocess` task, and also ad-hoc.

Usage:
    PYTHONPATH=$PWD <venv>/bin/python app/scripts/ingress/run_nfl_canonical_refresh.py [SEASON_YEAR]
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

BACKEND_DIR = Path(__file__).resolve().parents[3]
PY = sys.executable


def current_season_year() -> int:
    now = datetime.datetime.now(ZoneInfo("America/New_York"))
    # NFL league year rolls over in March; Jan/Feb still belong to the prior season.
    return now.year if now.month >= 3 else now.year - 1


def _run(cmd: list[str]) -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{BACKEND_DIR}:{env.get('PYTHONPATH', '')}"
    print(f"+ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=str(BACKEND_DIR), env=env, check=True)


def main() -> None:
    year = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else current_season_year()
    print(f"[canonical-refresh] season year = {year}", flush=True)

    # 1) canonical nflverse tables (current season; non-yearly refs included)
    _run([PY, "app/scripts/backfill_nflverse_all.py", "--start", str(year), "--end", str(year)])

    # 2) situational v2 tables (full rebuild from canonical tables)
    _run([PY, "-m", "app.ingestion.nfl_team_splits"])
    _run([PY, "-m", "app.ingestion.nfl_player_splits"])
    _run([PY, "-m", "app.ingestion.nfl_def_vs_position"])

    # 3) freshness guard — fail LOUDLY if the canonical weekly tables are behind the
    # latest PLAYED week. Without this, a stale local download cache (or an upstream
    # hiccup) left the tables a week behind while the task still reported success.
    # (One event loop for both the check and the outcome report — the async engine
    # cannot be shared across two asyncio.run() calls.)
    started = datetime.datetime.now(datetime.timezone.utc)

    async def _verify_and_report() -> list[str]:
        problems = await _collect_problems(year)
        for p in problems:
            print(f"[canonical-refresh] STALE: {p}", flush=True)
        try:
            from app.scripts.ingress._ingest_common import report_task_outcome

            await report_task_outcome(
                "nfl-canonical-refresh",
                success=not problems,
                error="; ".join(problems),
                started_at=started,
            )
        except Exception as exc:  # never let the reporting helper break the job
            print(f"[canonical-refresh] outcome report skipped: {exc}", flush=True)
        return problems

    try:
        import asyncio

        asyncio.run(_verify_and_report())
    except Exception as exc:
        print(f"[canonical-refresh] freshness guard skipped: {exc}", flush=True)

    print("[canonical-refresh] done", flush=True)


async def _collect_problems(year: int) -> list[str]:
    """Return stale-table problems (empty list == healthy).

    Compares each canonical weekly table's max REG week for `year` against the
    latest week actually PLAYED (FINAL REG games in nfl.games).
    """
    try:
        from sqlalchemy import text

        from app.database import async_session
    except Exception as exc:  # pragma: no cover
        return [f"freshness check unavailable: {exc}"]

    out: list[str] = []
    async with async_session() as db:
        played = (
            await db.execute(
                text(
                    """
                    SELECT max(g.week) FROM nfl.games g
                    JOIN nfl.seasons s ON s.id = g.season_id
                    WHERE s.year = :y AND g.game_type::text = 'REG'
                      AND g.status::text = 'FINAL'
                    """
                ),
                {"y": year},
            )
        ).scalar()
        if not played:
            return out  # nothing played yet — nothing to be stale against
        # (table, extra filter) — column conventions differ per nflverse asset
        checks = [
            ("stats_team_week", "AND season_type = 'REG'"),
            ("stats_player_week", "AND season_type = 'REG'"),
            ("pbp", "AND season_type = 'REG'"),
            ("snap_counts", "AND game_type = 'REG'"),
            ("ftn_charting", ""),
            ("ngs_passing", "AND season_type = 'REG'"),
            ("pfr_advstats_pass", ""),
        ]
        for tbl, extra in checks:
            try:
                got = (
                    await db.execute(
                        text(f"SELECT max(week) FROM nfl.{tbl} WHERE season = :y {extra}"),
                        {"y": year},
                    )
                ).scalar()
            except Exception as exc:
                out.append(f"{tbl}: check failed ({str(exc)[:60]})")
                continue
            if got is None or int(got) < int(played):
                out.append(f"{tbl} at week {got} < played week {played} (season {year})")
    return out


def _freshness_report(year: int) -> list[str]:
    """Sync helper for ad-hoc/CLI use."""
    import asyncio

    try:
        return asyncio.run(_collect_problems(year))
    except Exception as exc:
        return [f"freshness query failed: {exc}"]


if __name__ == "__main__":
    main()
