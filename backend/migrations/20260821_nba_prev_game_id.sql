-- 2026-08-21 NBA prev_game_id / prev_game_date columns
-- Apply the same efficiency pattern as MLB's 20260821_prev_game_id.sql to the NBA
-- rolling + cumulative snapshot tables so the NBA data loader can resolve "the team's
-- previous game" with indexed equality lookups (prev_game_id -> game_id) instead of
-- correlated `ORDER BY ... LIMIT 1` date scans (the slow pattern currently in the
-- 'hcs/hrs/acs/ars/hrs_hv/ars_av/cgs_hv/cgs_av' LATERALs).
--
-- Conventions (match CURRENT ROW semantics: every row includes its own game's stats;
-- the PRIOR game's row is what the loader reads for "as of prior game / rest days"):
--
--   prev_game_id           -> immediately prior game the team played, ACROSS seasons
--   prev_game_date         -> that game's date (denormalized for rest/travel reads)
--   prev_game_id_season    -> immediately prior game WITHIN the same season
--                             (NULL on the first game of a season)
--   prev_game_date_season  -> that game's date
--
-- Both tables are UNIQUE on (team_id, game_id), so prev_game_id resolves to exactly one
-- prior row. Populated by the builders' LAG() windows (idempotent + rebuilt on refresh).
-- Columns added as nullable (prior game of the first appearance is NULL).

-- team_rolling_stats (set-based, THE snapshot for rolling/venue features)
ALTER TABLE nba.team_rolling_stats
    ADD COLUMN IF NOT EXISTS prev_game_id INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date DATE,
    ADD COLUMN IF NOT EXISTS prev_game_id_season INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date_season DATE,

    -- venue-split pointer: prior game at the SAME team_side (venue label)
    ADD COLUMN IF NOT EXISTS prev_game_id_side INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date_side DATE;

-- cumulative_game_stats (team cumulative/aggregate)
ALTER TABLE nba.cumulative_game_stats
    ADD COLUMN IF NOT EXISTS prev_game_id INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date DATE,
    ADD COLUMN IF NOT EXISTS prev_game_id_season INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date_season DATE;

-- Indexes to make the pointer lookups fast.
-- The loader joins snapshot.prev_game_id -> snapshot.game_id (a plain (game_id) index
-- exists); the entity+game_id equality lookups below cover the direct-join rewrite.
CREATE INDEX IF NOT EXISTS idx_trs_nba_pg      ON nba.team_rolling_stats (prev_game_id);
CREATE INDEX IF NOT EXISTS idx_trs_nba_pg_seas ON nba.team_rolling_stats (prev_game_id_season);
CREATE INDEX IF NOT EXISTS idx_trs_nba_pg_side ON nba.team_rolling_stats (prev_game_id_side);

CREATE INDEX IF NOT EXISTS idx_cgs_nba_pg      ON nba.cumulative_game_stats (prev_game_id);
CREATE INDEX IF NOT EXISTS idx_cgs_nba_pg_seas ON nba.cumulative_game_stats (prev_game_id_season);

-- Equality lookup for the loader's prior_game_id joins (mirrors the existing
-- idx_nba_trs_team_game on team_rolling_stats).
CREATE INDEX IF NOT EXISTS idx_nba_cgs_team_game ON nba.cumulative_game_stats (team_id, game_id);
