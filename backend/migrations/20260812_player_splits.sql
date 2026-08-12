-- Add per-player split table (mlb) for platoon / situational research.
-- Supports batter L/R splits, home/away, day/night, grass/turf, and city splits.
-- Rich's Earl chat + prop-bet writeups consume these.
--
-- split_type values (string), matching the MLB Stats API sitCodes + our derived ones:
--   vs_lhp, vs_rhp         -> batter vs left/right-handed pitcher (API sitCodes vl/vr)
--   home, away             -> game location (API sitCodes h/a)
--   day, night             -> game time (API sitCodes d/n)
--   grass, turf            -> field surface (API sitCodes g/t)
--   city_<normalized>      -> derived from game home team's city, e.g. city_cleveland
--
-- season_id IS NULL represents the CAREER aggregate; season_id set = that season.
-- Model: MLBPlayerSplit (app/models/mlb/player_split.py)
-- Idempotent.
CREATE TABLE IF NOT EXISTS mlb.player_splits (
    id              SERIAL PRIMARY KEY,
    player_id       INTEGER NOT NULL REFERENCES mlb.players(id) ON DELETE CASCADE,
    season_id       INTEGER REFERENCES mlb.seasons(id) ON DELETE CASCADE,
    split_type      TEXT NOT NULL,
    -- Context metadata
    split_label     TEXT,               -- human label e.g. "vs LHP", "Home", "City: Cleveland"
    city            TEXT,               -- normalized city slug when split_type is city_*
    -- Core counting stats
    games_played    INTEGER DEFAULT 0,
    plate_appearances INTEGER DEFAULT 0,
    at_bats         INTEGER DEFAULT 0,
    runs            INTEGER DEFAULT 0,
    hits            INTEGER DEFAULT 0,
    doubles         INTEGER DEFAULT 0,
    triples         INTEGER DEFAULT 0,
    home_runs       INTEGER DEFAULT 0,
    runs_batted_in  INTEGER DEFAULT 0,
    base_on_balls   INTEGER DEFAULT 0,
    strikeouts      INTEGER DEFAULT 0,
    hit_by_pitch    INTEGER DEFAULT 0,
    sacrifice_flies INTEGER DEFAULT 0,
    -- Rate stats
    avg             DOUBLE PRECISION,
    obp             DOUBLE PRECISION,
    slg             DOUBLE PRECISION,
    ops             DOUBLE PRECISION,
    woba            DOUBLE PRECISION,
    babip           DOUBLE PRECISION,
    iso             DOUBLE PRECISION,
    -- Derived
    total_bases     INTEGER DEFAULT 0,
    -- Freshness
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (player_id, split_type, season_id)
);

-- Index for Earl chat research: lookup by player + split type
CREATE INDEX IF NOT EXISTS idx_mlb_player_splits_player ON mlb.player_splits (player_id, split_type);
CREATE INDEX IF NOT EXISTS idx_mlb_player_splits_season ON mlb.player_splits (season_id);
