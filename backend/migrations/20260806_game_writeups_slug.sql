-- Add SEO-friendly slug to game_writeups across all sport schemas.
ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS slug TEXT;
ALTER TABLE nfl.game_writeups ADD COLUMN IF NOT EXISTS slug TEXT;
ALTER TABLE nba.game_writeups ADD COLUMN IF NOT EXISTS slug TEXT;
