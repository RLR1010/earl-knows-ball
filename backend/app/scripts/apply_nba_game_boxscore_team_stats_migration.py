"""Apply the nba.games team box-score stat columns migration.

Adds the full set of game box-score team stats supplied by the ESPN core API
team-statistics endpoint (real offensive/defensive rebounds, estimated
possessions, points in paint, fast-break points, turnovers split, lead/flow
stats, fouling detail, double/triple doubles, advanced ratios, NBARating, VORP,
etc.) to nba.games. See migrations/20260816_nba_game_boxscore_team_stats.sql.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260816_nba_game_boxscore_team_stats.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.exec_driver_sql(sql_file.read_text())

    # Verify the new columns landed
    new_cols = [
        "home_offensive_rebounds", "away_offensive_rebounds",
        "home_defensive_rebounds", "away_defensive_rebounds",
        "home_estimated_possessions", "away_estimated_possessions",
        "home_points_in_paint", "away_points_in_paint",
        "home_fast_break_points", "away_fast_break_points",
        "home_turnover_points", "away_turnover_points",
        "home_team_turnovers", "away_team_turnovers",
        "home_total_turnovers", "away_total_turnovers",
        "home_nba_rating", "away_nba_rating",
        "home_vorp", "away_vorp",
        "home_lead_changes", "away_lead_changes",
        "home_largest_lead", "away_largest_lead",
        "home_double_double", "away_double_double",
        "home_triple_double", "away_triple_double",
    ]
    with engine.connect() as conn:
        cols = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='nba' AND table_name='games'"
            )
        ).fetchall()
        colset = {r[0] for r in cols}
    missing = [c for c in new_cols if c not in colset]
    if missing:
        print("MISSING columns:", missing)
    else:
        print(f"All {len(new_cols)} checked new columns present.")
    print("Done.")


if __name__ == "__main__":
    main()
