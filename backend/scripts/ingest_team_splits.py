"""
Script: populate mlb.team_splits with situational splits computed from games.

Computes these split types for every team-season:
  - home / away        — game location
  - day / night        — day_night column
  - grass / turf       — surface column (backfilled from venues)

We count wins, losses, runs_scored, runs_allowed per split.
Advanced batting/pitching rates (avg, obp, slg, era, whip) are left as NULL
since the DB lacks player-level split data.

Run this *after* venues are ingested (so surface is backfilled on games).

Idempotent — DELETE + re-INSERT for clean rebuild each time.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, text
from app.core.config import settings

sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
engine = create_engine(sync_url, pool_pre_ping=True)

STATUS_FINAL = "FINAL"
STATUS_IN_PROGRESS = "IN_PROGRESS"

# The split queries each return the same shape:
#   team_id, season_id, split_type, games, wins, losses, rs, ra

QUERIES: list[tuple[str, str]] = [
    (
        "home",
        """
        SELECT
            g.home_team_id AS team_id,
            g.season_id,
            'home' AS split_type,
            COUNT(*)               AS games,
            SUM(CASE WHEN g.home_score > g.away_score THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN g.home_score < g.away_score THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(g.home_score), 0) AS rs,
            COALESCE(SUM(g.away_score), 0) AS ra
        FROM mlb.games g
        WHERE g.status::text = :sfinal
          AND g.season_id = :sid
          AND g.home_score IS NOT NULL
          AND g.away_score IS NOT NULL
        GROUP BY g.home_team_id, g.season_id
        """,
    ),
    (
        "away",
        """
        SELECT
            g.away_team_id AS team_id,
            g.season_id,
            'away' AS split_type,
            COUNT(*)               AS games,
            SUM(CASE WHEN g.away_score > g.home_score THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN g.away_score < g.home_score THEN 1 ELSE 0 END) AS losses,
            COALESCE(SUM(g.away_score), 0) AS rs,
            COALESCE(SUM(g.home_score), 0) AS ra
        FROM mlb.games g
        WHERE g.status::text = :sfinal
          AND g.season_id = :sid
          AND g.home_score IS NOT NULL
          AND g.away_score IS NOT NULL
        GROUP BY g.away_team_id, g.season_id
        """,
    ),
    (
        "day",
        """
        SELECT
            CASE WHEN g.home_team_id = t.id THEN g.home_team_id ELSE g.away_team_id END AS team_id,
            g.season_id,
            'day' AS split_type,
            COUNT(*)               AS games,
            SUM(CASE WHEN g.home_team_id = t.id AND g.home_score > g.away_score THEN 1
                     WHEN g.away_team_id = t.id AND g.away_score > g.home_score THEN 1
                     ELSE 0 END)   AS wins,
            SUM(CASE WHEN g.home_team_id = t.id AND g.home_score < g.away_score THEN 1
                     WHEN g.away_team_id = t.id AND g.away_score < g.home_score THEN 1
                     ELSE 0 END)   AS losses,
            SUM(CASE WHEN g.home_team_id = t.id THEN g.home_score ELSE g.away_score END) AS rs,
            SUM(CASE WHEN g.home_team_id = t.id THEN g.away_score ELSE g.home_score END) AS ra
        FROM mlb.games g
        CROSS JOIN mlb.teams t
        WHERE g.status::text = :sfinal
          AND g.season_id = :sid
          AND g.home_score IS NOT NULL
          AND g.away_score IS NOT NULL
          AND g.day_night = 'day'
          AND (g.home_team_id = t.id OR g.away_team_id = t.id)
        GROUP BY team_id, g.season_id
        """,
    ),
    (
        "night",
        """
        SELECT
            CASE WHEN g.home_team_id = t.id THEN g.home_team_id ELSE g.away_team_id END AS team_id,
            g.season_id,
            'night' AS split_type,
            COUNT(*)               AS games,
            SUM(CASE WHEN g.home_team_id = t.id AND g.home_score > g.away_score THEN 1
                     WHEN g.away_team_id = t.id AND g.away_score > g.home_score THEN 1
                     ELSE 0 END)   AS wins,
            SUM(CASE WHEN g.home_team_id = t.id AND g.home_score < g.away_score THEN 1
                     WHEN g.away_team_id = t.id AND g.away_score < g.home_score THEN 1
                     ELSE 0 END)   AS losses,
            SUM(CASE WHEN g.home_team_id = t.id THEN g.home_score ELSE g.away_score END) AS rs,
            SUM(CASE WHEN g.home_team_id = t.id THEN g.away_score ELSE g.home_score END) AS ra
        FROM mlb.games g
        CROSS JOIN mlb.teams t
        WHERE g.status::text = :sfinal
          AND g.season_id = :sid
          AND g.home_score IS NOT NULL
          AND g.away_score IS NOT NULL
          AND g.day_night = 'night'
          AND (g.home_team_id = t.id OR g.away_team_id = t.id)
        GROUP BY team_id, g.season_id
        """,
    ),
    (
        "grass",
        """
        SELECT
            CASE WHEN g.home_team_id = t.id THEN g.home_team_id ELSE g.away_team_id END AS team_id,
            g.season_id,
            'grass' AS split_type,
            COUNT(*)               AS games,
            SUM(CASE WHEN g.home_team_id = t.id AND g.home_score > g.away_score THEN 1
                     WHEN g.away_team_id = t.id AND g.away_score > g.home_score THEN 1
                     ELSE 0 END)   AS wins,
            SUM(CASE WHEN g.home_team_id = t.id AND g.home_score < g.away_score THEN 1
                     WHEN g.away_team_id = t.id AND g.away_score < g.home_score THEN 1
                     ELSE 0 END)   AS losses,
            SUM(CASE WHEN g.home_team_id = t.id THEN g.home_score ELSE g.away_score END) AS rs,
            SUM(CASE WHEN g.home_team_id = t.id THEN g.away_score ELSE g.home_score END) AS ra
        FROM mlb.games g
        CROSS JOIN mlb.teams t
        WHERE g.status::text = :sfinal
          AND g.season_id = :sid
          AND g.home_score IS NOT NULL
          AND g.away_score IS NOT NULL
          AND g.surface ILIKE 'grass'
          AND (g.home_team_id = t.id OR g.away_team_id = t.id)
        GROUP BY team_id, g.season_id
        """,
    ),
    (
        "turf",
        """
        SELECT
            CASE WHEN g.home_team_id = t.id THEN g.home_team_id ELSE g.away_team_id END AS team_id,
            g.season_id,
            'turf' AS split_type,
            COUNT(*)               AS games,
            SUM(CASE WHEN g.home_team_id = t.id AND g.home_score > g.away_score THEN 1
                     WHEN g.away_team_id = t.id AND g.away_score > g.home_score THEN 1
                     ELSE 0 END)   AS wins,
            SUM(CASE WHEN g.home_team_id = t.id AND g.home_score < g.away_score THEN 1
                     WHEN g.away_team_id = t.id AND g.away_score < g.home_score THEN 1
                     ELSE 0 END)   AS losses,
            SUM(CASE WHEN g.home_team_id = t.id THEN g.home_score ELSE g.away_score END) AS rs,
            SUM(CASE WHEN g.home_team_id = t.id THEN g.away_score ELSE g.home_score END) AS ra
        FROM mlb.games g
        CROSS JOIN mlb.teams t
        WHERE g.status::text = :sfinal
          AND g.season_id = :sid
          AND g.home_score IS NOT NULL
          AND g.away_score IS NOT NULL
          AND g.surface ILIKE 'astroturf'
          AND (g.home_team_id = t.id OR g.away_team_id = t.id)
        GROUP BY team_id, g.season_id
        """,
    ),
]

INSERT_SQL = """
INSERT INTO mlb.team_splits
    (team_id, season_id, split_type,
     games, wins, losses, runs_scored, runs_allowed)
VALUES
    (:team_id, :season_id, :split_type,
     :games, :wins, :losses, :runs_scored, :runs_allowed)
ON CONFLICT (team_id, season_id, split_type)
DO UPDATE SET
    games        = EXCLUDED.games,
    wins         = EXCLUDED.wins,
    losses       = EXCLUDED.losses,
    runs_scored  = EXCLUDED.runs_scored,
    runs_allowed = EXCLUDED.runs_allowed
"""


def get_season_ids(conn) -> list[int]:
    """Return all MLB season IDs."""
    return [
        r[0]
        for r in conn.execute(text("SELECT id FROM mlb.seasons ORDER BY id")).fetchall()
    ]


def run():
    print("=== MLB Team Splits Population ===")

    with engine.connect() as conn:
        season_ids = get_season_ids(conn)
        print(f"Seasons to process: {season_ids}")

        total_rows = 0
        for sid in season_ids:
            for split_name, sql in QUERIES:
                rows = conn.execute(
                    text(sql), {"sfinal": STATUS_FINAL, "sid": sid}
                ).mappings().fetchall()

                for r in rows:
                    conn.execute(
                        text(INSERT_SQL),
                        {
                            "team_id": r["team_id"],
                            "season_id": r["season_id"],
                            "split_type": r["split_type"],
                            "games": r["games"],
                            "wins": r["wins"],
                            "losses": r["losses"],
                            "runs_scored": r["rs"],
                            "runs_allowed": r["ra"],
                        },
                    )
                    total_rows += 1

                print(f"  {split_name:6s} season={sid}: {len(rows)} rows")

        conn.commit()
        print(f"\n✅ {total_rows} split rows inserted/updated")

        # Verify
        count = conn.execute(
            text("SELECT COUNT(*) FROM mlb.team_splits")
        ).scalar()
        distinct_teams = conn.execute(
            text("SELECT COUNT(DISTINCT team_id) FROM mlb.team_splits")
        ).scalar()
        distinct_types = conn.execute(
            text("SELECT array_agg(DISTINCT split_type ORDER BY split_type) FROM mlb.team_splits")
        ).scalar()

        print(f"  Total rows: {count}")
        print(f"  Teams:      {distinct_teams}")
        print(f"  Types:      {distinct_types}")


if __name__ == "__main__":
    run()
