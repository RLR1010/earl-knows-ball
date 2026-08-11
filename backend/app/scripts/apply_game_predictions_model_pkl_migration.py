"""Apply the game_predictions model-provenance migration (mlb/nfl/nba).

Adds ats_model_file and ou_model_file columns to each sport's game_predictions
table so every pick records the exact pkl model file used for its picks.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_files = [
        root / "migrations" / "20260811_game_predictions_model_pkl.sql",
    ]
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        for sql_file in sql_files:
            print(f"Applying {sql_file.name}...")
            conn.exec_driver_sql(sql_file.read_text())
    print("Migration applied.")

    with engine.connect() as conn:
        for schema in ("mlb", "nfl", "nba"):
            for col in ("ats_model_file", "ou_model_file"):
                typ = conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema=:s AND table_name='game_predictions' "
                        "AND column_name=:c"
                    ),
                    {"s": schema, "c": col},
                ).scalar()
                print(f"  {schema}.{col} = {typ}")
    print("Done.")


if __name__ == "__main__":
    main()
