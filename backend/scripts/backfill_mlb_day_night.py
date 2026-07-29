"""
Backfill day_night for MLB games where it's NULL.

Uses the game's UTC timestamp, adjusted to US timezones,
to classify as 'day' or 'night': before 5 PM local = day, else night.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from app.core.config import settings


# Hours considered "night" (local start time >= this hour)
NIGHT_HOUR = 17  # 5 PM


def classify_day_night(hour: int) -> str | None:
    """Classify game start hour (local) as 'day' or 'night'."""
    if hour is None:
        return None
    return "night" if hour >= NIGHT_HOUR else "day"


def main():
    db_url = str(settings.database_url).replace("+asyncpg", "")
    engine = create_engine(db_url)

    with engine.connect() as conn:
        # Which seasons have NULL day_night?
        null_seasons = conn.execute(
            text("""
                SELECT season_id, COUNT(*)
                FROM mlb.games
                WHERE day_night IS NULL
                GROUP BY season_id
                ORDER BY season_id
            """)
        ).fetchall()

        if not null_seasons:
            print("No games with NULL day_night — nothing to backfill.")
            return

        print("Seasons with NULL day_night:")
        for sid, cnt in null_seasons:
            print(f"  Season {sid}: {cnt} games")

        # Update games. We approximate local time using team timezones.
        # For simplicity, use America/New_York (ET) as proxy for most teams,
        # but handle the handful that differ: Texas, Arizona, Seattle, etc.
        tz_overrides = {
            "TEX": "America/Chicago",
            "HOU": "America/Chicago",
            "CHC": "America/Chicago",
            "CWS": "America/Chicago",
            "MIL": "America/Chicago",
            "MIN": "America/Chicago",
            "KC":  "America/Chicago",
            "STL": "America/Chicago",
            "ARI": "America/Phoenix",
            "COL": "America/Denver",
            "LAD": "America/Los_Angeles",
            "LAA": "America/Los_Angeles",
            "OAK": "America/Los_Angeles",
            "SD":  "America/Los_Angeles",
            "SF":  "America/Los_Angeles",
            "SEA": "America/Los_Angeles",
        }
        default_tz = "America/New_York"

        # Update using home team as best guess for local time
        updated = conn.execute(
            text("""
                WITH tz_map AS (
                    SELECT abbreviation, tz
                    FROM (VALUES
                        ('TEX', 'America/Chicago'),
                        ('HOU', 'America/Chicago'),
                        ('CHC', 'America/Chicago'),
                        ('CWS', 'America/Chicago'),
                        ('MIL', 'America/Chicago'),
                        ('MIN', 'America/Chicago'),
                        ('KC',  'America/Chicago'),
                        ('STL', 'America/Chicago'),
                        ('ARI', 'America/Phoenix'),
                        ('COL', 'America/Denver'),
                        ('LAD', 'America/Los_Angeles'),
                        ('LAA', 'America/Los_Angeles'),
                        ('OAK', 'America/Los_Angeles'),
                        ('SD',  'America/Los_Angeles'),
                        ('SF',  'America/Los_Angeles'),
                        ('SEA', 'America/Los_Angeles')
                    ) AS t(abbreviation, tz)
                )
                UPDATE mlb.games g
                SET day_night = CASE
                    WHEN EXTRACT(HOUR FROM g.date AT TIME ZONE COALESCE(tz_map.tz, 'America/New_York')) >= 17
                    THEN 'night' ELSE 'day'
                END
                FROM mlb.teams t
                LEFT JOIN tz_map ON t.abbreviation = tz_map.abbreviation
                WHERE g.home_team_id = t.id
                  AND g.day_night IS NULL
                  AND g.date IS NOT NULL
                RETURNING g.id, g.day_night, g.date
            """)
        ).fetchall()

        count = len(updated)
        print(f"\nBackfilled {count} games:")
        for gid, dn, dt in updated[:10]:
            print(f"  Game {gid}: {dt} -> {dn}")
        if count > 10:
            print(f"  ... and {count - 10} more")

        conn.commit()

    engine.dispose()
    print("\nDone. Run populate_rolling_stats.py next to populate day_era_ytd/night_era_ytd.")


if __name__ == "__main__":
    main()
