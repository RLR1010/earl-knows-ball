-- Migration: Add scraper tables for prop bets and futures
-- Date: 2026-07-20
-- Creates per-schema tables for: team_props, player_season_props, player_daily_props

-- ==========================================
-- MLB — team_props
-- ==========================================
CREATE TABLE IF NOT EXISTS mlb.team_props (
    id SERIAL PRIMARY KEY,
    season_year INTEGER NOT NULL,
    team_id INTEGER REFERENCES mlb.teams(id) ON DELETE CASCADE,
    bookmaker VARCHAR(50) NOT NULL,
    championship_odds INTEGER,
    make_playoffs_odds INTEGER,
    miss_playoffs_odds INTEGER,
    win_total NUMERIC(4,1),
    win_total_over_odds INTEGER,
    win_total_under_odds INTEGER,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(season_year, team_id, bookmaker)
);

-- MLB — player_season_props
CREATE TABLE IF NOT EXISTS mlb.player_season_props (
    id SERIAL PRIMARY KEY,
    season_year INTEGER NOT NULL,
    player_name VARCHAR(255) NOT NULL,
    team_id INTEGER REFERENCES mlb.teams(id) ON DELETE SET NULL,
    prop_type VARCHAR(50) NOT NULL,
    bookmaker VARCHAR(50) NOT NULL,
    odds INTEGER,
    implied_probability NUMERIC(5,4),
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(season_year, player_name, prop_type, bookmaker)
);

-- MLB — player_daily_props
CREATE TABLE IF NOT EXISTS mlb.player_daily_props (
    id SERIAL PRIMARY KEY,
    game_id VARCHAR(50) NOT NULL,
    player_name VARCHAR(255) NOT NULL,
    team_id INTEGER REFERENCES mlb.teams(id) ON DELETE SET NULL,
    prop_type VARCHAR(100) NOT NULL,
    bookmaker VARCHAR(50) NOT NULL,
    line NUMERIC(6,2),
    over_odds INTEGER,
    under_odds INTEGER,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(game_id, player_name, prop_type, bookmaker)
);

-- MLB indexes
CREATE INDEX IF NOT EXISTS idx_mlb_team_props_season ON mlb.team_props (season_year);
CREATE INDEX IF NOT EXISTS idx_mlb_team_props_team ON mlb.team_props (team_id);
CREATE INDEX IF NOT EXISTS idx_mlb_player_season_props_type ON mlb.player_season_props (prop_type, season_year);
CREATE INDEX IF NOT EXISTS idx_mlb_player_daily_props_game ON mlb.player_daily_props (game_id);

-- ==========================================
-- NFL — team_props
-- ==========================================
CREATE TABLE IF NOT EXISTS nfl.team_props (
    id SERIAL PRIMARY KEY,
    season_year INTEGER NOT NULL,
    team_id INTEGER REFERENCES nfl.teams(id) ON DELETE CASCADE,
    bookmaker VARCHAR(50) NOT NULL,
    championship_odds INTEGER,
    make_playoffs_odds INTEGER,
    miss_playoffs_odds INTEGER,
    win_total NUMERIC(4,1),
    win_total_over_odds INTEGER,
    win_total_under_odds INTEGER,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(season_year, team_id, bookmaker)
);

-- NFL — player_season_props
CREATE TABLE IF NOT EXISTS nfl.player_season_props (
    id SERIAL PRIMARY KEY,
    season_year INTEGER NOT NULL,
    player_name VARCHAR(255) NOT NULL,
    team_id INTEGER REFERENCES nfl.teams(id) ON DELETE SET NULL,
    prop_type VARCHAR(50) NOT NULL,
    bookmaker VARCHAR(50) NOT NULL,
    odds INTEGER,
    implied_probability NUMERIC(5,4),
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(season_year, player_name, prop_type, bookmaker)
);

-- NFL — player_daily_props
CREATE TABLE IF NOT EXISTS nfl.player_daily_props (
    id SERIAL PRIMARY KEY,
    game_id VARCHAR(50) NOT NULL,
    player_name VARCHAR(255) NOT NULL,
    team_id INTEGER REFERENCES nfl.teams(id) ON DELETE SET NULL,
    prop_type VARCHAR(100) NOT NULL,
    bookmaker VARCHAR(50) NOT NULL,
    line NUMERIC(6,2),
    over_odds INTEGER,
    under_odds INTEGER,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(game_id, player_name, prop_type, bookmaker)
);

-- NFL indexes
CREATE INDEX IF NOT EXISTS idx_nfl_team_props_season ON nfl.team_props (season_year);
CREATE INDEX IF NOT EXISTS idx_nfl_team_props_team ON nfl.team_props (team_id);
CREATE INDEX IF NOT EXISTS idx_nfl_player_season_props_type ON nfl.player_season_props (prop_type, season_year);
CREATE INDEX IF NOT EXISTS idx_nfl_player_daily_props_game ON nfl.player_daily_props (game_id);

-- ==========================================
-- NBA — team_props
-- ==========================================
CREATE TABLE IF NOT EXISTS nba.team_props (
    id SERIAL PRIMARY KEY,
    season_year INTEGER NOT NULL,
    team_id INTEGER REFERENCES nba.teams(id) ON DELETE CASCADE,
    bookmaker VARCHAR(50) NOT NULL,
    championship_odds INTEGER,
    make_playoffs_odds INTEGER,
    miss_playoffs_odds INTEGER,
    win_total NUMERIC(4,1),
    win_total_over_odds INTEGER,
    win_total_under_odds INTEGER,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(season_year, team_id, bookmaker)
);

-- NBA — player_season_props
CREATE TABLE IF NOT EXISTS nba.player_season_props (
    id SERIAL PRIMARY KEY,
    season_year INTEGER NOT NULL,
    player_name VARCHAR(255) NOT NULL,
    team_id INTEGER REFERENCES nba.teams(id) ON DELETE SET NULL,
    prop_type VARCHAR(50) NOT NULL,
    bookmaker VARCHAR(50) NOT NULL,
    odds INTEGER,
    implied_probability NUMERIC(5,4),
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(season_year, player_name, prop_type, bookmaker)
);

-- NBA — player_daily_props
CREATE TABLE IF NOT EXISTS nba.player_daily_props (
    id SERIAL PRIMARY KEY,
    game_id VARCHAR(50) NOT NULL,
    player_name VARCHAR(255) NOT NULL,
    team_id INTEGER REFERENCES nba.teams(id) ON DELETE SET NULL,
    prop_type VARCHAR(100) NOT NULL,
    bookmaker VARCHAR(50) NOT NULL,
    line NUMERIC(6,2),
    over_odds INTEGER,
    under_odds INTEGER,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(game_id, player_name, prop_type, bookmaker)
);

-- NBA indexes
CREATE INDEX IF NOT EXISTS idx_nba_team_props_season ON nba.team_props (season_year);
CREATE INDEX IF NOT EXISTS idx_nba_team_props_team ON nba.team_props (team_id);
CREATE INDEX IF NOT EXISTS idx_nba_player_season_props_type ON nba.player_season_props (prop_type, season_year);
CREATE INDEX IF NOT EXISTS idx_nba_player_daily_props_game ON nba.player_daily_props (game_id);
