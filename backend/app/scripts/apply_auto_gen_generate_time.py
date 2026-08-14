"""Apply the 20260814 generate_time migration."""
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.database import async_session  # noqa: E402
from sqlalchemy import text  # noqa: E402

SQL = Path(REPO_ROOT / "migrations" / "20260814_auto_gen_generate_time.sql").read_text()


async def main() -> None:
    async with async_session() as db:
        await db.execute(text(SQL))
        await db.commit()
        rows = (
            await db.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='auto_generation_configs' "
                    "AND column_name='generate_time'"
                )
            )
        ).all()
        print("Migration applied. generate_time column:", rows or "MISSING")


if __name__ == "__main__":
    asyncio.run(main())
