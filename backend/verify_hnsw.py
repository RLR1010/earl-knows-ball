"""Verify HNSW indexes: timing + recall vs exact brute-force search.

For each schema, runs several sample queries and compares:
  - HNSW top-10 vs exact top-10 (recall = overlap / 10)
  - wall-clock time for HNSW vs exact
Uses the live embedding model for query vectors.
"""
import sys
import time

from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL
from app.ollama_embed import embed_sync

ENG = create_engine(PSYCOPG2_DATABASE_URL, connect_args={"options": "-c search_path=nfl,public"})

QUERIES = {
    "nfl": [
        "Which NFL team has the best defense against the run?",
        "Quarterback injuries and backup situations this week",
        "Best wide receiver matchups and prop bets",
    ],
    "nba": [
        "Which team has the best three point shooting?",
        "Injury report and minutes distribution",
        "Defensive ratings and matchup analysis",
    ],
    "mlb": [
        "Starting pitcher strikeout props tonight",
        "Bullpen usage and closer situations",
        "Team batting splits versus lefties",
    ],
}


def exact_top10(conn, schema, emb_str):
    # Force seq scan (disable all index scans) => exact brute-force ranking
    conn.execute(text("SET LOCAL enable_indexscan = off"))
    conn.execute(text("SET LOCAL enable_bitmapscan = off"))
    rows = conn.execute(
        text(
            f"SELECT article_id FROM {schema}.article_embeddings "
            f"ORDER BY embedding <=> '{emb_str}'::vector LIMIT 10"
        )
    ).fetchall()
    return [r[0] for r in rows]


def hnsw_top10(conn, schema, emb_str):
    conn.execute(text("SET LOCAL enable_indexscan = on"))
    conn.execute(text("SET LOCAL enable_bitmapscan = on"))
    conn.execute(text("SELECT set_config('hnsw.ef_search', '100', false)"))
    rows = conn.execute(
        text(
            f"SELECT article_id FROM {schema}.article_embeddings "
            f"ORDER BY embedding <=> '{emb_str}'::vector LIMIT 10"
        )
    ).fetchall()
    return [r[0] for r in rows]


def main():
    for schema, queries in QUERIES.items():
        # confirm planner uses hnsw now
        with ENG.connect() as c:
            fake = "[" + ",".join(["0.01"] * 1024) + "]"
            plan = c.execute(
                text(
                    f"EXPLAIN SELECT article_id FROM {schema}.article_embeddings "
                    f"ORDER BY embedding <=> '{fake}'::vector LIMIT 10"
                )
            ).fetchall()
        uses_hnsw = any("hnsw" in str(r[0]) for r in plan)
        print(f"\n=== {schema} ===  planner: {'HNSW index scan' if uses_hnsw else 'OTHER: ' + str(plan[0][0][:80])}")

        for q in queries:
            emb = embed_sync(q)
            emb_str = "[" + ",".join(str(v) for v in emb) + "]"
            with ENG.connect() as c:
                c.execute(text("BEGIN"))
                t0 = time.perf_counter()
                exact = exact_top10(c, schema, emb_str)
                t_exact = time.perf_counter() - t0
                c.execute(text("ROLLBACK"))

                c.execute(text("BEGIN"))
                t0 = time.perf_counter()
                hnsw = hnsw_top10(c, schema, emb_str)
                t_hnsw = time.perf_counter() - t0
                c.execute(text("ROLLBACK"))

            overlap = len(set(exact) & set(hnsw))
            print(f"  recall={overlap}/10  hnsw={t_hnsw*1000:.0f}ms  exact={t_exact*1000:.0f}ms  | {q[:50]}")

    ENG.dispose()


if __name__ == "__main__":
    main()
