#!/usr/bin/env python3
"""Register/refresh the post-game recap tasks in ``task_config`` (idempotent).

Creates one subprocess task per sport:
  * ``mlb-recaps`` / ``nfl-recaps`` / ``nba-recaps``
  * cron ``10 7 * * *`` tz ``America/New_York``  ->  next-morning, off-peak
    DeepSeek pricing, after overnight scrapes and before the 8:00 AM ET previews.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/seed_recap_tasks.py
    # prod:  python app/scripts/seed_recap_tasks.py \
    #          --backend-dir /home/rich/earl-knows-football/backend \
    #          --venv /home/rich/venv/bin/python
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.database import admin_async_session  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seed_recap_tasks")

CRON = "10 7 * * *"
TZ = "America/New_York"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-dir", default=str(BACKEND_DIR))
    parser.add_argument(
        "--venv",
        default=str(BACKEND_DIR.parent / "venv" / "bin" / "python"),
    )
    parser.add_argument("--disable", action="store_true", help="register but disabled")
    args = parser.parse_args()

    enabled = not args.disable
    async with admin_async_session() as session:
        for sport in ("mlb", "nfl", "nba"):
            name = f"{sport}-recaps"
            command = (
                f"cd {args.backend_dir} && PYTHONPATH=$PWD {args.venv} "
                f"app/scripts/run_recaps.py {sport}"
            )
            config = json.dumps({"command": command, "timeout": 1800})
            desc = (
                f"Post-game {sport.upper()} recap articles (free, SEO). "
                "Next-morning run: off-peak DeepSeek, after overnight scrapes, "
                "before 8:00 AM ET previews."
            )
            await session.execute(
                text(
                    """
                    INSERT INTO task_config
                        (name, task_type, config, cron_expr, timezone, enabled, max_retries, description)
                    VALUES
                        (:name, 'subprocess', CAST(:config AS jsonb), :cron, :tz, :enabled, 2, :desc)
                    ON CONFLICT (name) DO UPDATE SET
                        task_type = EXCLUDED.task_type,
                        config = EXCLUDED.config,
                        cron_expr = EXCLUDED.cron_expr,
                        timezone = EXCLUDED.timezone,
                        enabled = EXCLUDED.enabled,
                        max_retries = EXCLUDED.max_retries,
                        description = EXCLUDED.description
                    """
                ),
                {
                    "name": name,
                    "config": config,
                    "cron": CRON,
                    "tz": TZ,
                    "enabled": enabled,
                    "desc": desc,
                },
            )
            log.info("registered %s (enabled=%s) cron=%s tz=%s", name, enabled, CRON, TZ)
        await session.commit()
    log.info("recap task registration complete")


if __name__ == "__main__":
    asyncio.run(main())
