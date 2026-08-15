"""Rebuild all derived NBA stat tables from the corrected nba.games boxscores.

After backfill_nba_games_boxscores.py fixed the stale steal/block/turnover
columns, the derived tables (cumulative_game_stats, team_rolling_stats,
team_splits) still hold OLD computed values. This rebuilds all three from
scratch (full, not incremental) so every NBA feature is correct.

Usage: cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/rebuild_nba_derived.py [--skip-splits]
"""
import sys, os, time, argparse
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from sqlalchemy import create_engine
from app.core.config import settings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-splits", action="store_true", help="Skip team_splits rebuild")
    args = ap.parse_args()

    db_url = settings.database_url_sync
    t0 = time.time()

    # 1) Cumulative (full rebuild — drop + recreate)
    print("=== REBUILDING nba.cumulative_game_stats (full) ===")
    from app.handicapping.nba.cumulative_stats import populate_cumulative_stats
    cum = populate_cumulative_stats(db_url, seasons=None, force_rebuild=True)
    print(f"  cumulative done: {cum} [{time.time()-t0:.0f}s]")

    # 2) Team rolling (full rebuild — truncates)
    print("=== REBUILDING nba.team_rolling_stats (full) ===")
    from app.handicapping.nba.populate_team_rolling_stats import populate_team_rolling
    engine = create_engine(db_url)
    r = populate_team_rolling(engine, incremental=False)
    print(f"  rolling done: {r} [{time.time()-t0:.0f}s]")

    # 3) Team splits
    if not args.skip_splits:
        print("=== REBUILDING nba.team_splits ===")
        import asyncio
        from app.ingestion.nba_team_splits import build_team_splits
        from backend.app.database import async_session

        async def _splits():
            async with async_session() as db:
                return await build_team_splits(db)
        s = asyncio.run(_splits())
        print(f"  splits done: {s} [{time.time()-t0:.0f}s]")

    print(f"\nALL REBUILDS COMPLETE [{time.time()-t0:.0f}s]")


if __name__ == "__main__":
    main()
