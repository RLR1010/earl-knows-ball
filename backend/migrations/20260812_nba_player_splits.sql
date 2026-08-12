-- NBA player splits + career stats (mirrors MLB/NFL player_splits design)
--
-- season_id NULL  -> CAREER row (aggregated across all seasons)
-- season_id set   -> that season's split
--
-- split_type values:
--   home | away                 venue
--   vs_east | vs_west           vs opponent conference
--   starter | bench             started or came off bench
--   rest0                       back-to-back (0 days rest)
--   rest_ge1                    >=1 day rest
--   month_<oct|nov|dec|jan|feb|mar|apr>   calendar month (per season only,
--                                           no career rows for months)
-- season_id NULL rows are the CAREER aggregate (all split types except months).
--
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS nba.player_splits (
    id BIGSERIAL PRIMARY KEY,
    player_id INTEGER NOT NULL REFERENCES nba.players(id) ON DELETE CASCADE,
    season_id INTEGER REFERENCES nba.seasons(id) ON DELETE CASCADE,   -- NULL = career
    team_id INTEGER REFERENCES nba.teams(id) ON DELETE CASCADE,       -- actual team for that split set
    split_type TEXT NOT NULL,
    split_label TEXT NOT NULL,
    games INTEGER NOT NULL DEFAULT 0,
    games_started INTEGER NOT NULL DEFAULT 0,
    minutes_per_game NUMERIC(6,2),
    -- scoring
    points_per_game NUMERIC(6,2),
    field_goals_pct NUMERIC(5,3),
    three_point_pct NUMERIC(5,3),
    free_throw_pct NUMERIC(5,3),
    -- per-game
    rebounds_per_game NUMERIC(6,2),
    offensive_rebounds_per_game NUMERIC(6,2),
    defensive_rebounds_per_game NUMERIC(6,2),
    assists_per_game NUMERIC(6,2),
    steals_per_game NUMERIC(6,2),
    blocks_per_game NUMERIC(6,2),
    turnovers_per_game NUMERIC(6,2),
    fouls_per_game NUMERIC(6,2),
    plus_minus_per_game NUMERIC(6,2),
    -- advanced
    true_shooting_pct NUMERIC(5,3),
    usage_pct NUMERIC(5,3),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (player_id, season_id, split_type)
);

-- Index for chat lookups by player + split, and career filtering.
CREATE INDEX IF NOT EXISTS idx_nba_player_splits_player
    ON nba.player_splits (player_id, season_id);
CREATE INDEX IF NOT EXISTS idx_nba_player_splits_type
    ON nba.player_splits (split_type);
