"""
Run one or more SQL migration files against the Earl DB.

Usage:
    ../venv/bin/python scripts/run_migration.py migrations/20260828_parlay_tickets.sql
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
import asyncpg

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=True)


def split_statements(sql: str):
    """Split '-'+';'-terminated statements, skipping comments/whitespace only."""
    out, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return [s for s in out if s.strip()]


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    try:
        for path in sys.argv[1:]:
            sql = open(path).read()
            statements = split_statements(sql)
            print(f"-- {path}: {len(statements)} statement(s)")
            for stmt in statements:
                tag = stmt.split("\n", 1)[0].strip()[:70]
                await conn.execute(stmt)
                print(f"   ok: {tag}")
            print(f"-- {path}: DONE")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
