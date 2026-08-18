"""Reconcile the 7/20 backfill and check for post-start api_last_update among backfilled rows."""
from sqlalchemy import create_engine, text

ENGINE = create_engine("postgresql+psycopg2://earl:earl_dev_pass@localhost:5432/earl_knows_football")


def main():
    with ENGINE.connect() as conn:
        print("1) Distinct recorded_at batch timestamps (top 12 by volume) in live table:")
        for r in conn.execute(text("""
            SELECT date_trunc('hour', recorded_at) AS batch, count(*)
            FROM mlb.betting_lines
            GROUP BY 1 ORDER BY 2 DESC LIMIT 12
        """)):
            print("   ", r[0], r[1])

        print()
        print("2) The 7/20 backfill: how many rows, and how many have api_last_update AFTER game start?")
        r = conn.execute(text("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE bl.api_last_update > g.date) AS post_start_api
            FROM mlb.betting_lines bl
            JOIN mlb.games g ON g.id = bl.game_id
            WHERE bl.recorded_at >= '2026-07-20' AND bl.recorded_at < '2026-07-21'
        """))
        row = r.fetchone()
        print("   total written by 7/20 batch:", row[0], "| post-start api_last_update:", row[1])

        print()
        print("3) Same for the 6/29 recorded_at batch (the 48245 oddity):")
        r = conn.execute(text("""
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE bl.api_last_update > g.date) AS post_start_api
            FROM mlb.betting_lines bl
            JOIN mlb.games g ON g.id = bl.game_id
            WHERE bl.recorded_at >= '2026-06-29' AND bl.recorded_at < '2026-06-30'
        """))
        row = r.fetchone()
        print("   total:", row[0], "| post-start api_last_update:", row[1])

        print()
        print("4) The 3 genuinely-post-start rows detail (api_last_update > game start + 60s):")
        for r in conn.execute(text("""
            SELECT bl.game_id, to_char(g.date,'MM-DD HH24:MI:SS') start,
                   bl.sportsbook, bl.is_opening, bl.spread, bl.over_under, bl.home_moneyline,
                   to_char(bl.api_last_update,'MM-DD HH24:MI:SS') api,
                   to_char(bl.recorded_at,'MM-DD HH24:MI:SS') rec,
                   round(extract(epoch from (bl.api_last_update - g.date))/60)::int min_after
            FROM mlb.betting_lines bl JOIN mlb.games g ON g.id=bl.game_id
            WHERE bl.api_last_update IS NOT NULL
              AND bl.api_last_update > g.date + interval '60 seconds'
            ORDER BY bl.api_last_update - g.date DESC
        """)):
            print("   ", tuple(r))


if __name__ == "__main__":
    main()
