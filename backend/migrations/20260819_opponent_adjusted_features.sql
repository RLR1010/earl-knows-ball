-- Add opponent-adjusted season-cumulative metrics to mlb.features.
--
-- These adjust a team's raw runs-scored and win percentage for the QUALITY OF OPPONENT,
-- following Rich's chosen defaults (difference-based runs + multiplicative SOS
-- wins), season-cumulative, overall (home+away combined):
--
--   h_adj_rf_avg  = home rf_avg + (league_avg_ra - away ra_avg)   [diff-adjusted runs/game]
--   a_adj_rf_avg  = away rf_avg + (league_avg_ra - home ra_avg)
--   h_adj_win_pct = home win_pct * (away win_pct / 0.500)         [SOS-adjusted win pct]
--   a_adj_win_pct = away win_pct * (home win_pct / 0.500)
--
-- Direction is SOS-consistent: facing a STRONGER-than-league opponent underrates
-- a team, so adjusted values are RAISED (strong staff -> low ra_avg -> +delta on
-- runs; strong opp win_pct -> factor > 1 -> raised win pct). League constants sampled
-- from live data (~4.47 runs/game/team, 0.500 = .500).
--
-- The source columns are now projected in MLB data_loader.GAME_QUERY (h_adj_rf_avg,
-- h_adj_win_pct, a_adj_rf_avg, a_adj_win_pct) and auto-catalogued.
--
-- Rich's spec: is_trainable = TRUE, pick_card = TRUE, but current_ats / current_ou /
-- live_ats / live_ou = FALSE (NOT yet in the live ATS/OU sets until validated).
-- Idempotent: ON CONFLICT (name) DO UPDATE.

INSERT INTO mlb.features (name, description, display_name, current_ats, current_ou, is_trainable, live_ats, live_ou, pick_card) VALUES
('h_adj_rf_avg',      'Home team season runs scored per game, adjusted for opposing pitching strength (quality of opponent)', 'Home Runs Adj (Season)',  FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_adj_rf_avg',      'Away team season runs scored per game, adjusted for opposing pitching strength (quality of opponent)', 'Away Runs Adj (Season)',  FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('h_adj_win_pct',     'Home team season win pct, adjusted for strength of schedule (quality of opponent)',                   'Home WinPct Adj (Season)', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE),
('a_adj_win_pct',     'Away team season win pct, adjusted for strength of schedule (quality of opponent)',                   'Away WinPct Adj (Season)', FALSE, FALSE, TRUE, FALSE, FALSE, TRUE)
ON CONFLICT (name) DO UPDATE SET
    description   = EXCLUDED.description,
    display_name  = EXCLUDED.display_name,
    current_ats   = EXCLUDED.current_ats,
    current_ou    = EXCLUDED.current_ou,
    is_trainable  = EXCLUDED.is_trainable,
    live_ats      = EXCLUDED.live_ats,
    live_ou       = EXCLUDED.live_ou,
    pick_card     = EXCLUDED.pick_card;
