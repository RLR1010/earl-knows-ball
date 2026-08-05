-- ═══════════════════════════════════════════════════════════════════════════════
--  migrate_game_type.sql  —  Add game_type to NFL stats tables
--  Purpose: allow preseason (PRE), regular-season (REG) and playoff (POST) rows
--  to coexist in the same stats tables without ever mixing. Production scripts
--  default to REG. A PRE inference never blends into REG training/inference.
--  Idempotent: safe to re-run.
--
--  Applied live: 2026-08-05 (verified REG populate → identical row counts).
-- ═══════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ---------------------------------------------------------------
-- nfl.cumulative_game_stats   — already has season_type. Ensure indexed.
-- ---------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_cgs_season_type ON nfl.cumulative_game_stats(season_type);

-- ---------------------------------------------------------------
-- nfl.team_pace_stats         — already has season_type. Ensure indexed.
-- ---------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tps_season_type ON nfl.team_pace_stats(season_type);

-- ---------------------------------------------------------------
-- nfl.team_rolling_stats  (per-game); add game_type + indexes.
-- LIVING PK stays (game_id, team_abbr) — a game_id maps to exactly one
-- game_type, so no collision between PRE/REG/POST on the same game.
-- ---------------------------------------------------------------
ALTER TABLE nfl.team_rolling_stats
    ADD COLUMN IF NOT EXISTS game_type VARCHAR(10) NOT NULL DEFAULT 'REG';
CREATE INDEX IF NOT EXISTS idx_trs_game_type ON nfl.team_rolling_stats(game_type);
DROP INDEX IF EXISTS idx_trs_season_team;
CREATE INDEX IF NOT EXISTS idx_trs_season_team_type ON nfl.team_rolling_stats(season, game_type, team_abbr);

-- ---------------------------------------------------------------
-- nfl.qb_cumulative_stats  (per-game); add game_type + widen PK so PRE and REG
--   are distinct upsert targets for the ON CONFLICT clauses.
-- ---------------------------------------------------------------
ALTER TABLE nfl.qb_cumulative_stats
    ADD COLUMN IF NOT EXISTS game_type VARCHAR(10) NOT NULL DEFAULT 'REG';
ALTER TABLE nfl.qb_cumulative_stats DROP CONSTRAINT IF EXISTS qb_cumulative_stats_pkey;
ALTER TABLE nfl.qb_cumulative_stats
    ADD CONSTRAINT qb_cumulative_stats_pkey PRIMARY KEY (player_id, season, game_id, game_type);
CREATE INDEX IF NOT EXISTS idx_qcs_game_type ON nfl.qb_cumulative_stats(game_type);

-- ---------------------------------------------------------------
-- nfl.qb_rolling_stats  (per-game); add game_type + widen PK.
-- ---------------------------------------------------------------
ALTER TABLE nfl.qb_rolling_stats
    ADD COLUMN IF NOT EXISTS game_type VARCHAR(10) NOT NULL DEFAULT 'REG';
ALTER TABLE nfl.qb_rolling_stats DROP CONSTRAINT IF EXISTS qb_rolling_stats_pkey;
ALTER TABLE nfl.qb_rolling_stats
    ADD CONSTRAINT qb_rolling_stats_pkey PRIMARY KEY (player_id, season, game_id, game_type);
CREATE INDEX IF NOT EXISTS idx_qrs_game_type ON nfl.qb_rolling_stats(game_type);

-- ---------------------------------------------------------------
-- nfl.player_weekly_stats  (per-game); add game_type + index.
-- ---------------------------------------------------------------
ALTER TABLE nfl.player_weekly_stats
    ADD COLUMN IF NOT EXISTS game_type VARCHAR(10) NOT NULL DEFAULT 'REG';
CREATE INDEX IF NOT EXISTS idx_player_weekly_game_type ON nfl.player_weekly_stats(game_type);

-- ---------------------------------------------------------------
-- nfl.prior_team_stats  (season-level); add game_type and widen the PK so a
--   season can hold independent REG and PRE prior-stats rows.
-- ---------------------------------------------------------------
ALTER TABLE nfl.prior_team_stats
    ADD COLUMN IF NOT EXISTS game_type VARCHAR(10) NOT NULL DEFAULT 'REG';
ALTER TABLE nfl.prior_team_stats DROP CONSTRAINT IF EXISTS prior_team_stats_pkey;
ALTER TABLE nfl.prior_team_stats
    ADD CONSTRAINT prior_team_stats_pkey PRIMARY KEY (team_abbr, season, game_type);

-- ---------------------------------------------------------------
-- Backfill game_type on existing per-game tables from nfl.games.
-- (All pre-change rows are REG; backfill via game_id for correctness.)
-- prior_team_stats has no game_id → defaults to REG, which is correct.
-- ---------------------------------------------------------------
UPDATE nfl.team_rolling_stats trs
   SET game_type = COALESCE(NULLIF(g.game_type,''), 'REG')
  FROM nfl.games g WHERE g.id = trs.game_id AND trs.game_type = 'REG';

UPDATE nfl.qb_cumulative_stats q
   SET game_type = COALESCE(NULLIF(g.game_type,''), 'REG')
  FROM nfl.games g WHERE g.id = q.game_id AND q.game_type = 'REG';

UPDATE nfl.qb_rolling_stats q
   SET game_type = COALESCE(NULLIF(g.game_type,''), 'REG')
  FROM nfl.games g WHERE g.id = q.game_id AND q.game_type = 'REG';

UPDATE nfl.player_weekly_stats pws
   SET game_type = COALESCE(NULLIF(g.game_type,''), 'REG')
  FROM nfl.games g WHERE g.id = pws.game_id AND pws.game_type = 'REG';

COMMIT;
