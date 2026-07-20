-- Migration: Redesign player_daily_props to support tiered threshold props
-- Date: 2026-07-20
--
-- FD player props come in two flavors:
--   1. Standard O/U: "Over 0.5 @ -125" / "Under 0.5 @ +105"
--   2. Tiered: "2+ Hits @ +175" / "3+ Hits @ +210"
--
-- New schema: one row per runner with single odds column + direction flag.

-- ==========================================
-- MLB
-- ==========================================
DROP TABLE IF EXISTS mlb.player_daily_props CASCADE;
CREATE TABLE mlb.player_daily_props (
    id SERIAL PRIMARY KEY,
    game_id VARCHAR(50) NOT NULL,
    player_name VARCHAR(255) NOT NULL,
    team_id INTEGER REFERENCES mlb.teams(id) ON DELETE SET NULL,
    prop_type VARCHAR(100) NOT NULL,    -- hit, home_run, strikeout, point, etc.
    line NUMERIC(6,2),                  -- threshold (0.5 for O/U, 2 for 2+ Hits)
    odds INTEGER,                       -- American odds
    direction VARCHAR(10) NOT NULL,     -- 'over' | 'under' | 'tiered'
    bookmaker VARCHAR(50) NOT NULL,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(game_id, player_name, prop_type, direction, line, bookmaker)
);
CREATE INDEX IF NOT EXISTS idx_mlb_player_daily_props_game ON mlb.player_daily_props (game_id);
CREATE INDEX IF NOT EXISTS idx_mlb_player_daily_props_player ON mlb.player_daily_props (player_name);

-- ==========================================
-- NFL
-- ==========================================
DROP TABLE IF EXISTS nfl.player_daily_props CASCADE;
CREATE TABLE nfl.player_daily_props (
    id SERIAL PRIMARY KEY,
    game_id VARCHAR(50) NOT NULL,
    player_name VARCHAR(255) NOT NULL,
    team_id INTEGER REFERENCES nfl.teams(id) ON DELETE SET NULL,
    prop_type VARCHAR(100) NOT NULL,
    line NUMERIC(6,2),
    odds INTEGER,
    direction VARCHAR(10) NOT NULL,
    bookmaker VARCHAR(50) NOT NULL,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(game_id, player_name, prop_type, direction, line, bookmaker)
);
CREATE INDEX IF NOT EXISTS idx_nfl_player_daily_props_game ON nfl.player_daily_props (game_id);
CREATE INDEX IF NOT EXISTS idx_nfl_player_daily_props_player ON nfl.player_daily_props (player_name);

-- ==========================================
-- NBA
-- ==========================================
DROP TABLE IF EXISTS nba.player_daily_props CASCADE;
CREATE TABLE nba.player_daily_props (
    id SERIAL PRIMARY KEY,
    game_id VARCHAR(50) NOT NULL,
    player_name VARCHAR(255) NOT NULL,
    team_id INTEGER REFERENCES nba.teams(id) ON DELETE SET NULL,
    prop_type VARCHAR(100) NOT NULL,
    line NUMERIC(6,2),
    odds INTEGER,
    direction VARCHAR(10) NOT NULL,
    bookmaker VARCHAR(50) NOT NULL,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(game_id, player_name, prop_type, direction, line, bookmaker)
);
CREATE INDEX IF NOT EXISTS idx_nba_player_daily_props_game ON nba.player_daily_props (game_id);
CREATE INDEX IF NOT EXISTS idx_nba_player_daily_props_player ON nba.player_daily_props (player_name);
