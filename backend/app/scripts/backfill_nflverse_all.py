#!/usr/bin/env python3
"""Load ALL nflverse canonical datasets year-by-year, newest -> oldest.

Honours the workspace cardinal rule: process YEAR 2026 for every dataset, then
2025 for every dataset, then 2024 ... so an interrupted run still has the most
recent content everywhere.

Non-yearly reference datasets (schedules, players, teams, draft, officials,
combine, contracts) are loaded once up front.

Usage:
    PYTHONPATH=$PWD python3 app/scripts/backfill_nflverse_all.py --start 1999 --end 2026
    ... --datasets pbp,stats_team_week,stats_player_week,schedules
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import asyncpg

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ingestion.nflverse_hub import (  # noqa: E402
    DATASETS, backfill, dataset_type_plan, _dsn, _ident,
)

# default Phase-1+2 set (skip reference datasets here; loaded separately)
YEARLY_DEFAULT = [
    "pbp", "stats_team_week", "stats_player_week",
    "injuries", "snap_counts", "participation",
    "ngs_passing", "ngs_receiving", "ngs_rushing",
    "ftn_charting",
    "advstats_def", "advstats_pass", "advstats_rush", "advstats_rec",
    "roster_weekly",
]
NONYEARLY = ["schedules", "players", "teams", "draft_picks", "officials",
             "combine", "contracts"]


async def run(start: int, end: int, keys: list[str], recreate: bool = False) -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        if recreate:
            for k in keys:
                await conn.execute(f'DROP TABLE IF EXISTS nfl.{_ident(DATASETS[k].table)} CASCADE')
                print(f"[drop] nfl.{DATASETS[k].table}", flush=True)
    finally:
        await conn.close()

    # 0) precompute one union type-plan per dataset (accuracy: consistent types)
    plans: dict[str, dict] = {}
    for k in keys:
        ds = DATASETS[k]
        seasons = list(range(start, end + 1)) if ds.yearly else None
        if ds.yearly:
            seasons = [s for s in seasons if ds.start <= s <= ds.end]
        try:
            plans[k] = dataset_type_plan(ds, seasons, force=recreate)
            print(f"[plan] {k}: {len(plans[k])} columns typed", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[plan-FAIL] {k}: {e!r}", flush=True)

    # 1) reference (non-yearly) once
    conn = await asyncpg.connect(_dsn())
    try:
        for k in NONYEARLY:
            if k not in keys:
                continue
            try:
                info = await backfill(k, newest_first=False, plan=plans.get(k))
                print(f"[ref] {k} -> {info}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[ref-FAIL] {k}: {e!r}", flush=True)
    finally:
        await conn.close()

    # 2) year-by-year, newest -> oldest, across every yearly dataset
    for year in range(end, start - 1, -1):
        print(f"\n========== YEAR {year} ==========", flush=True)
        for k in keys:
            ds = DATASETS[k]
            if not ds.yearly:
                continue
            if year < ds.start or year > ds.end:
                continue
            t0 = time.time()
            try:
                await backfill(k, year, year, newest_first=True, plan=plans.get(k))
                print(f"  ok {k} ({time.time()-t0:.1f}s)", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  FAIL {k}: {e!r}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=1999)
    ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--datasets", default=",".join(YEARLY_DEFAULT + NONYEARLY))
    ap.add_argument("--recreate", action="store_true")
    a = ap.parse_args()
    keys = [k.strip() for k in a.datasets.split(",") if k.strip()]
    asyncio.run(run(a.start, a.end, keys, recreate=a.recreate))


if __name__ == "__main__":
    main()
