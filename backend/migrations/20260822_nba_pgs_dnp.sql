-- 2026-08-22 NBA player_game_stats: active/inactive (DNP) + populating is_starter.
-- Source: ESPN boxscore summary endpoint (per-game roster with starter + didNotPlay flags).
-- The `is_starter` column already existed but was never populated (100% NULL).
-- We add `dnp` (did not play / inactive) + `dnp_reason` (e.g. "COACH'S DECISION").
--
-- Semantics (ESPN field mapping):
--   * starter      -> is_starter  = TRUE  (official 5 starters)
--   * didNotPlay   -> dnp         = TRUE  (inactive / DNP that game)
--   * reason       -> dnp_reason          (DNP reason string)
-- A player with dnp = FALSE and minutes > 0 is "active/played".
ALTER TABLE nba.player_game_stats
    ADD COLUMN IF NOT EXISTS dnp         BOOLEAN,
    ADD COLUMN IF NOT EXISTS dnp_reason  VARCHAR(255);
