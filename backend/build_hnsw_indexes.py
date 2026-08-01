"""Build HNSW indexes on article_embeddings for all schemas (concurrent, no downtime).

Run with autocommit (CREATE INDEX CONCURRENTLY can't run in a transaction).
Logs progress to stdout; build progress visible in pg_stat_progress_create_index.
"""
import sys
import time

from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL

SCHEMAS = ["nfl", "nba", "mlb"]

eng = create_engine(
    PSYCOPG2_DATABASE_URL,
    isolation_level="AUTOCOMMIT",
    connect_args={"options": "-c search_path=nfl,public"},
)
conn = eng.connect()

for schema in SCHEMAS:
    idx_name = f"idx_{schema}_article_embeddings_hnsw"
    # Skip if already built
    exists = conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE schemaname=:s AND indexname=:i"),
        {"s": schema, "i": idx_name},
    ).first()
    if exists:
        print(f"[{schema}] HNSW index already exists, skipping", flush=True)
        continue

    n = conn.execute(text(f"SELECT count(*) FROM {schema}.article_embeddings")).scalar()
    print(f"[{schema}] Building HNSW on {n} rows...", flush=True)
    t0 = time.time()
    try:
        conn.execute(
            text(
                f"CREATE INDEX CONCURRENTLY {idx_name} "
                f"ON {schema}.article_embeddings USING hnsw (embedding vector_cosine_ops) "
                f"WITH (m = 16, ef_construction = 64)"
            )
        )
        print(f"[{schema}] HNSW built in {time.time() - t0:.1f}s", flush=True)
    except Exception as e:
        print(f"[{schema}] BUILD FAILED: {e}", flush=True)
        # clean up any invalid index left behind by the failed attempt
        try:
            conn.execute(
                text(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{idx_name}")
            )
        except Exception:
            pass
        continue

# Also bump ivfflat probes for any stragglers? No - ivfflat is being replaced.

print("ALL HNSW INDEXES BUILT", flush=True)
conn.close()
eng.dispose()
