-- Add lineup-OPS features to mlb.features.
--
-- These capture the QUALITY of each team's starting lineup (the 9 batters in
-- today's batting order) via their season-to-date OPS, and how that compares
-- to the team's overall season OPS:
--
--   h_lineup_ops / a_lineup_ops        = avg season ytd_ops of the 9 starters
--   h_lineup_ops_minus_team / a_...    = lineup avg OPS minus team season OPS
--
-- Each starter's ytd_ops is read from mlb.player_batting_rolling_stats at their
-- most recent FINAL game strictly BEFORE the target game (leak-safe, capped
-- 30min before -- mirrors the team/pitcher rolling LATERAL pattern). The 9
-- starters come from mlb.lineups (batting_order 1-9) mapped to the target game.
--
-- Rich's spec for these lineup-OPS features:
--   * DO NOT list them as current/live for the ATS or OU models (he adds them
--     during training explicitly).
--   * DO set is_trainable = TRUE and pick_card = TRUE.
-- So current_ats / current_ou / live_ats / live_ou = FALSE, is_trainable /
-- pick_card = TRUE -- identical to the opponent-adjusted features.
--
-- Source columns are projected in MLB data_loader.GAME_QUERY and auto-catalogued.
-- Idempotent: ON CONFLICT (name) DO UPDATE.

INSERT INTO mlb.features (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card) VALUES
('h_lineup_ops',             'Home starting lineup avg season-to-date OPS (9 starters)',      'Home Lineup OPS',   FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_lineup_ops',             'Away starting lineup avg season-to-date OPS (9 starters)',      'Away Lineup OPS',   FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_lineup_ops_minus_team',  'Home lineup avg OPS minus home team season OPS',                'Home Lineup OPS vs Team', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_lineup_ops_minus_team',  'Away lineup avg OPS minus away team season OPS',                'Away Lineup OPS vs Team', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE)
ON CONFLICT (name) DO UPDATE SET
    description   = EXCLUDED.description,
    display_name  = EXCLUDED.display_name,
    current_ats   = EXCLUDED.current_ats,
    current_ou    = EXCLUDED.current_ou,
    is_trainable  = EXCLUDED.is_trainable,
    live_ats      = EXCLUDED.live_ats,
    live_ou       = EXCLUDED.live_ou,
    pick_card     = EXCLUDED.pick_card;
