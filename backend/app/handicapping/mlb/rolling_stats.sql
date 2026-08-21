-- =============================================================================
-- Rolling Stats Tables for MLB Data Loader
-- =============================================================================
-- These tables pre-compute rolling window statistics so the data loader can
-- JOIN instead of re-computing everything in pandas every run.
--
-- Populate:
--   python -m backend.app.handicapping.mlb.populate_rolling
--
-- =============================================================================

-- ── Team Rolling Stats ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS mlb.team_rolling_stats (
    game_id          INTEGER NOT NULL,
    team_id          INTEGER NOT NULL,
    team_side        TEXT    NOT NULL CHECK (team_side IN ('home', 'away')),
    season_id        INTEGER NOT NULL,
    game_date        TIMESTAMPTZ NOT NULL,
    venue_id         INTEGER,

    -- Pre-computed game counters (replaces correlated subqueries in GAME_QUERY)
    home_games_sofar        INTEGER,
    away_games_sofar        INTEGER,
    game_away_venue_pct     DOUBLE PRECISION,

    -- Per-game context
    home_score       INTEGER,       -- final home team score
    away_score       INTEGER,       -- final away team score
    closing_ou       NUMERIC,       -- closing over/under line

    -- Per-game totals (derived from cumulative_game_stats)
    rf               INTEGER,       -- runs scored this game
    ra               INTEGER,       -- earned runs allowed this game
    hits             INTEGER,
    at_bats          INTEGER,
    walks            INTEGER,
    strikeouts       INTEGER,
    home_runs        INTEGER,
    total_bases      INTEGER,
    ip_outs          INTEGER,       -- IP in outs (3 outs = 1.0 IP)
    hits_allowed     INTEGER,
    walks_allowed    INTEGER,
    k_allowed        INTEGER,
    hr_allowed       INTEGER,

    -- Cumulative (season-to-date entering this game)
    cum_avg          DOUBLE PRECISION,
    cum_obp          DOUBLE PRECISION,
    cum_slg          DOUBLE PRECISION,
    cum_ops          DOUBLE PRECISION,
    cum_era          DOUBLE PRECISION,
    cum_whip         DOUBLE PRECISION,
    cum_k9           DOUBLE PRECISION,
    cum_bb9          DOUBLE PRECISION,
    cum_babip        DOUBLE PRECISION,
    cum_k_rate       DOUBLE PRECISION,
    cum_bb_rate      DOUBLE PRECISION,

    -- Rolling 5-game
    rf5              DOUBLE PRECISION,
    ra5              DOUBLE PRECISION,
    avg5             DOUBLE PRECISION,
    obp5             DOUBLE PRECISION,
    slg5             DOUBLE PRECISION,
    ops5             DOUBLE PRECISION,
    era5             DOUBLE PRECISION,
    whip5            DOUBLE PRECISION,
    k9_5             DOUBLE PRECISION,
    bb9_5            DOUBLE PRECISION,

    -- Rolling 10-game
    rf10             DOUBLE PRECISION,
    ra10             DOUBLE PRECISION,
    avg10            DOUBLE PRECISION,
    obp10            DOUBLE PRECISION,
    slg10            DOUBLE PRECISION,
    ops10            DOUBLE PRECISION,
    era10            DOUBLE PRECISION,
    whip10           DOUBLE PRECISION,
    k9_10            DOUBLE PRECISION,
    bb9_10           DOUBLE PRECISION,

    -- Rolling 15-game
    rf15             DOUBLE PRECISION,
    ra15             DOUBLE PRECISION,
    avg15            DOUBLE PRECISION,
    ops15            DOUBLE PRECISION,
    era15            DOUBLE PRECISION,
    whip15           DOUBLE PRECISION,

    -- Season record (entering this game)
    win_pct          DOUBLE PRECISION,
    spread_pct       DOUBLE PRECISION,
    over_pct         DOUBLE PRECISION,
    win_pct5         DOUBLE PRECISION,
    spread_pct5      DOUBLE PRECISION,
    over_pct5        DOUBLE PRECISION,
    win_pct10        DOUBLE PRECISION,
    over_pct10       DOUBLE PRECISION,
    win_pct15        DOUBLE PRECISION,
    over_pct15       DOUBLE PRECISION,

    -- Season expanding averages + last-10 W/L (entering this game, leak-safe)
    rf_avg           DOUBLE PRECISION,
    ra_avg           DOUBLE PRECISION,
    wins             INTEGER,
    losses           INTEGER,
    wins_l10         INTEGER,
    losses_l10       INTEGER,
    bullpen_ip_l5    INTEGER,
    bullpen_er_l5    DOUBLE PRECISION,
    venue_rf_r10     DOUBLE PRECISION,
    venue_win_pct_r10 DOUBLE PRECISION,

    PRIMARY KEY (game_id, team_side)
);

-- Ensure the expanding/l10 columns exist (idempotent for existing tables)
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS rf_avg       DOUBLE PRECISION;
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS ra_avg       DOUBLE PRECISION;
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS wins         INTEGER;
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS losses       INTEGER;
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS wins_l10     INTEGER;
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS losses_l10   INTEGER;
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS bullpen_ip_l5 INTEGER;
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS bullpen_er_l5 DOUBLE PRECISION;
-- Venue-conditional last-10 (only this team's games at this row's venue)
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS venue_rf_r10       DOUBLE PRECISION;
ALTER TABLE mlb.team_rolling_stats ADD COLUMN IF NOT EXISTS venue_win_pct_r10   DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_trs_team_season
    ON mlb.team_rolling_stats (team_id, season_id, game_date);
CREATE INDEX IF NOT EXISTS idx_trs_game
    ON mlb.team_rolling_stats (game_id);

-- ── Pitcher Rolling Stats ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS mlb.pitcher_rolling_stats (
    game_id          INTEGER NOT NULL,
    player_id        INTEGER NOT NULL,
    team_id          INTEGER NOT NULL,
    team_abbr        TEXT    NOT NULL,
    season_id        INTEGER NOT NULL,
    game_date        TIMESTAMPTZ NOT NULL,
    is_starter       BOOLEAN NOT NULL DEFAULT true,

    -- Per-start totals
    ip_outs          INTEGER,
    er               INTEGER,
    hits_allowed     INTEGER,
    walks_allowed    INTEGER,
    strikeouts       INTEGER,
    home_runs_allowed INTEGER,

    -- Per-start derived
    era_this_start   DOUBLE PRECISION,
    whip_this_start  DOUBLE PRECISION,
    k9_this_start    DOUBLE PRECISION,
    bb9_this_start   DOUBLE PRECISION,
    is_quality_start BOOLEAN,

    -- Cumulative (season-to-date entering this start)
    era_ytd          DOUBLE PRECISION,
    whip_ytd         DOUBLE PRECISION,
    k9_ytd           DOUBLE PRECISION,
    bb9_ytd          DOUBLE PRECISION,
    kbb_ytd          DOUBLE PRECISION,
    fip_ytd          DOUBLE PRECISION,
    qs_rate_ytd      DOUBLE PRECISION,
    starts_ytd       INTEGER,

    -- 5-start rolling
    era_5            DOUBLE PRECISION,
    whip_5           DOUBLE PRECISION,
    k9_5             DOUBLE PRECISION,
    bb9_5            DOUBLE PRECISION,
    kbb_5            DOUBLE PRECISION,

    -- 10-start rolling
    era_10           DOUBLE PRECISION,
    whip_10          DOUBLE PRECISION,
    k9_10            DOUBLE PRECISION,
    bb9_10           DOUBLE PRECISION,
    kbb_10           DOUBLE PRECISION,

    -- 15-start rolling
    era_15           DOUBLE PRECISION,
    whip_15          DOUBLE PRECISION,
    k9_15            DOUBLE PRECISION,
    bb9_15           DOUBLE PRECISION,

    PRIMARY KEY (game_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_prs_pitcher_season
    ON mlb.pitcher_rolling_stats (player_id, season_id, game_date);
CREATE INDEX IF NOT EXISTS idx_prs_game
    ON mlb.pitcher_rolling_stats (game_id);
CREATE INDEX IF NOT EXISTS idx_prs_starter_game
    ON mlb.pitcher_rolling_stats (game_id)
    WHERE is_starter = true;


-- ── Player Batting Rolling Stats ──────────────────────────────────────────
-- One row per (player, game) the batter appeared in a FINAL game.
-- Rolling windows use ROWS BETWEEN ... AND CURRENT ROW, so each row INCLUDES
-- its own game's result (matches the team_rolling_stats contract). The data
-- loader reads the PREVIOUS FINAL row per player for any live/final target.
--
-- OPS inputs here: avg/obp/slg derived from per-game H/PA(AB+BB+HBP+SF)/TB.

CREATE TABLE IF NOT EXISTS mlb.player_batting_rolling_stats (
    game_id        INTEGER NOT NULL,
    player_id      INTEGER NOT NULL,
    team_id        INTEGER,
    team_side      TEXT    NOT NULL CHECK (team_side IN ('home', 'away')),
    season_id      INTEGER NOT NULL,
    game_date        TIMESTAMPTZ NOT NULL,
    game_n         INTEGER,        -- Nth game this season for this player

    -- Per-game counting stats (OPS plate-appearance inputs)
    pa             INTEGER,
    at_bats        INTEGER,
    runs           INTEGER,
    hits           INTEGER,
    doubles        INTEGER,
    triples        INTEGER,
    home_runs      INTEGER,
    runs_batted_in INTEGER,
    walks          INTEGER,        -- base_on_balls
    strikeouts     INTEGER,
    total_bases    INTEGER,
    hit_by_pitch   INTEGER,
    sacrifice_flies INTEGER,

    -- Per-game rates
    avg_this   DOUBLE PRECISION,
    obp_this   DOUBLE PRECISION,
    slg_this   DOUBLE PRECISION,
    ops_this   DOUBLE PRECISION,

    -- Season-to-date (entering incl. this game, CURRENT ROW)
    ytd_games   INTEGER,
    ytd_pa      INTEGER,
    ytd_ab      INTEGER,
    ytd_hits    INTEGER,
    ytd_bb      INTEGER,
    ytd_hbp     INTEGER,
    ytd_sf      INTEGER,
    ytd_runs    INTEGER,
    ytd_rbi     INTEGER,
    ytd_tb      INTEGER,
    ytd_hr      INTEGER,
    ytd_so      INTEGER,
    ytd_avg     DOUBLE PRECISION,
    ytd_obp     DOUBLE PRECISION,
    ytd_slg     DOUBLE PRECISION,
    ytd_ops     DOUBLE PRECISION,

    -- Rolling 5-game (CURRENT ROW inclusive)
    avg_5       DOUBLE PRECISION,
    obp_5       DOUBLE PRECISION,
    slg_5       DOUBLE PRECISION,
    ops_5       DOUBLE PRECISION,

    -- Rolling 15-game
    avg_15      DOUBLE PRECISION,
    obp_15      DOUBLE PRECISION,
    slg_15      DOUBLE PRECISION,
    ops_15      DOUBLE PRECISION,

    -- Rolling 30-game
    avg_30      DOUBLE PRECISION,
    obp_30      DOUBLE PRECISION,
    slg_30      DOUBLE PRECISION,
    ops_30      DOUBLE PRECISION,

    PRIMARY KEY (player_id, game_id)
);

CREATE INDEX IF NOT EXISTS idx_pbrs_player_season
    ON mlb.player_batting_rolling_stats (player_id, season_id, game_date);
CREATE INDEX IF NOT EXISTS idx_pbrs_game
    ON mlb.player_batting_rolling_stats (game_id);
