"""Idempotent migration: keep mlb.pitcher_game_stats / mlb.bullpen_game_stats.game_timestamp
in sync with mlb.games.date automatically.

Root cause it fixes (2026-09-17): those two tables have no date column of their own; the
`game_timestamp` column (and its indexes, e.g. idx_pgs_name_ts) was bolted on and backfilled
ONCE by an ad-hoc script (scripts_tmp/add_ts_4tables.py) and is NOT written by the ingestion
pipeline. Every row ingested after that backfill (>= 2026-08-21) was therefore NULL, which
silently broke the MLB loader's pitcher/bullpen lookups.

This installs a BEFORE INSERT OR UPDATE OF game_id trigger on both tables that sets
NEW.game_timestamp := (SELECT date FROM mlb.games WHERE id = NEW.game_id), then backfills
any NULL/stale values. Run:  ../venv/bin/python app/scripts/add_game_timestamp_triggers.py
"""
import asyncio
import re

import asyncpg

TABLES = ["pitcher_game_stats", "bullpen_game_stats"]

FUNC_SQL = """
CREATE OR REPLACE FUNCTION mlb.set_game_timestamp_from_game() RETURNS trigger AS $$
BEGIN
    IF NEW.game_id IS NOT NULL THEN
        NEW.game_timestamp := (SELECT g.date FROM mlb.games g WHERE g.id = NEW.game_id);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

# NOTE: do NOT use DROP TRIGGER IF EXISTS here. Dropping (or replacing) a trigger needs an
# ACCESS EXCLUSIVE lock on the table, which blocks behind any in-flight read and can hang for
# minutes. The trigger body lives in the function, which CREATE OR REPLACE updates without a
# table lock — so we only need to CREATE the trigger when it is missing. This keeps re-runs cheap.
TRIGGER_SQL = """
CREATE TRIGGER {trg}
    BEFORE INSERT OR UPDATE OF game_id ON mlb.{tbl}
    FOR EACH ROW EXECUTE FUNCTION mlb.set_game_timestamp_from_game();
"""

TRIGGER_EXISTS_SQL = (
    "SELECT 1 FROM pg_trigger WHERE tgname = $1 AND tgrelid = 'mlb.{tbl}'::regclass"
)

BACKFILL_SQL = """
UPDATE mlb.{tbl} t
SET game_timestamp = g.date
FROM mlb.games g
WHERE g.id = t.game_id
  AND t.game_timestamp IS DISTINCT FROM g.date;
"""


def _url():
    u = None
    for line in open(".env"):
        if line.startswith("DATABASE_URL="):
            u = line.strip().split("=", 1)[1]
    return re.sub(r"^postgresql\+asyncpg://", "postgresql://", u)


async def main():
    conn = await asyncpg.connect(_url())
    try:
        await conn.execute(FUNC_SQL)
        for t in TABLES:
            trg = f"trg_{t}_game_ts"
            if await conn.fetchval(TRIGGER_EXISTS_SQL.format(tbl=t), trg):
                created = "already present"
            else:
                await conn.execute(TRIGGER_SQL.format(trg=trg, tbl=t))
                created = "created"
            status = await conn.execute(BACKFILL_SQL.format(tbl=t))
            fixed = status.split()[-1] if status else "0"
            nulls = await conn.fetchval(
                f"SELECT count(*) FROM mlb.{t} WHERE game_timestamp IS NULL"
            )
            print(f"{t}: trigger {trg} {created}; backfilled={fixed} rows; nulls_now={nulls}")
    finally:
        await conn.close()


asyncio.run(main())
