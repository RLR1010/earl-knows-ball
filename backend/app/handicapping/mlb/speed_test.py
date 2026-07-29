#!/usr/bin/env python3
"""Speed test with proper WHERE injection."""
import time, logging
from sqlalchemy import create_engine, text

from backend.app.core.config import settings
from backend.app.handicapping.mlb.data_loader import GAME_QUERY as OLD_QUERY
from backend.app.handicapping.mlb.data_loader_v2 import GAME_QUERY as NEW_QUERY

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("speed_test")
DB = settings.database_url_sync

YEAR, STATUS, LIMIT = 2024, "FINAL", 1000
WHERE = f"\nWHERE s.year = {YEAR} AND g.status = '{STATUS}'\nORDER BY g.date DESC\nLIMIT {LIMIT}"


def patched(sql):
    sql = sql.strip().rstrip(";").rstrip()
    if sql.endswith("ORDER BY g.date DESC"):
        sql = sql[: -len("ORDER BY g.date DESC")].rstrip()
    return sql + WHERE


def run(name, sql):
    engine = create_engine(DB)
    try:
        t0 = time.time()
        with engine.connect() as conn:
            df = __import__("pandas").read_sql(text(sql), conn)
        t = time.time() - t0
        logger.info("  %s: %.2f sec, %d rows x %d cols", name, t, len(df), len(df.columns))
        return t, len(df), len(df.columns)
    finally:
        engine.dispose()


if __name__ == "__main__":
    old_sql, new_sql = patched(OLD_QUERY), patched(NEW_QUERY)
    logger.info("Old: %d chars | New: %d chars", len(old_sql), len(new_sql))

    for label, sq in [("OLD", old_sql), ("NEW", new_sql)]:
        try:
            run(label + " (validate)", sq)
        except Exception as e:
            logger.error("%s FAILED: %s", label, e)
            raise

    to, tn = [], []
    for i in range(3):
        logger.info("--- Run %d ---", i + 1)
        t, *_ = run("OLD", old_sql)
        to.append(t)
        t, *_ = run("NEW", new_sql)
        tn.append(t)

    ao, an = sum(to) / len(to), sum(tn) / len(tn)
    logger.info("=" * 50)
    logger.info("  OLD (monster CTE):  avg %.2f sec", ao)
    logger.info("  NEW (pre-computed): avg %.2f sec", an)
    logger.info("  Speedup: %.1f\u00d7", ao / an)
    logger.info("=" * 50)
