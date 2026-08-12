-- Migration: nfl.qb_badweather_stats
-- Per-QB situational passer rating in "bad weather" games (cold / precipitation),
-- computed LEAK-FREE (prior games only) so it's safe to train on.
--
-- One row per (player, target_game) via feeds_into_game_id; the loader reads the
-- QB's cold/precip passer rating from PRIOR games for that game's resolved starter.
--
-- Condition definitions (consistent with nfl.player_splits / nfl.team_badweather_stats):
--   cold : temperature < 40F (game-time)
--   precip: weather_condition matches rain|snow|drizzle|thunder|shower
CREATE TABLE IF NOT EXISTS nfl.qb_badweather_stats (
    player_id        INTEGER NOT NULL,
    game_id          INTEGER NOT NULL,
    season           INTEGER NOT NULL,
    game_type        VARCHAR(10) NOT NULL DEFAULT 'REG',
    week             INTEGER NOT NULL,
    game_date        DATE,
    team_abbr        VARCHAR(3),
    feeds_into_game_id INTEGER,

    -- COLD (<40F) prior starts
    cold_starts      INTEGER,
    cold_passer_rating REAL,   -- NFL passer rating in cold games

    -- PRECIPITATION (rain/snow/drizzle/thunder/shower) prior starts
    precip_starts    INTEGER,
    precip_passer_rating REAL,

    PRIMARY KEY (player_id, game_id)
);

CREATE INDEX IF NOT EXISTS idx_nfl_qbs_feeds_into_game_id
    ON nfl.qb_badweather_stats (feeds_into_game_id);
CREATE INDEX IF NOT EXISTS idx_nfl_qbs_player_game
    ON nfl.qb_badweather_stats (player_id, feeds_into_game_id);
