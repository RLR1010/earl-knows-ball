"""
Migration script: Create write-up system tables for MLB schema.

Creates:
  - mlb.game_writeups      — public + premium write-up content
  - mlb.team_splits         — team-level situational splits
  - mlb.bullpen_stats        — team-level bullpen aggregates
  - mlb.venues               — stadium/venue profiles

Idempotent — safe to run multiple times (CREATE TABLE IF NOT EXISTS).

The FK from games.venue_id → venues.id is added as NOT VALID to avoid
blocking on existing orphan venue_ids. Once venues are populated, run:
  ALTER TABLE mlb.games VALIDATE CONSTRAINT fk_mlb_games_venue;
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine, text
from app.core.config import settings

sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
engine = create_engine(sync_url, pool_pre_ping=True)


SQL = """

-- 1. Venues
CREATE TABLE IF NOT EXISTS mlb.venues (
    id SERIAL PRIMARY KEY,
    mlb_venue_id INTEGER UNIQUE,
    name VARCHAR(150) NOT NULL,
    city VARCHAR(100) NOT NULL,
    state VARCHAR(50),
    capacity INTEGER,
    surface VARCHAR(50),
    roof_type VARCHAR(20),
    left_field INTEGER,
    left_center INTEGER,
    center_field INTEGER,
    right_center INTEGER,
    right_field INTEGER,
    wall_height_left FLOAT,
    wall_height_center FLOAT,
    wall_height_right FLOAT,
    altitude INTEGER,
    park_factor_overall FLOAT,
    park_factor_home_runs FLOAT,
    description TEXT,
    year_opened INTEGER
);

-- 2. Team splits
CREATE TABLE IF NOT EXISTS mlb.team_splits (
    id SERIAL PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES mlb.teams(id) ON DELETE CASCADE,
    season_id INTEGER NOT NULL REFERENCES mlb.seasons(id) ON DELETE CASCADE,
    split_type VARCHAR(20) NOT NULL,
    games INTEGER NOT NULL DEFAULT 0,
    runs_scored INTEGER NOT NULL DEFAULT 0,
    runs_allowed INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    avg FLOAT,
    obp FLOAT,
    slg FLOAT,
    ops FLOAT,
    home_runs INTEGER NOT NULL DEFAULT 0,
    era FLOAT,
    whip FLOAT,
    UNIQUE (team_id, season_id, split_type)
);

-- 3. Bullpen stats
CREATE TABLE IF NOT EXISTS mlb.bullpen_stats (
    id SERIAL PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES mlb.teams(id) ON DELETE CASCADE,
    season_id INTEGER NOT NULL REFERENCES mlb.seasons(id) ON DELETE CASCADE,
    era FLOAT,
    whip FLOAT,
    fip FLOAT,
    innings_pitched FLOAT NOT NULL DEFAULT 0.0,
    strikeouts INTEGER NOT NULL DEFAULT 0,
    walks INTEGER NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    home_runs INTEGER NOT NULL DEFAULT 0,
    batters_faced INTEGER NOT NULL DEFAULT 0,
    saves INTEGER NOT NULL DEFAULT 0,
    blown_saves INTEGER NOT NULL DEFAULT 0,
    hold INTEGER NOT NULL DEFAULT 0,
    save_opportunities INTEGER NOT NULL DEFAULT 0,
    left_avg FLOAT,
    right_avg FLOAT,
    left_ops FLOAT,
    right_ops FLOAT,
    UNIQUE (team_id, season_id)
);

-- 4. Game write-ups
CREATE TABLE IF NOT EXISTS mlb.game_writeups (
    id SERIAL PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES mlb.games(id) ON DELETE CASCADE UNIQUE,
    title VARCHAR(300) NOT NULL,
    public_content TEXT NOT NULL DEFAULT '',
    premium_content TEXT NOT NULL DEFAULT '',
    research_brief JSONB,
    quality_checks JSONB,
    status VARCHAR(20) NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    is_historical BOOLEAN NOT NULL DEFAULT FALSE,
    historical_game_date TIMESTAMPTZ,
    generated_by VARCHAR(100),
    total_tokens INTEGER,
    published_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_mlb_writeup_status ON mlb.game_writeups(status);
CREATE INDEX IF NOT EXISTS idx_mlb_writeup_game_status ON mlb.game_writeups(game_id, status);
CREATE INDEX IF NOT EXISTS idx_mlb_splits_team ON mlb.team_splits(team_id);
CREATE INDEX IF NOT EXISTS idx_mlb_splits_season ON mlb.team_splits(season_id);
CREATE INDEX IF NOT EXISTS idx_mlb_bullpen_team ON mlb.bullpen_stats(team_id);
CREATE INDEX IF NOT EXISTS idx_mlb_venues_mlb_id ON mlb.venues(mlb_venue_id);

"""

FK_SQL = """
-- Add FK games.venue_id → venues.id, NOT VALID so existing orphan IDs don't block
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_mlb_games_venue'
        AND connamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'mlb')
    ) THEN
        EXECUTE 'ALTER TABLE mlb.games '
                'ADD CONSTRAINT fk_mlb_games_venue '
                'FOREIGN KEY (venue_id) REFERENCES mlb.venues(mlb_venue_id) NOT VALID';
    END IF;
END $$;
"""


def run():
    print("=== MLB Write-up System Migration ===")
    print("Connecting to database...")

    with engine.connect() as conn:
        print("Creating tables: venues, team_splits, bullpen_stats, game_writeups...")
        conn.execute(text(SQL))
        print("  ✓ Tables and indexes created (or already exist)")

        print("Adding FK constraint games.venue_id → venues.id (NOT VALID)...")
        conn.execute(text(FK_SQL))
        print("  ✓ FK constraint added (or already exists)")

        conn.commit()

    print("\n✅ Migration complete!")


if __name__ == "__main__":
    run()
