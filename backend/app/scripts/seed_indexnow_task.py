#!/usr/bin/env python3
"""Register/refresh the IndexNow ping task in ``task_config`` (idempotent).

Creates one subprocess task:
  * ``indexnow-ping``  — hourly, submits recently published URLs to IndexNow
    (Bing/Yandex instant indexing) via ``app/scripts/ping_indexnow.py``.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/seed_indexnow_task.py
    # prod:  python app/scripts/seed_indexnow_task.py \
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
log = logging.getLogger("seed_indexnow_task")

NAME = "indexnow-ping"
CRON = "17 * * * *"          # hourly at :17
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
    command = (
        f"cd {args.backend_dir} && PYTHONPATH=$PWD {args.venv} "
        f"app/scripts/ping_indexnow.py --hours 2 --include-feeds"
    )
    config = json.dumps({"command": command, "timeout": 600})
    desc = (
        "Submit recently published URLs (articles, recaps, previews) to "
        "IndexNow for instant Bing/Yandex indexing. Hourly."
    )

    async with admin_async_session() as session:
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
                "name": NAME,
                "config": config,
                "cron": CRON,
                "tz": TZ,
                "enabled": enabled,
                "desc": desc,
            },
        )
        await session.commit()
    log.info("registered %s (enabled=%s) cron=%s tz=%s", NAME, enabled, CRON, TZ)


if __name__ == "__main__":
    asyncio.run(main())
