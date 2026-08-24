-- 2026-08-22 NBA active_players table (mirrors mlb.lineups).
--
-- Purpose: capture WHICH players are active for a given NBA game, so the ML
-- features can aggregate each active player's cumulative points / rebounds /
-- assists as a fast lookup. The roster must be captured because
-- nba.player_rolling_stats only fills AFTER a game, so for a scheduled game
-- there is no post-game data to derive the active roster from.
--
--   * game_id    -> nba.games.id   (the game this roster applies to)
--   * team_id    -> nba.teams.id   (which team; mirrors how lineups key by side)
--   * player_id  -> nba.players.id
--   * is_starter -> starting-five flag (NBA analog of mlb.lineups batting_order)
--   * src        -> 'pregame' (roster known before tip) | 'postgame' (backfill)
--
-- Features (built separately in nba.features) read this table for the TARGET
-- game, falling back to the most recent prior FINAL game's active roster when
-- a scheduled game has not been pregame-filled yet (mirrors mlb h_lin/a_lin).
--
-- Idempotent.

CREATE TABLE IF NOT EXISTS nba.active_players (
    id            BIGSERIAL PRIMARY KEY,
    game_id       INTEGER NOT NULL REFERENCES nba.games(id)  ON DELETE CASCADE,
    team_id       INTEGER NOT NULL REFERENCES nba.teams(id),
    player_id     INTEGER NOT NULL REFERENCES nba.players(id) ON DELETE CASCADE,
    is_starter    BOOLEAN NOT NULL DEFAULT FALSE,
    src           VARCHAR(16) NOT NULL DEFAULT 'pregame',   -- 'pregame' | 'postgame'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (game_id, player_id)
);

-- Primary lookup: all active players for a game.
CREATE INDEX IF NOT EXISTS idx_active_players_game
    ON nba.active_players (game_id);

-- Team-scoped lookup for the effective-game fallback (prior FINAL per team).
CREATE INDEX IF NOT EXISTS idx_active_players_team_game
    ON nba.active_players (team_id, game_id);
