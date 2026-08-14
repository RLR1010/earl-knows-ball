-- nba.prior_team_stats: final previous-season aggregates per team-season.
--
-- Mirrors mlb.prior_team_stats / nfl.prior_team_stats so the NBA data loader can
-- blend backward-looking features with the prior season for the first games of a
-- new season (fixes blank/0 features on opening-night games like 37960).
--
-- One row per (team, season_year). Each column holds the season-FINAL value from
-- the corresponding nba.cumulative_game_stats / nba.team_rolling_stats source row
-- (latest game per team-season). Column names here MATCH the source feature names
-- so the blend map in nba/data_loader.py stays a 1:1 suffix mapping.
--
-- The populator lives in backend/scripts/rebuild_nba_prior_team_stats.py.

CREATE TABLE IF NOT EXISTS nba.prior_team_stats (
    id                BIGSERIAL PRIMARY KEY,
    team_id           INTEGER NOT NULL,
    team_abbr         TEXT    NOT NULL,
    season_year       INTEGER NOT NULL,          -- the season these stats describe
    games_played      INTEGER,

    -- ── Cumulative / season-to-date finals (from cumulative_game_stats) ──
    cum_ppg              NUMERIC(8,2),
    cum_oppg             NUMERIC(8,2),
    cum_margin_pg        NUMERIC(8,2),
    cum_fg_pct           NUMERIC(8,2),
    cum_fg3_pct          NUMERIC(8,2),
    cum_ft_pct           NUMERIC(8,2),
    cum_reb_pg           NUMERIC(8,2),
    cum_ast_pg           NUMERIC(8,2),
    cum_stl_pg           NUMERIC(8,2),
    cum_blk_pg           NUMERIC(8,2),
    cum_tov_pg           NUMERIC(8,2),
    cum_pf_pg            NUMERIC(8,2),
    cum_ortg             NUMERIC(8,2),
    cum_drtg             NUMERIC(8,2),
    cum_net_ortg         NUMERIC(8,2),
    cum_pace             NUMERIC(8,2),
    cum_efg_pct          NUMERIC(8,2),
    cum_opp_efg_pct      NUMERIC(8,2),
    cum_tov_rate         NUMERIC(8,2),
    cum_opp_tov_rate     NUMERIC(8,2),
    cum_ft_rate          NUMERIC(8,2),
    cum_3pa_rate         NUMERIC(8,2),
    cum_ast_ratio        NUMERIC(8,2),
    cum_stl_rate         NUMERIC(8,2),
    cum_blk_rate         NUMERIC(8,2),
    cum_win_pct          NUMERIC(8,2),

    -- ── Rolling / momentum finals (from team_rolling_stats, last window) ──
    rw3_ppg              NUMERIC(8,2),
    rw5_ppg              NUMERIC(8,2),
    rw3_net_rtg          NUMERIC(8,2),
    rw5_net_rtg          NUMERIC(8,2),
    rw3_efg_pct          NUMERIC(8,2),
    rw5_efg_pct          NUMERIC(8,2),
    rw3_drtg             NUMERIC(8,2),
    rw5_drtg             NUMERIC(8,2),
    cv10_ppg             NUMERIC(8,2),
    cv20_ppg             NUMERIC(8,2),
    cv10_net_rtg         NUMERIC(8,2),
    recency_ppg          NUMERIC(8,2),
    recency_net_rtg      NUMERIC(8,2),
    net_rtg_r5           NUMERIC(8,2),
    net_rtg_r10          NUMERIC(8,2),
    ortg_r5              NUMERIC(8,2),
    ortg_r10             NUMERIC(8,2),
    drtg_r5              NUMERIC(8,2),
    drtg_r10             NUMERIC(8,2),
    efg_r5               NUMERIC(8,2),
    efg_r10              NUMERIC(8,2),
    pace_r5              NUMERIC(8,2),
    pace_r10             NUMERIC(8,2),
    ast_ratio_r5         NUMERIC(8,2),
    ast_ratio_r10        NUMERIC(8,2),
    ft_rate_r5           NUMERIC(8,2),
    ft_rate_r10          NUMERIC(8,2),
    threep_rate_r5       NUMERIC(8,2),
    threep_rate_r10      NUMERIC(8,2),

    -- ── Betting form finals ──
    ats_margin_5         NUMERIC(8,2),
    ats_margin_10        NUMERIC(8,2),
    ats_wins_5           INTEGER,
    ats_wins_10          INTEGER,
    ou_wins_5            INTEGER,
    ou_wins_10           INTEGER,
    ou_margin_5          NUMERIC(8,2),
    ou_margin_10         NUMERIC(8,2),
    wins_5               INTEGER,
    wins_10              INTEGER,
    adj_off_10           NUMERIC(8,2),
    adj_def_10           NUMERIC(8,2),

    -- ── Star player finals ──
    star_ppg_5           NUMERIC(8,2),
    star1_ppg_5          NUMERIC(8,2),
    stars_active         NUMERIC(5,2),
    star1_active         NUMERIC(5,2),

    UNIQUE (team_id, season_year)
);

CREATE INDEX IF NOT EXISTS idx_nba_prior_team_stats_team_year
    ON nba.prior_team_stats (team_id, season_year);
CREATE INDEX IF NOT EXISTS idx_nba_prior_team_stats_abbr_year
    ON nba.prior_team_stats (team_abbr, season_year);
