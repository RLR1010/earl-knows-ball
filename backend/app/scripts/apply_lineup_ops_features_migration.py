"""Apply the lineup-OPS features migration to mlb.features.

Adds 4 lineup-OPS features (h_lineup_ops, a_lineup_ops, h_lineup_ops_minus_team,
a_lineup_ops_minus_team), registered with is_trainable=TRUE, pick_card=TRUE, and
current_ats/current_ou/live_ats/live_ou=FALSE per Rich's spec (NOT in the live
ATS/OU sets -- he adds them during training). Idempotent (ON CONFLICT DO UPDATE).
Source columns are projected + auto-catalogued in MLB data_loader.GAME_QUERY.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


NEW_FEATURES = [
    "h_lineup_ops", "a_lineup_ops",
    "h_lineup_ops_minus_team", "a_lineup_ops_minus_team",
]


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260820_lineup_ops_features.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.exec_driver_sql(sql_file.read_text())
    print("Migration applied.")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name, is_trainable, current_ats, current_ou, live_ats, live_ou, pick_card "
                "FROM mlb.features WHERE name IN ("
                + ",".join(":" + f"f{i}" for i in range(len(NEW_FEATURES)))
                + ")"
            ),
            {f"f{i}": n for i, n in enumerate(NEW_FEATURES)},
        ).fetchall()
        print(f"  mlb.features rows ({len(rows)}):")
        for r in sorted(rows):
            print(f"    {r}")
    print("Done.")


if __name__ == "__main__":
    main()
