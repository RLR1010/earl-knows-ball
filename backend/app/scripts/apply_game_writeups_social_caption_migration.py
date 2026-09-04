"""Apply: add mlb.game_writeups.social_caption (caption to pair with the social card/post).

Usage:
    cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/apply_game_writeups_social_caption_migration.py

Idempotent: uses ADD COLUMN IF NOT EXISTS.
"""
import asyncio
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import settings


async def main() -> None:
    sql = (Path(__file__).resolve().parents[2]
           / "migrations" / "20260903_game_writeups_social_caption.sql").read_text()

    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)
    print("social_caption migration applied.")

    with engine.begin() as conn:
        cols = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='mlb' AND table_name='game_writeups' "
                "ORDER BY ordinal_position"
            )
        ).fetchall()
        print("game_writeups columns:", ", ".join(r[0] for r in cols))


if __name__ == "__main__":
    asyncio.run(main())
