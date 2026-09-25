#!/usr/bin/env python3
"""Backfill canonical nflverse history into the `nfl` schema.

Example:
    cd <backend> && PYTHONPATH=$PWD python3 app/scripts/backfill_nflverse_history.py \
        --dataset stats_team_week --start 1999 --end 2026
    ... --dataset pbp --start 1999 --end 2026
    ... --list
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.ingestion.nflverse_hub import DATASETS, backfill  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", "-d")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--oldest-first", action="store_true")
    ap.add_argument("--recreate", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    if a.list:
        for k, ds in DATASETS.items():
            rng = f"{ds.start}-{ds.end}" if ds.yearly else "non-yearly"
            print(f"{k:20s} -> nfl.{ds.table:22s} {rng}")
        return

    if not a.dataset:
        ap.error("--dataset required")
    if a.dataset not in DATASETS:
        ap.error(f"unknown dataset {a.dataset}; see --list")

    res = asyncio.run(backfill(
        a.dataset, a.start, a.end,
        newest_first=not a.oldest_first, recreate=a.recreate, dry_run=a.dry_run,
    ))
    total = sum(r.get("source_rows", 0) for r in res)
    print(f"\nDONE {a.dataset}: {len(res)} seasons, {total:,} source rows loaded.")


if __name__ == "__main__":
    main()
