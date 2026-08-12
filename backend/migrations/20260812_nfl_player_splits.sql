-- Add per-player situational/career split table (nfl) for chat research + props.
-- Computed from nfl.player_weekly_stats x nfl.games (game_id join) so NO new source.
-- Supports home/away, temperature/precipitation/dome, grass/turf, division,
-- primetime, opponent-tier splits. season_id IS NULL represents CAREER aggregate.
--
-- split_type values:
--   home, away            -> game location
--   cold, mild, warm      -> temperature buckets (cold <40F, mild 40-69, warm >=70)
--   precipitation         -> game w/ any precip (rain/snow)
--   clear                 -> no precip
--   dome, outdoor         -> roof_type
--   grass, turf           -> surface_type
--   division, non_division-> vs division rivals
--   primetime, day        -> day of week/time slot (SNF/MNF/THU vs SUN)
--   vs_top10, vs_mid, vs_bottom -> vs defense EPA tier (needs crosswalk; later)
--
-- Position-aware columns (QB/RB/WR/TE/DEF all fit):
--   pass_* (QBs), rush_* (RB + rushing QBs), recv_* (WR/TE + pass-catching RBs),
--   fantasy_* (std/half/ppr) for cross-position "how hot" answers,
--   def_* (points/yards allowed, sacks, turnovers) for DEF/IDP rows.
CREATE TABLE IF NOT EXISTS nfl.player_splits (
    id              SERIAL PRIMARY KEY,
    player_id       INTEGER NOT NULL REFERENCES nfl.players(id) ON DELETE CASCADE,
    season_id       INTEGER,            -- NULL = career
    split_type      TEXT NOT NULL,
    split_label     TEXT,               -- human label e.g. "Home", "Cold (<40F)", "Dome"
    opponent_side   TEXT,               -- 'home'/'away' context maybe; reserved
    games_played    INTEGER DEFAULT 0,
    -- Passing (QBs)
    pass_attempts   INTEGER DEFAULT 0,
    pass_completions INTEGER DEFAULT 0,
    pass_yards      INTEGER DEFAULT 0,
    pass_tds        INTEGER DEFAULT 0,
    pass_int        INTEGER DEFAULT 0,
    passer_rating   DOUBLE PRECISION,
    -- Rushing
    rush_attempts   INTEGER DEFAULT 0,
    rush_yards      INTEGER DEFAULT 0,
    rush_tds        INTEGER DEFAULT 0,
    -- Receiving
    targets         INTEGER DEFAULT 0,
    receptions      INTEGER DEFAULT 0,
    receiving_yards INTEGER DEFAULT 0,
    receiving_tds   INTEGER DEFAULT 0,
    -- Misc
    fumbles         INTEGER DEFAULT 0,
    -- Fantasy (for cross-position "who's hot" + props)
    fantasy_std     DOUBLE PRECISION,
    fantasy_half    DOUBLE PRECISION,
    fantasy_ppr     DOUBLE PRECISION,
    -- Defense (DEF/IDP)
    def_points_allowed INTEGER DEFAULT 0,
    def_tackles     INTEGER DEFAULT 0,
    def_sacks       INTEGER DEFAULT 0,
    def_takeaways   INTEGER DEFAULT 0,  -- ints + fumble recoveries
    def_fantasy_pts DOUBLE PRECISION,
    -- Rate helpers
    ypc             DOUBLE PRECISION,   -- rush yards per carry (when games_played>0)
    ypr             DOUBLE PRECISION,   -- receiving yards per reception
    -- Freshness
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (player_id, split_type, season_id)
);

-- Indexes for Earl chat research
CREATE INDEX IF NOT EXISTS idx_nfl_player_splits_player ON nfl.player_splits (player_id, split_type);
CREATE INDEX IF NOT EXISTS idx_nfl_player_splits_season ON nfl.player_splits (season_id);
