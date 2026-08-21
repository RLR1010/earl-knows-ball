-- Add lineup production (RBI / runs) + share-of-team features to mlb.features.
--
-- For each team's starting 9 (from mlb.lineups, batting_order 1-9 of the TARGET
-- game), sums each starter's season RBI and runs from mlb.batting_game_stats
-- (leak-safe: strict FINAL games strictly before the target, same-season only),
-- then computes the starters' share of the team's total season production:
--
--   *_lineup_runs      = sum of the 9 starters' season runs scored
--   *_lineup_rbi       = sum of the 9 starters' season RBI
--   *_lineup_pct_runs  = lineup_runs  / team season runs   (all batters)
--   *_lineup_pct_rbi   = lineup_rbi / team season RBI      (all batters)
--
-- Team totals derive from ALL batters on the team (team id resolved per bgs row
-- via the game's home/away_team_id matched to bgs.team_side), same-season, leak-safe.
--
-- Rich's spec (same as lineup_ops): training-only features, added during training.
--   * NOT listed as current/live for ATS or OU models.
--   * is_trainable = TRUE, pick_card = TRUE.
-- So current_ats / current_ou / live_ats / live_ou = FALSE, is_trainable / pick_card = TRUE.
--
-- Source columns are projected in MLB data_loader.GAME_QUERY (lpop_h/lpop_a
-- LATERALs) and auto-catalogued. Idempotent: ON CONFLICT (name) DO UPDATE.

INSERT INTO mlb.features (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card) VALUES
('h_lineup_runs',     'Home starting lineup Sum of season runs scored (9 starters)',               'Home Lineup Runs',    FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_lineup_rbi',      'Home starting lineup Sum of season RBI (9 starters)',                        'Home Lineup RBI',     FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_lineup_pct_runs', 'Home lineup season runs / team season runs (share of offense)',              'Home Lineup % Runs',  FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_lineup_pct_rbi',  'Home lineup season RBI / team season RBI (share of offense)',                'Home Lineup % RBI',   FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_lineup_runs',     'Away starting lineup Sum of season runs scored (9 starters)',               'Away Lineup Runs',    FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_lineup_rbi',      'Away starting lineup Sum of season RBI (9 starters)',                        'Away Lineup RBI',     FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_lineup_pct_runs', 'Away lineup season runs / team season runs (share of offense)',              'Away Lineup % Runs',  FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_lineup_pct_rbi',  'Away lineup season RBI / team season RBI (share of offense)',                'Away Lineup % RBI',   FALSE, FALSE, TRUE, FALSE, FALSE, TRUE)
ON CONFLICT (name) DO UPDATE SET
    description   = EXCLUDED.description,
    display_name  = EXCLUDED.display_name,
    current_ats   = EXCLUDED.current_ats,
    current_ou    = EXCLUDED.current_ou,
    is_trainable  = EXCLUDED.is_trainable,
    live_ats      = EXCLUDED.live_ats,
    live_ou       = EXCLUDED.live_ou,
    pick_card     = EXCLUDED.pick_card;
