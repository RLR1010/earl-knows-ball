"""Audit mlb.betting_lines + mlb.betting_lines_old for rows collected after game start.

Detects lines whose collection timestamp (api_last_update, or recorded_at as fallback
for batch/backfill rows) is after the game's start time (mlb.games.date).

Also flags "backfill signature" rows where recorded_at is far after game start
(indicating a batch backfill script wrote them, not the live task).
"""
from sqlalchemy import create_engine, text

ENGINE = create_engine("postgresql+psycopg2://earl:earl_dev_pass@localhost:5432/earl_knows_football")


def main() -> None:
    with ENGINE.connect() as conn:
        print("=" * 78)
        print("mlb.betting_lines columns:")
        for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='mlb' AND table_name='betting_lines' ORDER BY ordinal_position"
        )):
            print("  ", r[0])

        print("=" * 78)
        print("1) ROWS with api_last_update AFTER game start (live table):")
        r = conn.execute(text("""
            SELECT bl.game_id, to_char(g.date,'MM-DD HH24:MI') AS start,
                   bl.sportsbook, bl.is_opening,
                   to_char(bl.api_last_update,'MM-DD HH24:MI:SS') AS api,
                   to_char(bl.recorded_at,'MM-DD HH24:MI:SS') AS rec
            FROM mlb.betting_lines bl
            JOIN mlb.games g ON g.id = bl.game_id
            WHERE bl.api_last_update IS NOT NULL
              AND bl.api_last_update > g.date
            ORDER BY bl.api_last_update - g.date DESC
        """))
        rows = r.fetchall()
        print("   COUNT:", len(rows))
        for x in rows:
            print("  ", tuple(x))

        print("=" * 78)
        print("2) ROWS with recorded_at > 6h AFTER game start (backfill/batch signature):")
        r = conn.execute(text("""
            SELECT bl.game_id, to_char(g.date,'MM-DD HH24:MI') AS start,
                   bl.sportsbook, bl.is_opening,
                   to_char(bl.recorded_at,'MM-DD HH24:MI:SS') AS rec,
                   to_char(bl.api_last_update,'MM-DD HH24:MI:SS') AS api
            FROM mlb.betting_lines bl
            JOIN mlb.games g ON g.id = bl.game_id
            WHERE bl.recorded_at > g.date + interval '6 hours'
            ORDER BY bl.recorded_at DESC
        """))
        rows = r.fetchall()
        print("   COUNT:", len(rows))
        for x in rows[:60]:
            print("  ", tuple(x))
        if len(rows) > 60:
            print("   ...", len(rows) - 60, "more")

        print("=" * 78)
        print("3) mlb.betting_lines_old: rows with api_last_update AFTER game start:")
        r = conn.execute(text("""
            SELECT bl.game_id, to_char(g.date,'MM-DD HH24:MI') AS start,
                   bl.sportsbook, bl.is_opening,
                   to_char(bl.api_last_update,'MM-DD HH24:MI:SS') AS api,
                   to_char(bl.recorded_at,'MM-DD HH24:MI:SS') AS rec
            FROM mlb.betting_lines_old bl
            JOIN mlb.games g ON g.id = bl.game_id
            WHERE bl.api_last_update IS NOT NULL
              AND bl.api_last_update > g.date
            ORDER BY bl.api_last_update - g.date DESC
        """))
        rows = r.fetchall()
        print("   COUNT:", len(rows))
        for x in rows:
            print("  ", tuple(x))


if __name__ == "__main__":
    main()
