-- Add NBA team-split features (home/away + vs conference) to nba.features.
-- All columns FALSE except is_trainable=TRUE and pick_card=TRUE (Rich's spec).
-- Derived from nba.team_splits in data_loader.py (prior-season splits).
-- NOT in current ATS/OU models (current_ats/current_ou = false).
-- Idempotent: ON CONFLICT (name) DO UPDATE.
--
-- Feature set (12): home/away venue ATS + OU over-rate + points for/against,
-- plus vs-opponent-conference ATS + OU over-rate. No back-to-back/rest features.

INSERT INTO nba.features (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card) VALUES
('h_ats_pct_home',            'Home team ATS cover % at home (prior season)',             'Home ATS% Home',      FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_ats_pct_away',            'Away team ATS cover % on road (prior season)',             'Away ATS% Away',      FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_ou_over_pct_home',        'Home team OU over % at home (prior season)',               'Home OU Over% Home',  FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_ou_over_pct_away',        'Away team OU over % on road (prior season)',               'Away OU Over% Away',  FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_pts_home',                'Home team pts-for per game at home (prior season)',        'Home PTS Home',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_pts_away',                'Away team pts-for per game on road (prior season)',        'Away PTS Away',       FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_pts_against_home',        'Home team pts-against per game at home (prior season)',    'Home PTS-Ag Home',    FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_pts_against_away',        'Away team pts-against per game on road (prior season)',    'Away PTS-Ag Away',    FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_ats_pct_vs_conf',         'Home team ATS cover % vs opponent conference (prior season)', 'Home ATS% vsConf',  FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_ats_pct_vs_conf',         'Away team ATS cover % vs opponent conference (prior season)', 'Away ATS% vsConf',  FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_ou_over_pct_vs_conf',     'Home team OU over % vs opponent conference (prior season)', 'Home OU Over% vsConf', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_ou_over_pct_vs_conf',     'Away team OU over % vs opponent conference (prior season)', 'Away OU Over% vsConf', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE)
ON CONFLICT (name) DO UPDATE SET
    description    = EXCLUDED.description,
    display_name   = EXCLUDED.display_name,
    current_ats    = EXCLUDED.current_ats,
    current_ou     = EXCLUDED.current_ou,
    is_trainable   = EXCLUDED.is_trainable,
    live_ats       = EXCLUDED.live_ats,
    live_ou        = EXCLUDED.live_ou,
    pick_card      = EXCLUDED.pick_card;
