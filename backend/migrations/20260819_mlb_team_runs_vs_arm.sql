-- 20260819_mlb_team_runs_vs_arm.sql
--
-- Cumulative per-game table of a team's OFFENSE RUNS scored vs the opposing
-- starter's arm (L/R). Replaces the 8 correlated rpg_vs_arm scalar subqueries
-- in mlb.data_loader (h_rpg_vs_lhp/_rhp, a_rpg_vs_lhp/_rhp + vs_opp_hand), which
-- re-scan the team's full game history per game row (~21% of load_games time).
-- Precompute once so the loader LEFT JOINs the previous Final row instead.
--
-- Semantics match the loader's inline AVG exactly:
--   * (team, season, team_side, arm) -> cumulative AVG of runs that team scored
--     (home side = home_score, away side = away_score) through each game,
--     CURRENT ROW (own result included), leak-safe (loader reads PREVIOUS row
--     with a 30-min cutoff + FINAL filter).
-- PK = (team_id, season_id, game_id, team_side, arm) -> exactly one L/R bucket
-- per game per side.

CREATE TABLE IF NOT EXISTS mlb.team_runs_vs_arm (
    game_id        integer         NOT NULL,
    team_id        integer         NOT NULL,
    season_id      integer         NOT NULL,
    team_side      varchar(4)      NOT NULL CHECK (team_side IN ('home','away')),
    arm            varchar(1)      NOT NULL CHECK (arm IN ('L','R')),

    runs_vs_arm    integer         NOT NULL DEFAULT 0,  -- cumulative runs scored
    games_vs_arm   integer         NOT NULL DEFAULT 0,  -- count of games

    rpg_vs_arm     numeric(10,4),  -- runs_vs_arm / games_vs_arm (through-date)

    PRIMARY KEY (team_id, season_id, game_id, team_side, arm)
);

CREATE INDEX IF NOT EXISTS ix_mlb_team_runs_vs_arm_game
    ON mlb.team_runs_vs_arm (game_id);
CREATE INDEX IF NOT EXISTS ix_mlb_team_runs_vs_arm_arm_side
    ON mlb.team_runs_vs_arm (team_id, season_id, team_side, arm, game_id);

ALTER TABLE mlb.team_runs_vs_arm OWNER TO earl;
GRANT ALL ON TABLE mlb.team_runs_vs_arm TO earl;
