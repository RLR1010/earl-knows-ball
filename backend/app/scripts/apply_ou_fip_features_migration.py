"""Apply the OU FIP features migration to mlb.features.

Enables starter FIP (h_pitcher_fip_ytd / a_pitcher_fip_ytd) for the MLB OU model
(current_ou=TRUE) so get_model_features('ou') returns them. Left OUT of ATS.
Idempotent (ON CONFLICT DO UPDATE). 1-start outlier gating lives in
data_loader.build_features, not here.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


NEW_FEATURES = ["h_pitcher_fip_ytd", "a_pitcher_fip_ytd"]


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260819_ou_fip_features.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.exec_driver_sql(sql_file.read_text())
    print("Migration applied.")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name, current_ats, current_ou, is_trainable, pick_card "
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
