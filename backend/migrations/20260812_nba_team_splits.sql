-- NBA team splits (home/away, vs East/West team form) + ATS/O/U records.
--
-- Mirrors mlb.team_splits for venue/conference splits, but adds the
-- against-the-spread and over/under records this betting tool needs.
--
-- season_id NULL  -> CAREER row (all seasons aggregated)
-- season_id set   -> that season's split
--
-- split_type values: home | away | vs_east | vs_west
--
-- ATS is computed against the latest recorded betting line per game
-- (closing line = is_opening='N', falling back to opening). Margin is
-- from the perspective of the subject team (positive = they outscored).
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS nba.team_splits (
    id BIGSERIAL PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES nba.teams(id) ON DELETE CASCADE,
    season_id INTEGER REFERENCES nba.seasons(id) ON DELETE CASCADE,   -- NULL = career
    split_type TEXT NOT NULL,
    split_label TEXT NOT NULL,

    -- Games & results
    games INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    win_pct NUMERIC(5,3),

    -- Scoring
    points_for NUMERIC(8,1) NOT NULL DEFAULT 0,
    points_against NUMERIC(8,1) NOT NULL DEFAULT 0,
    point_differential NUMERIC(7,1) NOT NULL DEFAULT 0,
    pace NUMERIC(6,1),

    -- Basic team rates (derived from nba.games team stat columns)
    field_goal_pct NUMERIC(5,3),
    three_point_pct NUMERIC(5,3),
    free_throw_pct NUMERIC(5,3),
    rebounds_per_game NUMERIC(6,2),
    assists_per_game NUMERIC(6,2),
    steals_per_game NUMERIC(6,2),
    blocks_per_game NUMERIC(6,2),
    turnovers_per_game NUMERIC(6,2),
    fouls_per_game NUMERIC(6,2),

    -- ATS / O/U (banking) records
    ats_wins INTEGER NOT NULL DEFAULT 0,
    ats_losses INTEGER NOT NULL DEFAULT 0,
    ats_pushes INTEGER NOT NULL DEFAULT 0,
    ats_pct NUMERIC(5,3),
    ou_overs INTEGER NOT NULL DEFAULT 0,
    ou_unders INTEGER NOT NULL DEFAULT 0,
    ou_pushes INTEGER NOT NULL DEFAULT 0,
    ou_overs_pct NUMERIC(5,3),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (team_id, season_id, split_type)
);

CREATE INDEX IF NOT EXISTS idx_nba_team_splits_team
    ON nba.team_splits (team_id, season_id);
CREATE INDEX IF NOT EXISTS idx_nba_team_splits_type
    ON nba.team_splits (split_type);
