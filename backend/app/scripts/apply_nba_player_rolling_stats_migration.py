"""Apply the 20260821_nba_player_rolling_stats migration. Idempotent (IF NOT EXISTS).

Creates nba.player_rolling_stats (one row per player-game) mirroring
mlb.player_batting_rolling_stats: per-game raw stats, season-to-date cumulative,
rolling windows (ppg_5/10/15/30 etc.), and prior-game pointers. Columns nullable;
populated by the populate_player_rolling_stats builder on next run.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260821_nba_player_rolling_stats.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.exec_driver_sql(sql_file.read_text())
    print("Migration applied.")
    with engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='nba' AND table_name='player_rolling_stats'"
            )
        ).scalar()
        print(f"  nba.player_rolling_stats columns = {n}")


if __name__ == "__main__":
    main()
