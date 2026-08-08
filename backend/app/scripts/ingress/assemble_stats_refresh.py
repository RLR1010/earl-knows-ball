#!/usr/bin/env python3
"""One-time assemble script: build the three stats-refresh subprocess scripts
from verbatim source extracted from app/routers/ingest.py.

Reads /tmp/sr_extract/_run_{sport}_stats_refresh.py (exact function bodies)
and emits app/scripts/ingress/run_{sport}_stats_refresh.py with a standard
standalone main() wrapper. Helper references (_run_in_thread, _report_task_outcome)
are rewritten to the shared _ingest_common module.

This exists to guarantee byte-for-byte verbatim fidelity of the heavy worker
logic during the migration. After assembly the source files are no longer needed.
"""
import re
from pathlib import Path

OUT_DIR = Path("app/scripts/ingress")
HEADER = '''#!/usr/bin/env python3
"""
SPORT_UPPER stats refresh — standalone subprocess job.

Runs via the Earl task scheduler as a `subprocess` task (previously an
`api_call` hitting /ingest/SPORT/stats/refresh). Moved off the granian event loop
so it can never block a request-serving worker.

Previously this was a fire-and-forget `asyncio.create_task` inside a granian
worker loop; the route returned ~242ms "success" and the scheduler recorded a
fake success before the background work (with real failures) finished. Now the
entire refresh runs in a real OS subprocess, reports nothing until it is
actually done, and updates the real `task_runs` row via report_task_outcome.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_SPORT_stats_refresh.py

Exit code 0 on success, non-zero on failure.
"""

import asyncio
import logging
import os
import sys

# sys.path: make the repo importable when run as <repo>/backend/app/scripts/...py
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.app.database import async_session  # noqa: E402
from app.scripts.ingress._ingest_common import (  # noqa: E402
    run_in_thread,
    report_task_outcome,
    mlb_full_refresh_due,
    mlb_mark_full_refresh,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("earl.SPORT_stats_refresh")
'''

MAIN_FOOTER = '''

async def _run_standalone() -> int:
    from datetime import datetime, timezone
    started_at = datetime.now(timezone.utc)
    try:
        # The worker body reports its own outcome internally (report_task_outcome)
        # on success OR failure, mirroring the old fire-and-forget flow.
        await run(started_at)
        return 0
    except Exception:
        import traceback
        logger.error("SPORT stats refresh CRASHED: " + traceback.format_exc())
        # Only report here if the worker never got a chance to (hard crash).
        try:
            await async_session_commit_crash("SPORT", started_at)
        except Exception:
            pass
        return 1


async def async_session_commit_crash(sport: str, started_at) -> None:
    from datetime import datetime, timezone
    from app.scripts.ingress._ingest_common import report_task_outcome
    await report_task_outcome(
        sport + "-stats-refresh", success=False, error="crash", started_at=started_at,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_standalone()))
'''


def build(sport):
    src = Path(f"/tmp/sr_extract/_run_{sport}_stats_refresh.py").read_text()
    body = src.strip()
    # Rename the worker def to `run` (it's already `async def _run_X_stats_refresh`)
    # so the wrapper can call it simply.
    body = re.sub(
        rf"async def _run_{sport}_stats_refresh\(",
        "async def run(",
        body,
        count=1,
    )
    # Rewrite helper refs -> shared module.
    body = body.replace("_report_task_outcome(", "report_task_outcome(")
    body = body.replace("_run_in_thread(", "run_in_thread(")
    body = body.replace("_mlb_full_refresh_due(", "mlb_full_refresh_due(")
    body = body.replace("_mlb_mark_full_refresh(", "mlb_mark_full_refresh(")

    header = HEADER.replace("SPORT_UPPER", sport.upper()).replace("SPORT", sport)
    footer = MAIN_FOOTER.replace("SPORT", sport)
    out = header + "\n\n" + body + "\n" + footer
    (OUT_DIR / f"run_{sport}_stats_refresh.py").write_text(out)
    print(f"wrote run_{sport}_stats_refresh.py ({len(out)} bytes)")


for s in ("mlb", "nfl", "nba"):
    build(s)
print("done")
