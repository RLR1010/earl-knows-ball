-- Migration: nfl.team_badweather_stats
-- Per-team situational performance in "bad weather" games (cold / precipitation),
-- computed LEAK-FREE (only games with date < the target game's date), mirroring
-- how nfl.team_rolling_stats is structured so data_loader joins it the same way.
--
-- Rows: one per (team, target_game) via feeds_into_game_id. The loader reads
-- bad-weather performance from PRIOR games only, so no lookahead leakage.
--
-- Condition definitions (consistent with nfl.player_splits):
--   cold : temperature < 40F (game-time)
--   precip: weather_condition matches rain|snow|drizzle|thunder|shower
CREATE TABLE IF NOT EXISTS nfl.team_badweather_stats (
    game_id          INTEGER NOT NULL,
    team_abbr        VARCHAR(3) NOT NULL,
    season           INTEGER NOT NULL,
    game_type        VARCHAR(10) NOT NULL DEFAULT 'REG',
    week             INTEGER NOT NULL,
    game_date        DATE,
    is_home          BOOLEAN,
    feeds_into_game_id INTEGER,

    -- COLD (<40F) prior games
    cold_games       INTEGER,
    cold_ppg         REAL,   -- points per game in cold prior games
    cold_ypg         REAL,   -- yards per game in cold prior games
    cold_win_pct     REAL,   -- win rate in cold prior games

    -- WARM (>=40F) prior games
    warm_games       INTEGER,
    warm_ppg         REAL,
    warm_ypg         REAL,
    warm_win_pct     REAL,

    -- PRECIPITATION (rain/snow/drizzle/thunder/shower) prior games
    precip_games     INTEGER,
    precip_ppg       REAL,
    precip_ypg       REAL,
    precip_win_pct   REAL,

    -- DRY (no precipitation) prior games
    dry_games        INTEGER,
    dry_ppg          REAL,
    dry_ypg          REAL,
    dry_win_pct      REAL,

    PRIMARY KEY (game_id, team_abbr)
);

CREATE INDEX IF NOT EXISTS idx_nfl_tbs_feeds_into_game_id
    ON nfl.team_badweather_stats (feeds_into_game_id);
CREATE INDEX IF NOT EXISTS idx_nfl_tbs_team_game
    ON nfl.team_badweather_stats (team_abbr, feeds_into_game_id);
