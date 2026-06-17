"""Task Scheduler — APScheduler-based periodic job runner.

Runs in a background thread alongside the FastAPI process.
Executes tasks defined in task_config, logs results to task_runs,
and exposes next-run info via an in-process endpoint.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pytz
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import create_engine

from app.database import async_session

# Sync engine for APScheduler job store (asyncpg can't be driven synchronously)
SYNC_DB_URL = "postgresql+psycopg2://earl:earl_dev_pass@127.0.0.1:5432/earl_knows_football"
# Also used: see settings.database_url for the asyncpg URL
sync_engine = create_engine(SYNC_DB_URL, pool_pre_ping=True, pool_size=5)

logger = logging.getLogger("task_scheduler")

API_BASE = "http://localhost:8001"
TZ = pytz.timezone("America/Chicago")

# In-memory next-run tracking, updated periodically
_next_runs: dict[str, str] = {}
_scheduler: AsyncIOScheduler | None = None


# ── Task Executor ───────────────────────────────────────────────────

async def _execute_api_call(config: dict) -> dict:
    """Call the API via HTTP (same host, port 8001)."""
    url = config["url"]
    method = config.get("method", "POST").upper()
    body = config.get("body")

    async with httpx.AsyncClient(timeout=300) as client:
        if method == "POST":
            resp = await client.post(url, json=body)
        elif method == "GET":
            resp = await client.get(url)
        else:
            resp = await client.request(method, url, json=body)
        resp.raise_for_status()
        return resp.json()


async def _execute_subprocess(config: dict) -> dict:
    """Run a shell command and capture output."""
    import asyncio.subprocess
    cmd = config["command"]
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=config.get("timeout", 600)
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Subprocess failed (rc={proc.returncode}): {stderr.decode()[:2000]}"
        )
    return {"stdout": stdout.decode()[:5000], "returncode": proc.returncode}


EXECUTORS = {
    "api_call": _execute_api_call,
    "subprocess": _execute_subprocess,
}


# ── Logging ─────────────────────────────────────────────────────────

async def _log_task_start(task_name: str, db: AsyncSession) -> int:
    result = await db.execute(
        text("INSERT INTO task_runs (task_name, status, started_at) VALUES (:name, 'running', :now) RETURNING id"),
        {"name": task_name, "now": datetime.now(timezone.utc)}
    )
    await db.commit()
    return result.scalar()


async def _log_task_end(run_id: int, status: str, db: AsyncSession,
                        duration_ms: int = 0, error: str = "",
                        details: dict | None = None):
    await db.execute(
        text("""UPDATE task_runs SET status=:s, finished_at=:f, duration_ms=:d,
                 error_message=:e, details=:det WHERE id=:id"""),
        {
            "s": status, "f": datetime.now(timezone.utc),
            "d": duration_ms, "e": error[:2000] if error else None,
            "det": json.dumps(details) if details else None, "id": run_id,
        }
    )
    await db.commit()


# ── Wrapped Job ─────────────────────────────────────────────────────

async def wrapped_job(task_name: str):
    """Execute a task with DB logging."""
    async with async_session() as db:
        row = await db.execute(
            text("SELECT task_type, config, max_retries FROM task_config WHERE name=:name"),
            {"name": task_name}
        )
        row = row.one_or_none()
        if not row:
            logger.error("Task %s not found", task_name)
            return
        task_type, config_json, max_retries = row
        config = config_json if isinstance(config_json, dict) else json.loads(config_json)
        task_cfg = {"task_type": task_type, "config": config}

    run_id = await _log_task_start(task_name, db)
    logger.info("Starting task %s (run_id=%s)", task_name, run_id)

    last_error = ""
    details = None
    start = time.monotonic()

    for attempt in range(max_retries + 1):
        try:
            executor = EXECUTORS.get(task_type)
            if not executor:
                raise ValueError(f"Unknown task_type: {task_type}")
            result = await executor(config)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            details = result

            async with async_session() as db2:
                await _log_task_end(run_id, "success", db2, elapsed_ms, details=details)
            logger.info("Task %s succeeded (%dms)", task_name, elapsed_ms)
            return
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning("Task %s attempt %d failed: %s", task_name, attempt + 1, last_error)
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    async with async_session() as db2:
        await _log_task_end(run_id, "failed", db2, elapsed_ms, error=last_error)
    logger.error("Task %s failed: %s", task_name, last_error)


# ── Scheduler Setup ─────────────────────────────────────────────────

def create_scheduler() -> AsyncIOScheduler:
    jobstores = {
        "default": SQLAlchemyJobStore(
            engine=sync_engine,
            tablename="apscheduler_jobs",
        )
    }
    executors = {
        "default": AsyncIOExecutor(),
    }
    return AsyncIOScheduler(
        jobstores=jobstores,
        executors=executors,
        timezone=TZ,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 600,
        },
    )


async def load_tasks(scheduler: AsyncIOScheduler):
    """Read tasks from DB and register with APScheduler."""
    async with async_session() as db:
        rows = await db.execute(
            text("SELECT name, cron_expr, timezone, enabled, description FROM task_config ORDER BY name")
        )
        tasks = rows.fetchall()

    # Remove stale jobs that are no longer in the DB
    db_names = {row[0] for row in tasks if row[3]}  # only enabled tasks
    for job in scheduler.get_jobs():
        if job.id not in db_names:
            try:
                scheduler.remove_job(job.id)
                logger.info("Removed stale job: %s", job.id)
            except Exception:
                pass

    for name, cron_expr, tz_name, enabled, description in tasks:
        if not enabled:
            continue
        try:
            tz = pytz.timezone(tz_name or "America/Chicago")
            trigger = CronTrigger.from_crontab(cron_expr, timezone=tz)
            scheduler.add_job(
                wrapped_job, trigger=trigger, args=[name],
                id=name, name=name, replace_existing=True,
            )
            logger.info("Registered task %s: cron=%s", name, cron_expr)
        except Exception as e:
            logger.error("Failed to register task %s: %s", name, e)


def _update_next_runs():
    """Refresh in-memory next_run from scheduler."""
    global _next_runs
    if _scheduler is None:
        return
    now = datetime.now(TZ)
    next_runs = {}
    for job in _scheduler.get_jobs():
        if job.next_run_time:
            diff = job.next_run_time - now
            mins = int(diff.total_seconds() / 60)
            if mins < 0:
                next_runs[job.id] = "now"
            elif mins < 60:
                next_runs[job.id] = f"in {mins}m"
            elif mins < 1440:
                next_runs[job.id] = f"in {mins // 60}h {mins % 60}m"
            else:
                days = mins // 1440
                next_runs[job.id] = f"in {days}d {(mins % 1440) // 60}h"
        else:
            next_runs[job.id] = "—"
    _next_runs = next_runs


# ── Admin Helpers ───────────────────────────────────────────────────

def get_next_run_times() -> dict[str, str]:
    return _next_runs


async def get_task_statuses() -> list[dict[str, Any]]:
    """Return all tasks with last run info (async version)."""
    async with async_session() as db:
        rows = await db.execute(text("""
            SELECT tc.id, tc.name, tc.description, tc.task_type,
                   tc.cron_expr, tc.timezone, tc.enabled, tc.created_at,
                   tr.status AS last_status, tr.started_at AS last_run,
                   tr.duration_ms AS last_duration, tr.error_message AS last_error
            FROM task_config tc
            LEFT JOIN LATERAL (
                SELECT status, started_at, duration_ms, error_message
                FROM task_runs WHERE task_name = tc.name
                ORDER BY started_at DESC LIMIT 1
            ) tr ON true
            ORDER BY tc.name
        """))
        cols = rows.keys()
        result = [dict(zip(cols, row)) for row in rows]
        for r in result:
            r["next_run"] = _next_runs.get(r["name"])
        return result


async def get_task_runs(task_name: str, limit: int = 20) -> list[dict[str, Any]]:
    async with async_session() as db:
        rows = await db.execute(
            text("""SELECT id, task_name, status, started_at, finished_at,
                     duration_ms, error_message, details, created_at
                     FROM task_runs WHERE task_name = :n
                     ORDER BY started_at DESC LIMIT :l"""),
            {"n": task_name, "l": limit}
        )
        cols = rows.keys()
        return [dict(zip(cols, row)) for row in rows]


# ── Lifecycle ───────────────────────────────────────────────────────

async def start_scheduler():
    """Start the scheduler and register all tasks."""
    global _scheduler
    _scheduler = create_scheduler()
    await load_tasks(_scheduler)
    _scheduler.start()
    _update_next_runs()
    logger.info("Scheduler started with %d jobs", len(_scheduler.get_jobs()))

    # Periodic next-run refresh
    async def _refresh():
        while True:
            await asyncio.sleep(30)
            _update_next_runs()
    asyncio.create_task(_refresh())


async def stop_scheduler():
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler shut down")


async def trigger_task(task_name: str) -> bool:
    """Manually trigger a task to run now."""
    if _scheduler is None:
        return False
    job = _scheduler.get_job(task_name)
    if not job:
        return False
    _scheduler.modify_job(task_name, next_run_time=datetime.now(TZ))
    return True
