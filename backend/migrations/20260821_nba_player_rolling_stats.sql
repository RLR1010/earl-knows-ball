-- 2026-08-21 NBA player_rolling_stats — mirror of mlb.player_batting_rolling_stats
-- One row per (player_id, game_id) — pre-computed per-player season-to-date cumulative
-- and rolling-window averages so the NBA data loader / engine can JOIN per-player
-- features instead of recomputing them from nba.player_game_stats (528k rows) per run.
--
-- Source: nba.player_game_stats (raw boxscore per player-game).
-- Conventions (match MLB / our prev_game_id work):
--   * Column order is NULLABLE; a player had no prior game -> prior pointers are NULL.
--   * game_date is the US EASTERN calendar date (see TOOLS.md timezone note).
--   * "entering this game" semantics: each row carries stats as of the PRIOR game
--     (so no look-ahead: reading a row for game G gives you stats from games BEFORE G).
--     For cumulative fields we store the running total INCLUDING prior games only
--     (the player's own game-G row is NOT included in its cumulative).

CREATE TABLE IF NOT EXISTS nba.player_rolling_stats (

    -- keys
    player_id          INTEGER NOT NULL,
    game_id            INTEGER NOT NULL,
    team_id            INTEGER,
    season_id          INTEGER,
    game_date          DATE,
    is_starter         BOOLEAN,
    position           TEXT,

    -- prior-game pointers (LAG; NULL on the player's first appearance)
    prev_game_id            INTEGER,
    prev_game_date          DATE,
    prev_game_id_season     INTEGER,
    prev_game_date_season   DATE,

    -- ── Per-game raw (this player's game, for joins/debug) ──────────────
    minutes_txt        TEXT,
    minutes            DOUBLE PRECISION,   -- decimal minutes
    points             INTEGER,
    rebounds_offensive INTEGER,
    rebounds_defensive INTEGER,
    rebounds_total     INTEGER,
    assists            INTEGER,
    steals             INTEGER,
    blocks             INTEGER,
    turnovers          INTEGER,
    fouls_personal     INTEGER,
    plus_minus         DOUBLE PRECISION,
    fantasy_points     DOUBLE PRECISION,
    fgm                INTEGER,
    fga                INTEGER,
    fg_pct             DOUBLE PRECISION,
    tpm                INTEGER,
    tpa                INTEGER,
    tp_pct             DOUBLE PRECISION,
    ftm                INTEGER,
    fta                INTEGER,
    ft_pct             DOUBLE PRECISION,

    -- ── Season-to-date cumulative (ENTERING this game; excludes this game) ──
    cum_games          INTEGER,            -- prior games played this season
    cum_points         INTEGER,
    cum_rebounds       INTEGER,
    cum_assists        INTEGER,
    cum_minutes        DOUBLE PRECISION,
    cum_ppg            DOUBLE PRECISION,   -- cumulative PPG entering game
    cum_rpg            DOUBLE PRECISION,
    cum_apg            DOUBLE PRECISION,
    cum_mpg            DOUBLE PRECISION,
    cum_fg_pct         DOUBLE PRECISION,
    cum_tp_pct         DOUBLE PRECISION,
    cum_ft_pct         DOUBLE PRECISION,

    -- ── Rolling windows (ENTERING this game; last N prior games) ────────
    ppg_5              DOUBLE PRECISION,
    ppg_10             DOUBLE PRECISION,
    ppg_15             DOUBLE PRECISION,
    ppg_30             DOUBLE PRECISION,
    rpg_5              DOUBLE PRECISION,
    apg_5              DOUBLE PRECISION,
    mpg_5              DOUBLE PRECISION,
    spg_5              DOUBLE PRECISION,
    bpg_5              DOUBLE PRECISION,
    tpg_5              DOUBLE PRECISION,
    fg_pct_5           DOUBLE PRECISION,
    tp_pct_5           DOUBLE PRECISION,
    ft_pct_5           DOUBLE PRECISION,
    plus_minus_5       DOUBLE PRECISION,
    gp_5               INTEGER,            -- games played in last 5 (for availability)

    PRIMARY KEY (player_id, game_id)
);

-- indexes for the loader/engine joins + prior-pointer O(1) lookups
CREATE INDEX IF NOT EXISTS idx_nba_prs_player_game  ON nba.player_rolling_stats (player_id, game_id);
CREATE INDEX IF NOT EXISTS idx_nba_prs_game         ON nba.player_rolling_stats (game_id);
CREATE INDEX IF NOT EXISTS idx_nba_prs_pg           ON nba.player_rolling_stats (prev_game_id);
CREATE INDEX IF NOT EXISTS idx_nba_prs_pg_season    ON nba.player_rolling_stats (prev_game_id_season);
CREATE INDEX IF NOT EXISTS idx_nba_prs_team_date    ON nba.player_rolling_stats (team_id, game_date DESC, season_id);
