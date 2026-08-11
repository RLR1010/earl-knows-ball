"""Apply the game_writeups schema alignment migration (nfl/nba -> mlb parity)."""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_files = [
        root / "migrations" / "20260810_align_writeups_schema.sql",
    ]
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        for sql_file in sql_files:
            print(f"Applying {sql_file.name}...")
            conn.exec_driver_sql(sql_file.read_text())
    print("Migration applied.")

    # Verify alignment: research_brief/quality_checks should be jsonb everywhere,
    # and NFL defaults should now match MLB.
    with engine.connect() as conn:
        for schema in ("mlb", "nfl", "nba"):
            rb = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema=:s AND table_name='game_writeups' "
                    "AND column_name='research_brief'"
                ),
                {"s": schema},
            ).scalar()
            qc = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema=:s AND table_name='game_writeups' "
                    "AND column_name='quality_checks'"
                ),
                {"s": schema},
            ).scalar()
            print(f"  {schema}: research_brief={rb}, quality_checks={qc}")

        nfl_def = conn.execute(
            text(
                "SELECT column_name, column_default FROM information_schema.columns "
                "WHERE table_schema='nfl' AND table_name='game_writeups' "
                "AND column_name IN ('public_content','premium_content','status',"
                "'version','is_historical','created_at','updated_at') "
                "ORDER BY column_name"
            )
        ).fetchall()
        print("  nfl column defaults:")
        for name, d in nfl_def:
            print(f"    {name} = {d}")

    print("Done.")


if __name__ == "__main__":
    main()
