-- 2026-08-22 NBA accurate active/inactive status.
--
-- Backed by ESPN core API (sports.core.api.espn.com) game roster `didNotPlay` +
-- reason (validated 100% vs pgs minutes on sampled games), giving a TRULY accurate
-- per-game classification:
--   * PLAYED   = didNotPlay=False, minutes>0
--   * DNP_CD   = dressed/active but coach did not play (didNotPlay=True, reason="COACH'S DECISION")
--   * INACTIVE = not dressed (injured/inactive) -> recorded in the new inactive_players table
--
-- The previous backfill built active_players from pgs minutes>0 only ("who actually
-- played"), which conflated injured-inactive and DNP-CD. This migration:
--   1) adds status/reason to nba.active_players so active rows carry PLAYED vs DNP_CD
--   2) creates nba.inactive_players for the not-dressed (injured/inactive) players.
--
-- BACKUP of the old table was taken first: nba.active_players_bak_20260822_1803
-- (270,021 rows, same columns + old values). Idempotent.

ALTER TABLE nba.active_players
    ADD COLUMN IF NOT EXISTS status VARCHAR(12) NOT NULL DEFAULT 'PLAYED';
ALTER TABLE nba.active_players
    ADD COLUMN IF NOT EXISTS reason VARCHAR(140);

DROP TABLE IF EXISTS nba.inactive_players;
CREATE TABLE nba.inactive_players (
    id          BIGSERIAL PRIMARY KEY,
    game_id     INTEGER      NOT NULL REFERENCES nba.games(id) ON DELETE CASCADE,
    team_id     INTEGER      NOT NULL REFERENCES nba.teams(id),
    player_id   INTEGER      NOT NULL REFERENCES nba.players(id) ON DELETE CASCADE,
    status      VARCHAR(12)  NOT NULL DEFAULT 'INACTIVE',  -- INACTIVE / OUT / QUESTIONABLE / DTD
    reason      VARCHAR(140),
    src         VARCHAR(16)  NOT NULL DEFAULT 'postgame',  -- 'pregame' | 'postgame'
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_inactive_player_game ON nba.inactive_players (game_id, player_id);
CREATE INDEX IF NOT EXISTS idx_inactive_players_game ON nba.inactive_players (game_id);
CREATE INDEX IF NOT EXISTS idx_inactive_players_team ON nba.inactive_players (team_id, game_id);
