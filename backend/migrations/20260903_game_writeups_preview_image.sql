-- Add preview_image (social/og card PNG path) to MLB game writeups.
-- The card PNG is rendered to frontend/public/og/previews/mlb/gw-{game_id}.png
-- and this column stores the site-relative URL (e.g. /og/previews/mlb/gw-123.png).
ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS preview_image TEXT;
