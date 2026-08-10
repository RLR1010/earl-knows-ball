-- Add separate premium Prop Bets article columns to game_writeups
-- across all sport schemas (mlb, nfl, nba).
--
-- These host a second, distinct DE premium article on the same one-row-per-game
-- writeup row. prop_content is premium-only (never shown in public content).
-- prop_title is e.g. "Prop Bets for Bus vs NYY - Aug 9, 2026".
--
-- prop_* columns are NULL until a props article has been generated for the game.
ALTER TABLE mlb.game_writeups
    ADD COLUMN IF NOT EXISTS prop_title         VARCHAR(300),
    ADD COLUMN IF NOT EXISTS prop_content       TEXT,
    ADD COLUMN IF NOT EXISTS prop_generated_by  VARCHAR(100),
    ADD COLUMN IF NOT EXISTS prop_total_tokens  INTEGER,
    ADD COLUMN IF NOT EXISTS prop_published_at  TIMESTAMPTZ;

ALTER TABLE nfl.game_writeups
    ADD COLUMN IF NOT EXISTS prop_title         VARCHAR(300),
    ADD COLUMN IF NOT EXISTS prop_content       TEXT,
    ADD COLUMN IF NOT EXISTS prop_generated_by  VARCHAR(100),
    ADD COLUMN IF NOT EXISTS prop_total_tokens  INTEGER,
    ADD COLUMN IF NOT EXISTS prop_published_at  TIMESTAMPTZ;

ALTER TABLE nba.game_writeups
    ADD COLUMN IF NOT EXISTS prop_title         VARCHAR(300),
    ADD COLUMN IF NOT EXISTS prop_content       TEXT,
    ADD COLUMN IF NOT EXISTS prop_generated_by  VARCHAR(100),
    ADD COLUMN IF NOT EXISTS prop_total_tokens  INTEGER,
    ADD COLUMN IF NOT EXISTS prop_published_at  TIMESTAMPTZ;
