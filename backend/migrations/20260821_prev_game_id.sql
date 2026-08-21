-- 2026-08-21 prev_game_id / prev_game_date columns
-- Add "prior game" pointer columns to the set-based MLB rolling snapshot tables so the
-- data loader can resolve "the entity's previous game" with indexed equality lookups
-- instead of correlated `ORDER BY ... LIMIT 1` date scans (the 8-20 min bottleneck).
--
-- Conventions (match each table's CURRENT ROW semantics: every row includes its own
-- game's stats; the PRIOR game's row is what the loader must read for "as of prior game").
--
--   prev_game_id           -> immediately prior game the entity appeared in, ACROSS seasons
--   prev_game_date         -> that game's date (denormalized, so rest/travel reads no join)
--   prev_game_id_season    -> immediately prior game WITHIN the same season (NULL on first
--                             game of a season)
--   prev_game_date_season  -> that game's date
--
-- Only set-based tables (batting, team, pitcher) get pointers now. cumulative_game_stats is
-- incremental and deferred (Option 1).
--
-- NOTE: columns are added as nullable (prior-game of the first appearance is NULL). Populated
-- by the builders' LAG() windows, which are idempotent + rebuilt on refresh.

ALTER TABLE mlb.player_batting_rolling_stats
    ADD COLUMN IF NOT EXISTS prev_game_id INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date DATE,
    ADD COLUMN IF NOT EXISTS prev_game_id_season INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date_season DATE;

ALTER TABLE mlb.team_rolling_stats
    ADD COLUMN IF NOT EXISTS prev_game_id INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date DATE,
    ADD COLUMN IF NOT EXISTS prev_game_id_season INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date_season DATE,

    -- venue-split pointer: prior game at the SAME team_side (venue label)
    ADD COLUMN IF NOT EXISTS prev_game_id_side INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date_side DATE;

ALTER TABLE mlb.pitcher_rolling_stats
    ADD COLUMN IF NOT EXISTS prev_game_id INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date DATE,
    ADD COLUMN IF NOT EXISTS prev_game_id_season INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date_season DATE;

-- Indexes to make the pointer lookups fast (player/team -> prior row by game_id).
-- The loader joins snapshot.prev_game_id -> snapshot.game_id, so a plain (game_id) index
-- (already present as idx_pbrs_game / its equivalents) suffices; add composite lookups
-- for the entity+game_id pattern used by lineup resolution.
CREATE INDEX IF NOT EXISTS idx_pbrs_pg ON mlb.player_batting_rolling_stats (prev_game_id);
CREATE INDEX IF NOT EXISTS idx_pbrs_pg_season ON mlb.player_batting_rolling_stats (prev_game_id_season);

CREATE INDEX IF NOT EXISTS idx_trs_pg ON mlb.team_rolling_stats (prev_game_id);
CREATE INDEX IF NOT EXISTS idx_trs_pg_season ON mlb.team_rolling_stats (prev_game_id_season);
CREATE INDEX IF NOT EXISTS idx_trs_pg_side ON mlb.team_rolling_stats (prev_game_id_side);

CREATE INDEX IF NOT EXISTS idx_prs_pg ON mlb.pitcher_rolling_stats (prev_game_id);
CREATE INDEX IF NOT EXISTS idx_prs_pg_season ON mlb.pitcher_rolling_stats (prev_game_id_season);
