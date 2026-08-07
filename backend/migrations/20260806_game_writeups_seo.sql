-- Add SEO meta fields to game_writeups across all sport schemas.
-- seo_description : meta description for <head> (LLM-generated, ~155 chars).
-- seo_keywords    : comma-separated keyword tags for <head> (LLM-generated).
ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS seo_description TEXT;
ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS seo_keywords    TEXT;
ALTER TABLE nfl.game_writeups ADD COLUMN IF NOT EXISTS seo_description TEXT;
ALTER TABLE nfl.game_writeups ADD COLUMN IF NOT EXISTS seo_keywords    TEXT;
ALTER TABLE nba.game_writeups ADD COLUMN IF NOT EXISTS seo_description TEXT;
ALTER TABLE nba.game_writeups ADD COLUMN IF NOT EXISTS seo_keywords    TEXT;
