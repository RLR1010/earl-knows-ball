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

    print("[canonical-refresh] done", flush=True)


if __name__ == "__main__":
    main()
