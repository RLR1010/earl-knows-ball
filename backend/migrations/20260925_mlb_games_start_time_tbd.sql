-- Doubleheader start-time support for MLB games.
-- The MLB feed leaves a doubleheader nightcap's first pitch unset until it is
-- announced (status.startTimeTBD = true) and returns a placeholder gameDate
-- (game 1 time + 5 min). We store that flag so the UI can show "TBD" instead of
-- a bogus 5-minutes-apart time. Idempotent.
ALTER TABLE mlb.games
    ADD COLUMN IF NOT EXISTS start_time_tbd BOOLEAN NOT NULL DEFAULT FALSE;
