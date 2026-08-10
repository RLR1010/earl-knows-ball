"""Apply the Prop Bets article columns migration to game_writeups (mlb/nfl/nba)."""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_files = [
        root / "migrations" / "20260809_game_writeups_props_article.sql",
    ]
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        for sql_file in sql_files:
            print(f"Applying {sql_file.name}...")
            conn.exec_driver_sql(sql_file.read_text())
    print("Migrations applied.")

    with engine.connect() as conn:
        for schema in ("mlb", "nfl", "nba"):
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name='game_writeups' "
                    "AND column_name IN ('prop_title','prop_content','prop_generated_by',"
                    "'prop_total_tokens','prop_published_at') ORDER BY column_name"
                ),
                {"schema": schema},
            ).fetchall()
            print(f"{schema}.game_writeups prop columns:", [r[0] for r in rows])


main()
