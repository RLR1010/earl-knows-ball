-- Migration: MLB feature query optimization
-- 1. Add missing indexes for subquery performance
-- 2. Add pre-computed columns to team_rolling_stats

-- ============================================================
-- PART 1: Missing Indexes
-- ============================================================

-- Partial indexes for the 3 correlated subqueries
CREATE INDEX IF NOT EXISTS ix_mlb_games_home_season_date_final
    ON mlb.games (home_team_id, season_id, date)
    WHERE status = 'FINAL';

CREATE INDEX IF NOT EXISTS ix_mlb_games_away_season_date_final
    ON mlb.games (away_team_id, season_id, date)
    WHERE status = 'FINAL';

CREATE INDEX IF NOT EXISTS ix_mlb_games_venue_away_date_final
    ON mlb.games (venue_id, away_team_id, date)
    WHERE status = 'FINAL';

-- Composite index for pitcher_game_stats 3-column JOIN
CREATE INDEX IF NOT EXISTS ix_mlb_pgs_game_team_starter
    ON mlb.pitcher_game_stats (game_id, team_abbr, is_starter);

-- Composite index for pitcher_rolling_stats partial match
CREATE INDEX IF NOT EXISTS ix_mlb_prs_game_team_starter
    ON mlb.pitcher_rolling_stats (game_id, team_abbr)
    WHERE is_starter = TRUE;

-- ============================================================
-- PART 2: New columns on team_rolling_stats for pre-computed values
-- ============================================================

ALTER TABLE mlb.team_rolling_stats
    ADD COLUMN IF NOT EXISTS home_games_sofar INTEGER,
    ADD COLUMN IF NOT EXISTS away_games_sofar INTEGER,
    ADD COLUMN IF NOT EXISTS game_away_venue_pct DOUBLE PRECISION;


