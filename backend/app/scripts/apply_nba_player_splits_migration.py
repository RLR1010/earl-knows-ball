"""Apply the nba.player_splits migration. Idempotent (CREATE IF NOT EXISTS).

Creates nba.player_splits: per-player, per-split-type stats used by Earl's chat
research. Sources are nba.player_game_stats x nba.games (no new external data).
season_id NULL = career aggregate.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260812_nba_player_splits.sql"
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
                "WHERE table_schema='nba' AND table_name='player_splits'"
            )
        ).scalar()
        print(f"  nba.player_splits columns = {n}")
    print("Done.")


if __name__ == "__main__":
    main()
