-- 20260812: Add MLB training features for platoon (L/R) + starting pitcher hand.
-- Team OPS vs RHP/LHP, Team runs-per-game vs RHP/LHP, and starting pitcher hand
-- (pick-card only). Per Rich: current_ats/current_ou/live_ats/live_ou all FALSE;
-- is_trainable + pick_card TRUE for the OPS/RPG features; pitcher_hand = pick_card ONLY.

INSERT INTO mlb.features
    (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card)
VALUES
    -- Home team OPS vs each arm
    ('h_ops_vs_rhp',   'Home team OPS vs RHP starters',   'Home OPS vs RHP',   FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    ('h_ops_vs_lhp',   'Home team OPS vs LHP starters',   'Home OPS vs LHP',   FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    ('a_ops_vs_rhp',   'Away team OPS vs RHP starters',   'Away OPS vs RHP',   FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    ('a_ops_vs_lhp',   'Away team OPS vs LHP starters',   'Away OPS vs LHP',   FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    -- Home/away team runs per game vs each arm
    ('h_rpg_vs_rhp',   'Home team runs per game vs RHP starters', 'Home RPG vs RHP', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    ('h_rpg_vs_lhp',   'Home team runs per game vs LHP starters', 'Home RPG vs LHP', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    ('a_rpg_vs_rhp',   'Away team runs per game vs RHP starters', 'Away RPG vs RHP', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    ('a_rpg_vs_lhp',   'Away team runs per game vs LHP starters', 'Away RPG vs LHP', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    -- Starting pitcher throwing hand (pick card only; NOT trainable)
    ('h_pitcher_hand', 'Home starting pitcher throwing hand (R or L)', 'Home SP Hand', FALSE, FALSE, FALSE, FALSE, FALSE, TRUE),
    ('a_pitcher_hand', 'Away starting pitcher throwing hand (R or L)', 'Away SP Hand', FALSE, FALSE, FALSE, FALSE, FALSE, TRUE)
ON CONFLICT (name) DO NOTHING;

-- Resolved platoon features: offense value vs the OPPOSING starter's arm
-- (home offense faces away SP; away offense faces home SP). Same flags as the
-- static L/R features: is_trainable + pick_card, current/live all FALSE.
INSERT INTO mlb.features
    (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card)
VALUES
    ('h_ops_vs_opp_hand', 'Home team OPS vs the opposing starting pitcher''s arm (R or L)', 'Home OPS vs Opp SP', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    ('a_ops_vs_opp_hand', 'Away team OPS vs the opposing starting pitcher''s arm (R or L)', 'Away OPS vs Opp SP', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    ('h_rpg_vs_opp_hand', 'Home team runs per game vs the opposing starting pitcher''s arm (R or L)', 'Home RPG vs Opp SP', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
    ('a_rpg_vs_opp_hand', 'Away team runs per game vs the opposing starting pitcher''s arm (R or L)', 'Away RPG vs Opp SP', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE)
ON CONFLICT (name) DO NOTHING;
