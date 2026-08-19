-- Add venue-conditional home/road features (last-10 + season) to mlb.features
-- and nba.features. These measure play AT the venue (home team at home, away
-- team on road), from venue_rf_r10/venue_win_pct_r10/venue_win_pct_season
-- (MLB) and venue_pts_r10/venue_win_pct_r10/venue_win_pct_season (NBA).
--
-- Rich's spec: is_trainable = TRUE, but current_ats / current_ou / live_ats /
-- live_ou = FALSE (NOT yet in the live ATS/OU feature sets). pick_card = TRUE so
-- handicappers see them. Idempotent: ON CONFLICT (name) DO UPDATE.
--
-- Feature set (12: 6 MLB + 6 NBA):

INSERT INTO mlb.features (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card) VALUES
('h_home_rf_r10',           'Home team avg runs scored in its last 10 HOME games (venue-conditional rolling)',   'Home Runs L10 Home',      FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_away_rf_r10',           'Away team avg runs scored in its last 10 ROAD games (venue-conditional rolling)',   'Away Runs L10 Away',      FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_home_win_pct_r10',      'Home team win pct in its last 10 HOME games',                                          'Home Win%% L10 Home',      FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_away_win_pct_r10',      'Away team win pct in its last 10 ROAD games',                                          'Away Win%% L10 Away',      FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_home_win_pct_season',   'Home team season win pct in HOME games only (venue-scoped season)',                     'Home Win%% Home (Season)', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_away_win_pct_season',   'Away team season win pct in ROAD games only (venue-scoped season)',                     'Away Win%% Away (Season)', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE)
ON CONFLICT (name) DO UPDATE SET
    description    = EXCLUDED.description,
    display_name   = EXCLUDED.display_name,
    current_ats    = EXCLUDED.current_ats,
    current_ou     = EXCLUDED.current_ou,
    is_trainable   = EXCLUDED.is_trainable,
    live_ats       = EXCLUDED.live_ats,
    live_ou        = EXCLUDED.live_ou,
    pick_card      = EXCLUDED.pick_card;

INSERT INTO nba.features (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card) VALUES
('h_home_pts_r10',          'Home team avg points in its last 10 HOME games (venue-conditional rolling)',        'Home PTS L10 Home',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_away_pts_r10',          'Away team avg points in its last 10 ROAD games (venue-conditional rolling)',        'Away PTS L10 Away',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_home_win_pct_r10',      'Home team win pct in its last 10 HOME games',                                          'Home Win%% L10 Home',      FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_away_win_pct_r10',      'Away team win pct in its last 10 ROAD games',                                          'Away Win%% L10 Away',      FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_home_win_pct_season',   'Home team season win pct in HOME games only (venue-scoped season)',                     'Home Win%% Home (Season)', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_away_win_pct_season',   'Away team season win pct in ROAD games only (venue-scoped season)',                     'Away Win%% Away (Season)', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE)
ON CONFLICT (name) DO UPDATE SET
    description    = EXCLUDED.description,
    display_name   = EXCLUDED.display_name,
    current_ats    = EXCLUDED.current_ats,
    current_ou     = EXCLUDED.current_ou,
    is_trainable   = EXCLUDED.is_trainable,
    live_ats       = EXCLUDED.live_ats,
    live_ou        = EXCLUDED.live_ou,
    pick_card      = EXCLUDED.pick_card;
