-- 20260816_nba_game_boxscore_team_stats.sql
--
-- Add the full set of game box-score team stats supplied by the ESPN core API
-- team-statistics endpoint to nba.games. Previously we only persisted a subset
-- (fgm/fga, 3pm/3pa, ftm/fta, rebounds, assists, steals, blocks, turnovers,
-- fouls) and derived the rest from player_game_stats sums.
--
-- The fields below come straight from ESPN's per-team statistics payload:
--   * real offensive/defensive rebounds (we previously only stored total rebounds
--     and approximated ORB with a 0.245 proxy in cumulative_stats.py)
--   * ESPN's own estimatedPossessions (the authoritative possession count — this
--     lets us fix the broken ORTG/DRTG formula instead of reinventing it)
--   * scoring/advanced metrics (pointsInPaint, fastBreak, turnoverPoints, ratios,
--     NBARating, VORP, etc.)
--
-- Naming convention: home_*/away_* mirror the existing boxscore columns.
-- All are NULLable so the player-sum backfill (which fills the stat columns that
-- ARE derivable from players) and the ESPN team-statistics backfill (which fills
-- the team-only ones) can each write their slice independently.

-- Offensive / possession stats
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_offensive_rebounds INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_offensive_rebounds INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_defensive_rebounds INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_defensive_rebounds INTEGER;

ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_two_point_field_goals_made INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_two_point_field_goals_made INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_two_point_field_goals_attempted INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_two_point_field_goals_attempted INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_two_point_field_goal_pct NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_two_point_field_goal_pct NUMERIC(6,3);

ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_points_in_paint INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_points_in_paint INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_fast_break_points INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_fast_break_points INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_turnover_points INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_turnover_points INTEGER;

-- Turnovers split (we only stored total player turnovers before)
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_team_turnovers INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_team_turnovers INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_total_turnovers INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_total_turnovers INTEGER;

-- Possessions + pace/advanced (ESPN authoritative)
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_estimated_possessions NUMERIC(8,2);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_estimated_possessions NUMERIC(8,2);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_offensive_rebound_pct NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_offensive_rebound_pct NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_points_per_estimated_possessions NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_points_per_estimated_possessions NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_scoring_efficiency NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_scoring_efficiency NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_shooting_efficiency NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_shooting_efficiency NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_brick_index NUMERIC(8,2);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_brick_index NUMERIC(8,2);

-- FG attempts that didn't make possession (ESPN shot split)
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_field_goals_that_made_possession NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_field_goals_that_made_possession NUMERIC(6,3);

-- Lead / game-flow stats
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_lead_changes INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_lead_changes INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_largest_lead INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_largest_lead INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_lead_percentage NUMERIC(6,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_lead_percentage NUMERIC(6,3);

-- Fouling detail (we only stored total personal fouls)
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_technical_fouls INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_technical_fouls INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_flagrant_fouls INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_flagrant_fouls INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_ejections INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_ejections INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_disqualifications INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_disqualifications INTEGER;

-- Lineup / outing counts
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_double_double INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_double_double INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_triple_double INTEGER;
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_triple_double INTEGER;

-- Ratios + advanced ratings (ESPN pre-computed)
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_assist_turnover_ratio NUMERIC(8,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_assist_turnover_ratio NUMERIC(8,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_steal_turnover_ratio NUMERIC(8,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_steal_turnover_ratio NUMERIC(8,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_steal_foul_ratio NUMERIC(8,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_steal_foul_ratio NUMERIC(8,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_block_foul_ratio NUMERIC(8,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_block_foul_ratio NUMERIC(8,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_team_assist_turnover_ratio NUMERIC(8,3);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_team_assist_turnover_ratio NUMERIC(8,3);

ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_nba_rating NUMERIC(8,2);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_nba_rating NUMERIC(8,2);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS home_vorp NUMERIC(8,2);
ALTER TABLE nba.games ADD COLUMN IF NOT EXISTS away_vorp NUMERIC(8,2);
