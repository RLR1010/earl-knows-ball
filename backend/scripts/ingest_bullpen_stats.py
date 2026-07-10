"""
Script: populate mlb.bullpen_stats from pitching_stats filtered to relievers.

Identifies relievers as pitchers with games_started = 0
in the mlb.pitching_stats table, then aggregates per team per season.

Idempotent — re-runnable.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, text
from app.core.config import settings

sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
engine = create_engine(sync_url, pool_pre_ping=True)

AGG_SQL = """
INSERT INTO mlb.bullpen_stats (
    team_id, season_id,
    era, whip,
    innings_pitched, strikeouts, walks, hits, home_runs,
    batters_faced,
    saves, blown_saves, hold, save_opportunities,
    left_avg, right_avg, left_ops, right_ops
)
WITH relievers AS (
    SELECT *
    FROM mlb.pitching_stats ps
    WHERE ps.games_started = 0
      AND ps.innings_pitched > 0
      AND ps.season_id = :sid
)
SELECT
    ps.team_id,
    ps.season_id,

    -- ERA = 9 * earned_runs / innings_pitched
    ROUND(
        9.0 * SUM(ps.earned_runs) / NULLIF(SUM(ps.innings_pitched), 0)::numeric,
        2
    )::float AS era,

    -- WHIP = (walks + hits) / innings_pitched
    ROUND(
        (SUM(ps.base_on_balls) + SUM(ps.hits))::numeric
        / NULLIF(SUM(ps.innings_pitched), 0)::numeric,
        3
    )::float AS whip,

    SUM(ps.innings_pitched) AS innings_pitched,
    SUM(ps.strikeouts) AS strikeouts,
    SUM(ps.base_on_balls) AS walks,
    SUM(ps.hits) AS hits,
    SUM(ps.home_runs) AS home_runs,

    SUM(ps.batters_faced) AS batters_faced,

    SUM(ps.saves) AS saves,
    SUM(ps.blown_saves) AS blown_saves,
    SUM(ps.holds) AS hold,
    SUM(ps.save_opportunities) AS save_opportunities,

    -- Platoon splits not available in aggregate source
    NULL::float AS left_avg,
    NULL::float AS right_avg,
    NULL::float AS left_ops,
    NULL::float AS right_ops

FROM relievers ps
GROUP BY ps.team_id, ps.season_id
ON CONFLICT (team_id, season_id)
DO UPDATE SET
    era               = EXCLUDED.era,
    whip              = EXCLUDED.whip,
    innings_pitched   = EXCLUDED.innings_pitched,
    strikeouts        = EXCLUDED.strikeouts,
    walks             = EXCLUDED.walks,
    hits              = EXCLUDED.hits,
    home_runs         = EXCLUDED.home_runs,
    batters_faced     = EXCLUDED.batters_faced,
    saves             = EXCLUDED.saves,
    blown_saves       = EXCLUDED.blown_saves,
    hold              = EXCLUDED.hold,
    save_opportunities = EXCLUDED.save_opportunities
"""


def run():
    print("=== MLB Bullpen Stats Population ===")

    with engine.connect() as conn:
        # Get all season IDs
        sids = [
            r[0]
            for r in conn.execute(text("SELECT id FROM mlb.seasons ORDER BY id")).fetchall()
        ]
        print(f"Seasons to process: {sids}")

        total = 0
        for sid in sids:
            result = conn.execute(text(AGG_SQL), {"sid": sid})
            rows = result.rowcount
            total += rows
            print(f"  Season {sid}: {rows} teams")

        conn.commit()

    print(f"\n✅ {total} bullpen rows inserted/updated")

    # Verify
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM mlb.bullpen_stats")
        ).scalar()
        teams = conn.execute(
            text("SELECT COUNT(DISTINCT team_id) FROM mlb.bullpen_stats")
        ).scalar()
        seasons = conn.execute(
            text("SELECT array_agg(DISTINCT season_id ORDER BY season_id) FROM mlb.bullpen_stats")
        ).scalar()
        sample = conn.execute(
            text("""
                SELECT t.abbreviation, bs.season_id, bs.era, bs.whip,
                       bs.saves, bs.strikeouts, bs.innings_pitched
                FROM mlb.bullpen_stats bs
                JOIN mlb.teams t ON t.id = bs.team_id
                WHERE bs.season_id = 21
                ORDER BY bs.era ASC
                LIMIT 5
            """)
        ).fetchall()
        print(f"\n  Total rows: {count}")
        print(f"  Teams:      {teams}")
        print(f"  Seasons:    {seasons}")
        print(f"\n  Top 5 bullpens (Season 21, by ERA):")
        for row in sample:
            print(f"    {row.abbreviation:4s} ERA={row.era:.2f} WHIP={row.whip:.3f} "
                  f"SV={row.saves} K={row.strikeouts} IP={row.innings_pitched:.0f}")


if __name__ == "__main__":
    run()
