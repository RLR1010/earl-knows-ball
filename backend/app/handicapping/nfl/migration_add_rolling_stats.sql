-- Migration: Add missing columns to nfl.team_rolling_stats
-- These were added to the populate script but need ALTER TABLE for existing DB

-- Simple rolling averages
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS pass_att_r3 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS pass_att_r5 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS rush_att_r3 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS rush_att_r5 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS rush_td_r3 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS rush_td_r5 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS fumbles_r3 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS fumbles_r5 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS fourth_down_pct_r3 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS fourth_down_pct_r5 REAL;

-- Standard deviations
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS off_pts_stddev_r5 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS off_yds_stddev_r5 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS opp_pts_stddev_r5 REAL;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS opp_yds_stddev_r5 REAL;

-- Season ranks
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS off_yardage_rank INTEGER;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS def_yardage_rank INTEGER;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS off_scoring_rank INTEGER;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS def_scoring_rank INTEGER;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS off_rushing_rank INTEGER;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS def_rushing_rank INTEGER;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS off_passing_rank INTEGER;
ALTER TABLE nfl.team_rolling_stats ADD COLUMN IF NOT EXISTS def_passing_rating_rank INTEGER;
