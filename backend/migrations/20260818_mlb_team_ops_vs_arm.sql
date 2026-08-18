-- 20260818_mlb_team_ops_vs_arm.sql
--
-- Cumulative per-game table of a team's OFFENSE OPS and WINS vs opposing
-- starter hand (LHP / RHP), computed over a through-date window.
--
-- Purpose: this is the leak-safe, precomputable replacement for the platoon
-- LATERAL subqueries currently in mlb.data_loader (plato_h_lhp / plato_h_rhp /
-- plato_a_lhp / plato_a_rhp). Those LATERALs re-scan 34,839 game rows x 4
-- subqueries over the 678k-row batting_game_stats table, which is the dominant
-- cost of load_games() (~145-290s). By precomputing this table ONCE (mirroring
-- the mlb.team_rolling_stats CURRENT ROW window pattern), the loader can later
-- simply LEFT JOIN the PREVIOUS Final row and skip the expensive per-row scans.
--
-- ⚠️  DATA-QUALITY / LEAK CONTRACT (MUST MATCH the current plato LATERALs exactly)
-- -------------------------------------------------------------------------------
-- * OPS formula   : (H + BB + HBP + TB) / (AB + BB + HBP + SF)
--   = SUM(bg.hits + bg.base_on_balls + bg.hit_by_pitch + bg.total_bases)
--   / SUM(bg.at_bats + bg.base_on_balls + bg.hit_by_pitch + bg.sacrifice_flies)
-- * Arm attribution: the OPPOSING starter's hand:
--     - home offense (team_side='home') -> games.away_pitcher_name -> players.throws
--     - away offense (team_side='away') -> games.home_pitcher_name -> players.throws
--   Only throws = 'L' and throws = 'R' collect. throws = 'S' (switch, 3 rows)
--   and NULL throws are excluded from BOTH buckets -- identical to the loader.
-- * Leak-safety   : only FINAL games, SAME season_id, strictly BEFORE the target
--   game (date < g.date - INTERVAL '30 minutes'). The 30-minute offset is
--   REQUIRED so a game starting 6:00pm never ingests a 6:05pm game's stats.
--   Preserve it exactly -- do not collapse to a plain date ordering.
-- * Cumulation semantics (CURRENT ROW, like team_rolling_stats):
--   EACH row stores stats THROUGH that game (its own result INCLUDED). The
--   loader will read the PREVIOUS Final row, so the model sees the through-date
--   value entering the target game -- leak-safe. Do NOT switch to "... AND 1
--   PRECEDING" (off-by-one: the previous-row read would double-subtract).
-- * WIN  vs arm    : a team "wins vs {arm}" when it is the offense side above and
--   wins the ballgame (home_side: home_score > away_score; away_side:
--   away_score > home_score) IN the same FINAL game where the opposing starter
--   threw that arm. Otherwise counts 0. Cumulative sum over the through-date
--   window.
-- * Grain          : (team_id, season_id, game_id, team_side, arm)
-- * games_vs_arm   : number of games in the through-date window where this side
--   faced that arm (excludes rainouts/partials via status='FINAL' and non-L/R).
-- -------------------------------------------------------------------------------

BEGIN;

CREATE TABLE IF NOT EXISTS mlb.team_ops_vs_arm (
    game_id        integer         NOT NULL,
    team_id        integer         NOT NULL,
    season_id      integer         NOT NULL,
    team_side      varchar(4)      NOT NULL CHECK (team_side IN ('home','away')),
    arm            varchar(1)      NOT NULL CHECK (arm IN ('L','R')),

    -- Composite numerator/denominator parts (raw sums over the through-date window)
    ab             integer         NOT NULL DEFAULT 0,
    h              integer         NOT NULL DEFAULT 0,
    bb             integer         NOT NULL DEFAULT 0,
    hbp            integer         NOT NULL DEFAULT 0,
    sf             integer         NOT NULL DEFAULT 0,
    tb             integer         NOT NULL DEFAULT 0,

    -- Derived metrics (through-date, CURRENT ROW semantics)
    ops_vs_arm     numeric(10,4),
    wins_vs_arm    integer         NOT NULL DEFAULT 0,
    games_vs_arm   integer         NOT NULL DEFAULT 0,

    PRIMARY KEY (team_id, season_id, game_id, team_side, arm)
);

-- Indexes for the eventual loader lookups (previous Final row per team/arm)
CREATE INDEX IF NOT EXISTS ix_mlb_team_ops_vs_arm_game
    ON mlb.team_ops_vs_arm (game_id);
CREATE INDEX IF NOT EXISTS ix_mlb_team_ops_vs_arm_arm_side
    ON mlb.team_ops_vs_arm (team_id, season_id, team_side, arm, game_id);

COMMIT;
