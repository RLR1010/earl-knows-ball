"""
Populate mlb.team_rolling_stats and mlb.pitcher_rolling_stats.

These tables pre-compute rolling window statistics from cumulative_game_stats
and pitcher_game_stats so the data loader can JOIN instead of re-computing
everything in pandas every run.

Run:  python -m backend.app.handicapping.mlb.populate_rolling_stats

One-time backfill:  processes all historical games.
Incremental:        pass --incremental to skip games already in the tables.
"""

import argparse
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# ── Schema ───────────────────────────────────────────────────────────────────

CREATE_TEAM_ROLLING_STATS_SQL = """
CREATE TABLE IF NOT EXISTS mlb.team_rolling_stats (
    game_id         INTEGER NOT NULL,
    team_id         INTEGER NOT NULL,
    team_side       TEXT    NOT NULL CHECK (team_side IN ('home', 'away')),
    season_id       INTEGER NOT NULL,
    game_date       DATE    NOT NULL,

    -- Per-team per-game event totals (derived from cumulative_game_stats LAG)
    rf              INTEGER,       -- runs scored this game
    ra              INTEGER,       -- runs allowed this game (earned)
    hits            INTEGER,
    at_bats         INTEGER,
    walks           INTEGER,
    strikeouts      INTEGER,
    home_runs       INTEGER,
    total_bases     INTEGER,
    ip_outs         INTEGER,       -- IP expressed in outs (3 outs = 1 IP)
    hits_allowed    INTEGER,
    walks_allowed   INTEGER,
    k_allowed       INTEGER,
    hr_allowed      INTEGER,

    -- Cumulative (season-to-date entering this game)
    cum_avg         DOUBLE PRECISION,
    cum_obp         DOUBLE PRECISION,
    cum_slg         DOUBLE PRECISION,
    cum_ops         DOUBLE PRECISION,
    cum_era         DOUBLE PRECISION,
    cum_whip        DOUBLE PRECISION,
    cum_k9          DOUBLE PRECISION,
    cum_bb9         DOUBLE PRECISION,
    cum_babip       DOUBLE PRECISION,
    cum_k_rate      DOUBLE PRECISION,
    cum_bb_rate     DOUBLE PRECISION,

    -- 5-game rolling averages
    rf5             DOUBLE PRECISION,
    ra5             DOUBLE PRECISION,
    avg5            DOUBLE PRECISION,
    obp5            DOUBLE PRECISION,
    slg5            DOUBLE PRECISION,
    ops5            DOUBLE PRECISION,
    era5            DOUBLE PRECISION,
    whip5           DOUBLE PRECISION,
    k9_5            DOUBLE PRECISION,
    bb9_5           DOUBLE PRECISION,

    -- 10-game rolling averages
    rf10            DOUBLE PRECISION,
    ra10            DOUBLE PRECISION,
    avg10           DOUBLE PRECISION,
    obp10           DOUBLE PRECISION,
    slg10           DOUBLE PRECISION,
    ops10           DOUBLE PRECISION,
    era10           DOUBLE PRECISION,
    whip10          DOUBLE PRECISION,
    k9_10           DOUBLE PRECISION,
    bb9_10          DOUBLE PRECISION,

    -- 15-game rolling averages
    rf15            DOUBLE PRECISION,
    ra15            DOUBLE PRECISION,
    avg15           DOUBLE PRECISION,
    ops15           DOUBLE PRECISION,
    era15           DOUBLE PRECISION,
    whip15          DOUBLE PRECISION,

    -- Win / ATS / OU record (season-to-date)
    win_pct         DOUBLE PRECISION,
    spread_pct      DOUBLE PRECISION,
    over_pct        DOUBLE PRECISION,

    PRIMARY KEY (game_id, team_side)
);
"""

CREATE_TEAM_ROLLING_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_t_rolling_team_season
    ON mlb.team_rolling_stats (team_id, season_id, game_date);
CREATE INDEX IF NOT EXISTS idx_t_rolling_game_id
    ON mlb.team_rolling_stats (game_id);
"""

CREATE_PITCHER_ROLLING_STATS_SQL = """
CREATE TABLE IF NOT EXISTS mlb.pitcher_rolling_stats (
    game_id         INTEGER NOT NULL,
    player_id       INTEGER NOT NULL,
    team_id         INTEGER NOT NULL,
    team_abbr       TEXT    NOT NULL,
    season_id       INTEGER NOT NULL,
    game_date       DATE    NOT NULL,
    is_starter      BOOLEAN NOT NULL DEFAULT true,

    -- Per-start raw totals
    ip_outs         INTEGER,       -- IP in outs (3 outs = 1 IP)
    er              INTEGER,
    hits_allowed    INTEGER,
    walks_allowed   INTEGER,
    strikeouts      INTEGER,
    home_runs_allowed INTEGER,

    -- Per-start derived
    era_this_start  DOUBLE PRECISION,
    whip_this_start DOUBLE PRECISION,
    k9_this_start   DOUBLE PRECISION,
    bb9_this_start  DOUBLE PRECISION,
    is_quality_start BOOLEAN,

    -- Cumulative (season-to-date entering this start)
    era_ytd         DOUBLE PRECISION,
    whip_ytd        DOUBLE PRECISION,
    k9_ytd          DOUBLE PRECISION,
    bb9_ytd         DOUBLE PRECISION,
    kbb_ytd         DOUBLE PRECISION,
    fip_ytd         DOUBLE PRECISION,
    qs_rate_ytd     DOUBLE PRECISION,
    starts_ytd      INTEGER,

    -- 5-start rolling
    era_5           DOUBLE PRECISION,
    whip_5          DOUBLE PRECISION,
    k9_5            DOUBLE PRECISION,
    bb9_5           DOUBLE PRECISION,
    kbb_5           DOUBLE PRECISION,

    -- 10-start rolling
    era_10          DOUBLE PRECISION,
    whip_10         DOUBLE PRECISION,
    k9_10           DOUBLE PRECISION,
    bb9_10          DOUBLE PRECISION,
    kbb_10          DOUBLE PRECISION,

    -- 15-start rolling
    era_15          DOUBLE PRECISION,
    whip_15         DOUBLE PRECISION,
    k9_15           DOUBLE PRECISION,
    bb9_15          DOUBLE PRECISION,
    -- 20-start rolling averages
    era_20          DOUBLE PRECISION,
    whip_20         DOUBLE PRECISION,
    k9_20           DOUBLE PRECISION,
    bb9_20          DOUBLE PRECISION,
    kbb_20          DOUBLE PRECISION,

    -- Rest days since last start
    rest_days       INTEGER,

    -- Split ERA (cumulative YTD, expanding mean shift(1))
    home_era_ytd    DOUBLE PRECISION,
    road_era_ytd    DOUBLE PRECISION,
    day_era_ytd     DOUBLE PRECISION,
    night_era_ytd   DOUBLE PRECISION,

    PRIMARY KEY (game_id, player_id)
);
"""

CREATE_PITCHER_ROLLING_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_p_rolling_pitcher_season
    ON mlb.pitcher_rolling_stats (player_id, season_id, game_date);
CREATE INDEX IF NOT EXISTS idx_p_rolling_game_id
    ON mlb.pitcher_rolling_stats (game_id);
CREATE INDEX IF NOT EXISTS idx_p_rolling_starter_game
    ON mlb.pitcher_rolling_stats (game_id, is_starter)
    WHERE is_starter = true;
"""

# ── Populate team_rolling_stats ─────────────────────────────────────────────

POPULATE_TEAM_ROLLING_SQL = """
WITH per_game AS (
    SELECT
        cgs.game_id,
        cgs.team_id,
        cgs.team_side,
        cgs.season_id,
        cgs.game_date,
        g.home_team_id = cgs.team_id AS is_home,
        g.home_score,
        g.away_score,

        -- Per-game batting totals (subtract previous cumulative)
        cgs.bat_runs - COALESCE(
            LAG(cgs.bat_runs) OVER w, 0
        ) AS rf,
        cgs.bat_hits - COALESCE(
            LAG(cgs.bat_hits) OVER w, 0
        ) AS hits,
        cgs.bat_at_bats - COALESCE(
            LAG(cgs.bat_at_bats) OVER w, 0
        ) AS at_bats,
        cgs.bat_walks - COALESCE(
            LAG(cgs.bat_walks) OVER w, 0
        ) AS walks,
        cgs.bat_strikeouts - COALESCE(
            LAG(cgs.bat_strikeouts) OVER w, 0
        ) AS strikeouts,
        cgs.bat_home_runs - COALESCE(
            LAG(cgs.bat_home_runs) OVER w, 0
        ) AS home_runs,
        cgs.bat_total_bases - COALESCE(
            LAG(cgs.bat_total_bases) OVER w, 0
        ) AS total_bases,

        -- Cumulative batting (season to date entering this game)
        cgs.cum_avg,
        cgs.cum_obp,
        cgs.cum_slg,
        cgs.cum_ops,
        cgs.cum_babip,
        cgs.cum_k_rate,
        cgs.cum_bb_rate,

        -- Per-game pitching totals
        cgs.pitch_ip - COALESCE(
            LAG(cgs.pitch_ip) OVER w, 0
        ) AS ip_outs,
        cgs.pitch_er - COALESCE(
            LAG(cgs.pitch_er) OVER w, 0
        ) AS ra,
        cgs.pitch_hits_allowed - COALESCE(
            LAG(cgs.pitch_hits_allowed) OVER w, 0
        ) AS hits_allowed,
        cgs.pitch_walks_allowed - COALESCE(
            LAG(cgs.pitch_walks_allowed) OVER w, 0
        ) AS walks_allowed,
        cgs.pitch_strikeouts - COALESCE(
            LAG(cgs.pitch_strikeouts) OVER w, 0
        ) AS k_allowed,
        cgs.pitch_home_runs_allowed - COALESCE(
            LAG(cgs.pitch_home_runs_allowed) OVER w, 0
        ) AS hr_allowed,

        -- Cumulative pitching
        cgs.cum_era,
        cgs.cum_whip,
        cgs.cum_k9,
        cgs.cum_bb9,

        -- Season record (win / ats / over)
        gw.wins::DOUBLE PRECISION / NULLIF(gw.games_played, 0) AS win_pct,
        gw.ats_wins::DOUBLE PRECISION / NULLIF(gw.ats_games, 0) AS spread_pct,
        gw.over_games::DOUBLE PRECISION / NULLIF(gw.over_total, 0) AS over_pct,

        ROW_NUMBER() OVER (PARTITION BY cgs.team_id, cgs.season_id ORDER BY cgs.game_date, cgs.game_id) AS game_n

    FROM mlb.cumulative_game_stats cgs
    JOIN mlb.games g ON g.id = cgs.game_id
    LEFT JOIN (
        SELECT
            cgs2.team_id,
            cgs2.season_id,
            COUNT(*) AS games_played,
            SUM(CASE WHEN g2.home_team_id = cgs2.team_id AND g2.home_score > g2.away_score
                      OR g2.away_team_id = cgs2.team_id AND g2.away_score > g2.home_score
                 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN g2.home_team_id = cgs2.team_id AND g2.home_score > g2.away_score
                      OR g2.away_team_id = cgs2.team_id AND g2.away_score > g2.home_score
                 THEN 1 ELSE 0 END) AS ats_wins,
            COUNT(*) AS ats_games,
            SUM(CASE WHEN (g2.home_score + g2.away_score) > blc.closing_ou THEN 1
                     WHEN (g2.home_score + g2.away_score) < blc.closing_ou THEN 0
                     ELSE NULL END) AS over_games,
            COUNT(*) FILTER (WHERE blc.closing_ou IS NOT NULL) AS over_total
        FROM mlb.cumulative_game_stats cgs2
        JOIN mlb.games g2 ON g2.id = cgs2.game_id
        LEFT JOIN mlb.betting_lines_consolidated blc ON blc.game_id = g2.id
        WHERE g2.status = 'FINAL'
        GROUP BY cgs2.team_id, cgs2.season_id
    ) gw ON gw.team_id = cgs.team_id AND gw.season_id = cgs.season_id

    WINDOW w AS (PARTITION BY cgs.team_id, cgs.season_id ORDER BY cgs.game_date, cgs.game_id)
)
SELECT *,
    -- 5-game rolling averages
    AVG(rf)  FILTER (WHERE rf IS NOT NULL)
        OVER (PARTITION BY team_id, season_id ORDER BY game_date, game_id
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS rf5,
    AVG(ra)  FILTER (WHERE ra IS NOT NULL)
        OVER (PARTITION BY team_id, season_id ORDER BY game_date, game_id
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS ra5,
    SUM(hits)  FILTER (WHERE hits IS NOT NULL)::DOUBLE PRECISION /
        NULLIF(SUM(NULLIF(at_bats, 0)) FILTER (WHERE hits IS NOT NULL)
               OVER (PARTITION BY team_id, season_id ORDER BY game_date, game_id
                     ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING), 0) AS avg5,
    SUM(total_bases + walks + (0))::DOUBLE PRECISION /  -- simplified obp numerator
        NULLIF(SUM(NULLIF(at_bats + walks, 0)) 
               OVER (PARTITION BY team_id, season_id ORDER BY game_date, game_id
                     ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING), 0) AS obp5,
    -- AVG(hits) / AVG(at_bats) over 5 games - correct approach
    AVG(hits::DOUBLE PRECISION / NULLIF(NULLIF(at_bats, 0), 0)) FILTER (WHERE at_bats > 0)
        OVER (PARTITION BY team_id, season_id ORDER BY game_date, game_id
              ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS avg5_simple,

    -- 10-game rolling
    AVG(rf)  FILTER (WHERE rf IS NOT NULL)
        OVER (PARTITION BY team_id, season_id ORDER BY game_date, game_id
              ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING) AS rf10,
    AVG(ra)  FILTER (WHERE ra IS NOT NULL)
        OVER (PARTITION BY team_id, season_id ORDER BY game_date, game_id
              ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING) AS ra10,

    -- 15-game rolling
    AVG(rf)  FILTER (WHERE rf IS NOT NULL)
        OVER (PARTITION BY team_id, season_id ORDER BY game_date, game_id
              ROWS BETWEEN 15 PRECEDING AND 1 PRECEDING) AS rf15,
    AVG(ra)  FILTER (WHERE ra IS NOT NULL)
        OVER (PARTITION BY team_id, season_id ORDER BY game_date, game_id
              ROWS BETWEEN 15 PRECEDING AND 1 PRECEDING) AS ra15

FROM per_game
ORDER BY team_id, season_id, game_date, game_id
;
"""

# ── Populate pitcher_rolling_stats ───────────────────────────────────────────

POPULATE_PITCHER_ROLLING_SQL = """
WITH per_start AS (
    SELECT
        pgs.game_id,
        pgs.pitcher_mlb_id AS player_id,
        t.id AS team_id,
        pgs.team_abbr,
        g.season_id,
        g.date AS game_date,
        pgs.is_starter,
        TRUE AS is_starter_filter,

        -- Raw totals
        pgs.ip,
        -- ip_outs is computed below from IP string (e.g. 6.1 → 19 outs)
        pgs.er,
        pgs.h AS hits_allowed,
        pgs.bb AS walks_allowed,
        pgs.k AS strikeouts,
        pgs.hr AS home_runs_allowed,
        pgs.hit_by_pitch,
        g.day_night,
        (g.home_team_id = t.id) AS is_home,
        
        -- IP in outs (for division)
        (COALESCE(NULLIF(SPLIT_PART(pgs.ip::TEXT, '.', 1), ''), '0')::INTEGER * 3
         + COALESCE(NULLIF(SPLIT_PART(pgs.ip::TEXT, '.', 2), ''), '0')::INTEGER) AS ip_outs,

        ROW_NUMBER() OVER (
            PARTITION BY pgs.pitcher_mlb_id, g.season_id
            ORDER BY g.date, pgs.game_id
        ) AS start_n

    FROM mlb.pitcher_game_stats pgs
    JOIN mlb.games g ON g.id = pgs.game_id
    JOIN mlb.teams t ON t.abbreviation = pgs.team_abbr
    WHERE pgs.is_starter = TRUE
)
, with_derived AS (
    SELECT *,
        LAG(game_date) OVER (
            PARTITION BY player_id, season_id
            ORDER BY game_date, game_id
        ) AS prev_start_date,
        -- Per-game derived stats (multiply by 9 for rate stats)
        CASE WHEN ip_outs > 0
            THEN 9.0 * er::DOUBLE PRECISION / (ip_outs::DOUBLE PRECISION / 3)
            ELSE NULL END AS era_this_start,
        CASE WHEN ip_outs > 0
            THEN (hits_allowed + walks_allowed)::DOUBLE PRECISION / (ip_outs::DOUBLE PRECISION / 3)
            ELSE NULL END AS whip_this_start,
        CASE WHEN ip_outs > 0
            THEN strikeouts::DOUBLE PRECISION / (ip_outs::DOUBLE PRECISION / 3) * 9
            ELSE NULL END AS k9_this_start,
        CASE WHEN ip_outs > 0
            THEN walks_allowed::DOUBLE PRECISION / (ip_outs::DOUBLE PRECISION / 3) * 9
            ELSE NULL END AS bb9_this_start,
        CASE WHEN ip_outs >= 18 AND er <= 3 THEN TRUE ELSE FALSE END AS is_quality_start

    FROM per_start
)
SELECT *,
    -- Cumulative (season to date, excluding this start)
    9.0 * SUM(er) OVER w / NULLIF(SUM(ip_outs) OVER w / 3.0, 0) AS era_ytd,
    (SUM(hits_allowed) OVER w + SUM(walks_allowed) OVER w)::DOUBLE PRECISION
        / NULLIF(SUM(ip_outs) OVER w / 3.0, 0) AS whip_ytd,
    SUM(strikeouts) OVER w / NULLIF(SUM(ip_outs) OVER w / 3.0, 0) * 9 AS k9_ytd,
    SUM(walks_allowed) OVER w / NULLIF(SUM(ip_outs) OVER w / 3.0, 0) * 9 AS bb9_ytd,
    SUM(strikeouts) OVER w::DOUBLE PRECISION / NULLIF(SUM(walks_allowed) OVER w, 0) AS kbb_ytd,
    -- FIP simplified: (13*HR + 3*BB - 2*K) / IP + constant (use 3.10 as FIP constant)
    (13.0 * SUM(home_runs_allowed) OVER w + 3.0 * SUM(walks_allowed) OVER w - 2.0 * SUM(strikeouts) OVER w)
        / NULLIF(SUM(ip_outs) OVER w / 3.0, 0) + 3.10 AS fip_ytd,
    SUM(CASE WHEN is_quality_start THEN 1 ELSE 0 END) OVER w::DOUBLE PRECISION
        / NULLIF(COUNT(*) OVER w::DOUBLE PRECISION, 0) AS qs_rate_ytd,
    COUNT(*) OVER w AS starts_ytd,

    -- 5-start rolling
    9.0 * SUM(er) OVER w5 / NULLIF(SUM(ip_outs) OVER w5 / 3.0, 0) AS era_5,
    (SUM(hits_allowed) OVER w5 + SUM(walks_allowed) OVER w5)::DOUBLE PRECISION
        / NULLIF(SUM(ip_outs) OVER w5 / 3.0, 0) AS whip_5,
    SUM(strikeouts) OVER w5 / NULLIF(SUM(ip_outs) OVER w5 / 3.0, 0) * 9 AS k9_5,
    SUM(walks_allowed) OVER w5 / NULLIF(SUM(ip_outs) OVER w5 / 3.0, 0) * 9 AS bb9_5,
    SUM(strikeouts) OVER w5::DOUBLE PRECISION / NULLIF(SUM(walks_allowed) OVER w5, 0) AS kbb_5,

    -- 10-start rolling
    9.0 * SUM(er) OVER w10 / NULLIF(SUM(ip_outs) OVER w10 / 3.0, 0) AS era_10,
    (SUM(hits_allowed) OVER w10 + SUM(walks_allowed) OVER w10)::DOUBLE PRECISION
        / NULLIF(SUM(ip_outs) OVER w10 / 3.0, 0) AS whip_10,
    SUM(strikeouts) OVER w10 / NULLIF(SUM(ip_outs) OVER w10 / 3.0, 0) * 9 AS k9_10,
    SUM(walks_allowed) OVER w10 / NULLIF(SUM(ip_outs) OVER w10 / 3.0, 0) * 9 AS bb9_10,
    SUM(strikeouts) OVER w10::DOUBLE PRECISION / NULLIF(SUM(walks_allowed) OVER w10, 0) AS kbb_10,

    -- 15-start rolling
    9.0 * SUM(er) OVER w15 / NULLIF(SUM(ip_outs) OVER w15 / 3.0, 0) AS era_15,
    (SUM(hits_allowed) OVER w15 + SUM(walks_allowed) OVER w15)::DOUBLE PRECISION
        / NULLIF(SUM(ip_outs) OVER w15 / 3.0, 0) AS whip_15,
    SUM(strikeouts) OVER w15 / NULLIF(SUM(ip_outs) OVER w15 / 3.0, 0) * 9 AS k9_15,
    SUM(walks_allowed) OVER w15 / NULLIF(SUM(ip_outs) OVER w15 / 3.0, 0) * 9 AS bb9_15,


    -- 20-start rolling
    9.0 * SUM(er) OVER w20 / NULLIF(SUM(ip_outs) OVER w20 / 3.0, 0) AS era_20,
    (SUM(hits_allowed) OVER w20 + SUM(walks_allowed) OVER w20)::DOUBLE PRECISION
        / NULLIF(SUM(ip_outs) OVER w20 / 3.0, 0) AS whip_20,
    SUM(strikeouts) OVER w20 / NULLIF(SUM(ip_outs) OVER w20 / 3.0, 0) * 9 AS k9_20,
    SUM(walks_allowed) OVER w20 / NULLIF(SUM(ip_outs) OVER w20 / 3.0, 0) * 9 AS bb9_20,
    SUM(strikeouts) OVER w20::DOUBLE PRECISION / NULLIF(SUM(walks_allowed) OVER w20, 0) AS kbb_20,

    -- Rest days since last start
    CASE WHEN prev_start_date IS NOT NULL
        THEN EXTRACT(DAY FROM game_date - prev_start_date)::INTEGER
        ELSE NULL END AS rest_days,

    -- Split ERA (cumulative YTD, expanding mean, shift(1))
    9.0 * SUM(CASE WHEN is_home THEN er ELSE 0 END) OVER w
        / NULLIF(SUM(CASE WHEN is_home THEN ip_outs ELSE 0 END) OVER w / 3.0, 0) AS home_era_ytd,
    9.0 * SUM(CASE WHEN NOT is_home THEN er ELSE 0 END) OVER w
        / NULLIF(SUM(CASE WHEN NOT is_home THEN ip_outs ELSE 0 END) OVER w / 3.0, 0) AS road_era_ytd,
    9.0 * SUM(CASE WHEN day_night = 'Day' THEN er ELSE 0 END) OVER w
        / NULLIF(SUM(CASE WHEN day_night = 'Day' THEN ip_outs ELSE 0 END) OVER w / 3.0, 0) AS day_era_ytd,
    9.0 * SUM(CASE WHEN day_night = 'Night' THEN er ELSE 0 END) OVER w
        / NULLIF(SUM(CASE WHEN day_night = 'Night' THEN ip_outs ELSE 0 END) OVER w / 3.0, 0) AS night_era_ytd
FROM with_derived
WINDOW
    w  AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
    w5 AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
           ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING),
    w10 AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING),
    w15 AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 15 PRECEDING AND 1 PRECEDING),
    w20 AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
ORDER BY player_id, season_id, game_date, game_id
;
"""

# ── Insert / Upsert ──────────────────────────────────────────────────────────

UPSERT_TEAM_SQL = """
INSERT INTO mlb.team_rolling_stats (
    game_id, team_id, team_side, season_id, game_date,
    rf, ra, hits, at_bats, walks, strikeouts, home_runs, total_bates,
    ip_outs, hits_allowed, walks_allowed, k_allowed, hr_allowed,
    cum_avg, cum_obp, cum_slg, cum_ops, cum_era, cum_whip,
    cum_k9, cum_bb9, cum_babip, cum_k_rate, cum_bb_rate,
    rf5, ra5, avg5, obp5, slg5, ops5, era5, whip5, k9_5, bb9_5,
    rf10, ra10, avg10, obp10, slg10, ops10, era10, whip10, k9_10, bb9_10,
    rf15, ra15, avg15, ops15, era15, whip15,
    win_pct, spread_pct, over_pct
)
SELECT
    game_id, team_id, team_side, season_id, game_date,
    rf, ra, hits, at_bats, walks, strikeouts, home_runs, total_bases,
    ip_outs, hits_allowed, walks_allowed, k_allowed, hr_allowed,
    cum_avg, cum_obp, cum_slg, cum_ops, cum_era, cum_whip,
    cum_k9, cum_bb9, cum_babip, cum_k_rate, cum_bb_rate,
    rf5, ra5, avg5, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    rf10, ra10, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    rf15, ra15, NULL, NULL, NULL, NULL,
    win_pct, NULL, NULL
FROM team_rolling_data
ON CONFLICT (game_id, team_side) DO UPDATE SET
    rf = EXCLUDED.rf,
    ra = EXCLUDED.ra,
    ...
;
"""


def get_db_url() -> str:
    return os.environ.get(
        "SYNC_DATABASE_URL",
        "postgresql+psycopg2://earl:earl_dev_pass@localhost:5432/earl_knows_football"
    )


def ensure_tables(engine: Engine) -> None:
    """Create rolling stats tables if they don't exist."""
    with engine.connect() as conn:
        conn.execute(text(CREATE_TEAM_ROLLING_STATS_SQL))
        conn.execute(text(CREATE_TEAM_ROLLING_INDEXES_SQL))
        conn.execute(text(CREATE_PITCHER_ROLLING_STATS_SQL))
        conn.execute(text(CREATE_PITCHER_ROLLING_INDEXES_SQL))
        # Migration: add new columns if the table already exists
        for col, dtype in [
            ("era_20", "DOUBLE PRECISION"),
            ("whip_20", "DOUBLE PRECISION"),
            ("k9_20", "DOUBLE PRECISION"),
            ("bb9_20", "DOUBLE PRECISION"),
            ("kbb_20", "DOUBLE PRECISION"),
            ("rest_days", "INTEGER"),
            ("home_era_ytd", "DOUBLE PRECISION"),
            ("road_era_ytd", "DOUBLE PRECISION"),
            ("day_era_ytd", "DOUBLE PRECISION"),
            ("night_era_ytd", "DOUBLE PRECISION"),
        ]:
            try:
                conn.execute(text(
                    f"ALTER TABLE mlb.pitcher_rolling_stats ADD COLUMN {col} {dtype}"
                ))
            except Exception:
                pass  # column already exists
        conn.commit()
    logger.info("Tables ensured.")


def populate_team_rolling(engine: Engine, incremental: bool = False) -> int:
    """Populate mlb.team_rolling_stats. Returns row count."""
    from sqlalchemy import text

    # For incremental, only process games not yet in the table
    extra_filter = ""
    if incremental:
        extra_filter = """
            AND cgs.game_id NOT IN (
                SELECT game_id FROM mlb.team_rolling_stats
            )
        """

    # Use a more specific match to avoid replacing cgs2 in the subquery
    sql = POPULATE_TEAM_ROLLING_SQL.replace(
        "FROM mlb.cumulative_game_stats cgs\n    JOIN mlb.games",
        f"FROM mlb.cumulative_game_stats cgs {extra_filter}\n    JOIN mlb.games"
    )
    # Fallback if the specific match didn't work
    if sql == POPULATE_TEAM_ROLLING_SQL:
        sql = POPULATE_TEAM_ROLLING_SQL.replace(
            "FROM mlb.cumulative_game_stats cgs",
            f"FROM mlb.cumulative_game_stats cgs {extra_filter}"
        )

    with engine.connect() as conn:
        result = conn.execute(text(sql))
        rows = result.fetchall()
        cols = result.keys()

        # Bulk insert
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        insert_stmt = pg_insert(
            __import__('sqlalchemy').Table(
                'team_rolling_stats',
                __import__('sqlalchemy').MetaData(),
                autoload_with=engine,
                schema='mlb'
            )
        )

        # Actually, let's use raw SQL UPSERT for simplicity
        # Build column list from cols
        col_list = [c for c in cols if c not in ('game_n',)]
        placeholders = ", ".join(f":{c}" for c in col_list)
        col_names = ", ".join(col_list)

        upsert_sql = f"""
        INSERT INTO mlb.team_rolling_stats ({col_names})
        VALUES ({placeholders})
        ON CONFLICT (game_id, team_side) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in col_list if c not in ('game_id', 'team_side'))}
        """

        dict_rows = [dict(zip(cols, r)) for r in rows]
        conn.execute(text(upsert_sql), dict_rows)
        conn.commit()

        logger.info("Inserted/updated %d team rolling stat rows", len(dict_rows))
        return len(dict_rows)


def populate_pitcher_rolling(engine: Engine, incremental: bool = False) -> int:
    """Populate mlb.pitcher_rolling_stats. Returns row count."""

    extra_filter = ""
    if incremental:
        extra_filter = """
            AND pgs.game_id NOT IN (
                SELECT game_id FROM mlb.pitcher_rolling_stats
            )
        """

    sql = POPULATE_PITCHER_ROLLING_SQL.replace(
        "FROM mlb.pitcher_game_stats pgs",
        f"FROM mlb.pitcher_game_stats pgs {extra_filter}"
    )

    with engine.connect() as conn:
        result = conn.execute(text(sql))
        rows = result.fetchall()
        cols = result.keys()

        col_list = [c for c in cols if c not in (
            'start_n', 'is_starter_filter', 'ip', 'prev_start_date',
            'day_night', 'is_home', 'hit_by_pitch'
        )]
        col_names = ", ".join(col_list)
        placeholders = ", ".join(f":{c}" for c in col_list)

        upsert_sql = f"""
        INSERT INTO mlb.pitcher_rolling_stats ({col_names})
        VALUES ({placeholders})
        ON CONFLICT (game_id, player_id) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in col_list if c not in ('game_id', 'player_id'))}
        """

        dict_rows = [dict(zip(cols, r)) for r in rows]
        conn.execute(text(upsert_sql), dict_rows)
        conn.commit()

        logger.info("Inserted/updated %d pitcher rolling stat rows", len(dict_rows))
        return len(dict_rows)


def main():
    parser = argparse.ArgumentParser(description="Populate rolling stats tables")
    parser.add_argument("--incremental", action="store_true",
                        help="Only process games not yet in the tables")
    parser.add_argument("--team-only", action="store_true",
                        help="Only populate team rolling stats")
    parser.add_argument("--pitcher-only", action="store_true",
                        help="Only populate pitcher rolling stats")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    engine = create_engine(get_db_url())
    ensure_tables(engine)

    start = time.time()

    if not args.pitcher_only:
        t0 = time.time()
        n = populate_team_rolling(engine, incremental=args.incremental)
        logger.info("Team rolling stats: %d rows in %.1fs", n, time.time() - t0)

    if not args.team_only:
        t0 = time.time()
        n = populate_pitcher_rolling(engine, incremental=args.incremental)
        logger.info("Pitcher rolling stats: %d rows in %.1fs", n, time.time() - t0)

    logger.info("Total time: %.1fs", time.time() - start)
    engine.dispose()


if __name__ == "__main__":
    main()
