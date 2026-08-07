"""Apply the accuracy-verification column migrations (writeups + original articles).

Adds accuracy_check (JSON) and accuracy_check_tokens (INTEGER) to:
  - mlb.game_writeups, nfl.game_writeups, nba.game_writeups
  - public.original_articles
"""
from pathlib import Path

from sqlalchemy import text, create_engine
from app.core.config import settings


def apply(file_list):
    root = Path(__file__).resolve().parents[2]
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        for path in file_list:
            print(f"Applying {path.name}…")
            conn.exec_driver_sql(path.read_text())
    return engine


def main():
    root = Path(__file__).resolve().parents[2]
    engine = apply(
        [
            root / "migrations" / "20260807_game_writeups_accuracy.sql",
            root / "migrations" / "20260807_original_articles_accuracy.sql",
        ]
    )
    print("Migrations applied. Verifying…")
    with engine.connect() as conn:
        for schema, table in [
            ("mlb", "game_writeups"),
            ("nfl", "game_writeups"),
            ("nba", "game_writeups"),
            ("public", "original_articles"),
        ]:
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema=:s AND table_name=:t "
                    "AND column_name IN ('accuracy_check','accuracy_check_tokens')"
                ),
                {"s": schema, "t": table},
            ).fetchall()
            print(f"{schema}.{table}: {[r[0] for r in rows]}")


main()
