"""Apply the player_splits migration (mlb only).

Creates mlb.player_splits: per-player, per-split-type batting stats used by
Earl's chat research and the premium Prop Bets writeup. Supports batter L/R
splits (vs_lhp/vs_rhp), home/away, day/night, grass/turf, and derived city
splits (city_<slug>) plus career-level rows (season_id NULL).
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260812_player_splits.sql"
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
                "WHERE table_schema='mlb' AND table_name='player_splits'"
            )
        ).scalar()
        print(f"  mlb.player_splits columns = {n}")
    print("Done.")


if __name__ == "__main__":
    main()
