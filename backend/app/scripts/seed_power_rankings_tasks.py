#!/usr/bin/env python3
"""Seed/refresh the weekly power-rankings scheduler tasks — one per sport.

Upserts rows into ``public.task_config`` so the scheduler runs the weekly
pipeline (ratings -> blurbs -> social card -> *weekly column*) for NFL, NBA and
MLB. Idempotent: re-running updates the existing task in place.

The generated command uses the interpreter running this script and the resolved
backend directory, so the same script works on dev and prod.

Usage:
    cd <backend> && PYTHONPATH=$PWD <venv>/bin/python app/scripts/seed_power_rankings_tasks.py
    ... --dry-run
    ... --cron "0 9 * * 2" --timezone America/New_York
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.db_urls import ASYNC_DATABASE_URL  # noqa: E402

SPORTS = ("nfl", "nba", "mlb")

_DESCRIPTIONS = {
    "nfl": "Weekly NFL power rankings: recompute points-denominated team ratings, regenerate blurbs, write the week's power-rankings column. Tue mornings (after MNF).",
    "nba": "Weekly NBA power rankings: recompute team ratings, regenerate blurbs, write the week's power-rankings column.",
    "mlb": "Weekly MLB power rankings: recompute team ratings, regenerate blurbs, write the week's power-rankings column.",
}

_UPSERT = text(
    """
    INSERT INTO public.task_config
        (name, task_type, config, cron_expr, timezone, enabled, max_retries, description, created_at)
    VALUES
        (:name, 'subprocess', CAST(:config AS jsonb), :cron, :tz, true, 2, :desc, now())
    ON CONFLICT (name) DO UPDATE SET
        task_type   = EXCLUDED.task_type,
        config      = EXCLUDED.config,
        cron_expr   = EXCLUDED.cron_expr,
        timezone    = EXCLUDED.timezone,
        enabled     = EXCLUDED.enabled,
        max_retries = EXCLUDED.max_retries,
        description = EXCLUDED.description
    """
)


def _command(sport: str) -> str:
    return (
        f"cd {BACKEND_DIR} && PYTHONPATH=$PWD {sys.executable} "
        f"app/scripts/run_power_rankings_weekly.py --sport {sport}"
    )


async def main(cron: str, tz: str, timeout: int, dry_run: bool) -> None:
    engine = create_async_engine(ASYNC_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            for sport in SPORTS:
                name = f"power-rankings-{sport}"
                cfg = {"command": _command(sport), "timeout": timeout}
                if dry_run:
                    print(f"[dry-run] {name}: cron={cron} tz={tz} config={json.dumps(cfg)}")
                    continue
                await conn.execute(
                    _UPSERT,
                    {"name": name, "config": json.dumps(cfg), "cron": cron, "tz": tz,
                     "desc": _DESCRIPTIONS[sport]},
                )
                print(f"seeded {name}: {cron} {tz}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Seed weekly power-rankings tasks (one per sport)")
    ap.add_argument("--cron", default="0 9 * * 2")
    ap.add_argument("--timezone", default="America/New_York")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.cron, args.timezone, args.timeout, args.dry_run))
