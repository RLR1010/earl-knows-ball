-- 2026-08-22 NBA active-Player features.
--
-- These capture the CUMULATIVE production of each team's ACTIVE roster for a
-- given game (mirrors the MLB lineup-OPS features; see 20260820_lineup_ops*).
--
--   h_active_pts / a_active_pts   = sum of each active player's most-recent-prior
--                                   cumulative PPG (leak-safe, from
--                                   nba.player_rolling_stats.cum_ppg)
--   h_active_reb / h_active_ast   = same via cum_rpg / cum_apg
--   h_active_n  / a_active_n      = number of active players (auxiliary)
--   *_minus_team                  = active sum minus the team's own cumulative
--                                   stat (hcs.cum_ppg / cum_reb_pg / cum_ast_pg)
--
-- The active roster comes from nba.active_players for the target game (pregame
-- filled; postgame backfilled for training), falling back to the team's most
-- recent prior FINAL game's roster when a scheduled game has not been filled
-- yet (mirrors MLB h_lin/a_lin effective-game fallback).
--
-- Flags (per Rich's spec for the lineup features): current/live ATS+OU = FALSE
-- (added during training explicitly), is_trainable = TRUE. The core pts/reb/ast
-- and minus_team features show on the pick card; the _n counts are auxiliary
-- (is_trainable FALSE, not on pick card).
--
-- Source columns are projected in NBA data_loader.GAME_QUERY and auto-catalogued.
-- Idempotent: ON CONFLICT (name) DO UPDATE.

INSERT INTO nba.features (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card) VALUES
-- Home active-player cumulative production
('h_active_pts',             'Home active roster sum of most-recent-prior cumulative PPG',      'Home Active PTS',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_active_reb',             'Home active roster sum of most-recent-prior cumulative RPG',      'Home Active REB',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_active_ast',             'Home active roster sum of most-recent-prior cumulative APG',      'Home Active AST',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_active_n',               'Home number of active players',                                   'Home Active Players',   FALSE, FALSE, FALSE, FALSE, FALSE, FALSE),
('h_active_pts_minus_team',  'Home active PTS sum minus home team cumulative PPG',              'Home Active PTS vs Team',FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_active_reb_minus_team',  'Home active REB sum minus home team cumulative RPG',              'Home Active REB vs Team',FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_active_ast_minus_team',  'Home active AST sum minus home team cumulative APG',              'Home Active AST vs Team',FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
-- Away active-player cumulative production
('a_active_pts',             'Away active roster sum of most-recent-prior cumulative PPG',      'Away Active PTS',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_active_reb',             'Away active roster sum of most-recent-prior cumulative RPG',      'Away Active REB',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_active_ast',             'Away active roster sum of most-recent-prior cumulative APG',      'Away Active AST',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_active_n',               'Away number of active players',                                   'Away Active Players',   FALSE, FALSE, FALSE, FALSE, FALSE, FALSE),
('a_active_pts_minus_team',  'Away active PTS sum minus away team cumulative PPG',              'Away Active PTS vs Team',FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_active_reb_minus_team',  'Away active REB sum minus away team cumulative RPG',              'Away Active REB vs Team',FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_active_ast_minus_team',  'Away active AST sum minus away team cumulative APG',              'Away Active AST vs Team',FALSE, FALSE, TRUE, FALSE, FALSE, TRUE)
ON CONFLICT (name) DO UPDATE SET
    description  = EXCLUDED.description,
    display_name = EXCLUDED.display_name,
    current_ats  = EXCLUDED.current_ats,
    current_ou   = EXCLUDED.current_ou,
    is_trainable = EXCLUDED.is_trainable,
    live_ats     = EXCLUDED.live_ats,
    live_ou      = EXCLUDED.live_ou,
    pick_card    = EXCLUDED.pick_card;
