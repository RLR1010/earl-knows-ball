"""Shared helpers for Earl ingest subprocess jobs (scripts/ingress/*).

These were extracted verbatim from `app/routers/ingest.py` when the fire-and-
forget `stats/refresh` endpoints were converted to standalone subprocess jobs.
They were used only by the three `_run_{sport}_stats_refresh` workers (and the
lines-and-picks workers), so they moved here to be shared without bloating the
per-sport scripts.

Contents:
    _run_in_thread            — run a sync callable off the running event loop.
    _report_task_outcome      — overwrite the scheduler's dispatch-time task_runs
                                row with the REAL outcome of a background job.
    _MLB_REFRESH_TRACKER_SQL  — DDL for the MLB full-refresh staleness tracker.
    mlb_full_refresh_due      — True if the full MLB refresh is stale / never ran.
    mlb_mark_full_refresh     — record a successful full MLB refresh.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import text as sa_text


async def run_in_thread(fn, *args, **kwargs):
    """Run a sync callable off the running event loop (thread executor)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def report_task_outcome(task_name: str, success: bool, error: str = "", started_at=None):
    """Overwrite the scheduler's dispatch-time `task_runs` row with the REAL
    outcome of a background refresh.

    The scheduler marks api_call tasks `success` the moment the endpoint returns
    (fire-and-forget, ~242ms). That fake status hides background failures. This
    helper updates the latest run for `task_name` with the true result once the
    detached work actually finishes.
    """
    from app.database import async_session

    finished_at = datetime.now(timezone.utc)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000) if started_at else 0

    async with async_session() as s:
        await s.execute(
            sa_text("""
                UPDATE task_runs
                SET status = :s, finished_at = :f, duration_ms = :d, error_message = :e
                WHERE id = (
                    SELECT id FROM task_runs
                    WHERE task_name = :t
                    ORDER BY started_at DESC, id DESC LIMIT 1
                )
            """),
            {
                "t": task_name,
                "s": "success" if success else "failed",
                "f": finished_at,
                "d": duration_ms,
                "e": (error or "")[:2000] if not success else None,
            },
        )
        await s.commit()


_MLB_REFRESH_TRACKER_SQL = """
CREATE TABLE IF NOT EXISTS mlb.mlb_stats_refresh_tracker (
    year   INTEGER PRIMARY KEY,
    last_full_refresh_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def mlb_full_refresh_due(db, year, stale_seconds):
    """Return True if the full batting/pitching/games refresh is stale or never ran."""
    # Ensure tracker table exists (idempotent).
    await db.execute(sa_text(_MLB_REFRESH_TRACKER_SQL))

    row = (await db.execute(
        sa_text("""
            SELECT last_full_refresh_at
            FROM mlb.mlb_stats_refresh_tracker
            WHERE year = :y
        """),
        {"y": year},
    )).first()

    if row is None:
        return True  # never refreshed this season

    last_at = row[0]
    if last_at is None:
        return True
    # Normalize tz-aware comparison.
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last_at
    return age.total_seconds() > stale_seconds


async def mlb_mark_full_refresh(db, year):
    """Record a successful full refresh."""
    await db.execute(
        sa_text("""
            INSERT INTO mlb.mlb_stats_refresh_tracker (year, last_full_refresh_at)
            VALUES (:y, now())
            ON CONFLICT (year) DO UPDATE SET last_full_refresh_at = now()
        """),
        {"y": year},
    )
