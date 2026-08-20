-- Add high-signal starter FIP (Fielding Independent Pitching) to the MLB OU
-- model. FIP is purely strikeout/walk/home-run driven (independent of team
-- defense), so it is a far less noisy predictor of future runs allowed than ERA.
--
-- Rich's request (2026-08-19): add FIP to the OU model. The data already flows
-- through the loader (h_pitcher_fip_ytd / a_pitcher_fip_ytd from
-- pitcher_rolling_stats.fip_ytd) but was never registered as current_ou, so
-- get_model_features('ou') never handed it to the model.
--
-- Scope: enable ONLY for OU (current_ou=TRUE). Leave OUT of ATS (current_ats=
-- FALSE). Not live yet (live_ou=FALSE) and not pick_card (it is a model input,
-- not a handicap display metric). Gating vs 1-start outliers is handled in
-- data_loader.build_features (NULL when starter has < 3 starts).
--
-- Idempotent: ON CONFLICT (name) DO UPDATE.

INSERT INTO mlb.features (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card) VALUES
('h_pitcher_fip_ytd', 'Home starting pitcher season-cumulative FIP (fielding independent pitching; NULL until >= 3 starts)', 'Home SP FIP YTD', FALSE, TRUE, TRUE, FALSE, FALSE, FALSE),
('a_pitcher_fip_ytd', 'Away starting pitcher season-cumulative FIP (fielding independent pitching; NULL until >= 3 starts)', 'Away SP FIP YTD', FALSE, TRUE, TRUE, FALSE, FALSE, FALSE)
ON CONFLICT (name) DO UPDATE SET
    description  = EXCLUDED.description,
    display_name = EXCLUDED.display_name,
    current_ats  = EXCLUDED.current_ats,
    current_ou   = EXCLUDED.current_ou,
    is_trainable = EXCLUDED.is_trainable,
    live_ats     = EXCLUDED.live_ats,
    live_ou      = EXCLUDED.live_ou,
    pick_card    = EXCLUDED.pick_card;
