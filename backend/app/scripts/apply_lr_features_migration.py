"""Apply the L/R training-features migration (mlb.features only).

Adds 10 features:
- h_/a_ ops_vs_rhp, ops_vs_lhp, rpg_vs_rhp, rpg_vs_lhp (is_trainable + pick_card)
- h_/a_ pitcher_hand (pick_card only)

Idempotent via ON CONFLICT (name) DO NOTHING.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260812_training_lr_features.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.exec_driver_sql(sql_file.read_text())
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name, is_trainable, pick_card, current_ats, current_ou, live_ats, live_ou "
                "FROM mlb.features "
                "WHERE name IN ('h_ops_vs_rhp','h_ops_vs_lhp','a_ops_vs_rhp','a_ops_vs_lhp',"
                "'h_rpg_vs_rhp','h_rpg_vs_lhp','a_rpg_vs_rhp','a_rpg_vs_lhp',"
                "'h_pitcher_hand','a_pitcher_hand') ORDER BY name"
            )
        ).fetchall()
        print("\nNew features:")
        for r in rows:
            print(f"  {r[0]:<15} train={r[1]} pc={r[2]} ats={r[3]} ou={r[4]} live_ats={r[5]} live_ou={r[6]}")


if __name__ == "__main__":
    main()
