-- Feature: "Free Pick of the Day/Week" — one premium game writeup offered free.
-- Adds is_free_feature (nullable, single-active enforced in app) + free_featured_at
-- to game_writeups for mlb/nfl/nba. When is_free_feature is true and
-- free_featured_at is set, the writeup's full premium content is served to everyone
-- (no 403) and it surfaces on the homepage "Free Pick" section with its social card.
ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS is_free_feature BOOLEAN DEFAULT FALSE;
ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS free_featured_at TIMESTAMPTZ;
ALTER TABLE nfl.game_writeups ADD COLUMN IF NOT EXISTS is_free_feature BOOLEAN DEFAULT FALSE;
ALTER TABLE nfl.game_writeups ADD COLUMN IF NOT EXISTS free_featured_at TIMESTAMPTZ;
ALTER TABLE nba.game_writeups ADD COLUMN IF NOT EXISTS is_free_feature BOOLEAN DEFAULT FALSE;
ALTER TABLE nba.game_writeups ADD COLUMN IF NOT EXISTS free_featured_at TIMESTAMPTZ;
