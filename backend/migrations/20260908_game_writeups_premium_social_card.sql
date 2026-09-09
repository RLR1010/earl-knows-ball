-- Feature: "Free Pick" giveaway — dedicated PREMIUM social card for the featured free pick.
-- Adds premium_social_card (TEXT, holds the served path like /writeups/cards/{sport}/free-{game_id}.png)
-- to game_writeups for mlb/nfl/nba. Separate from preview_image (the public writeup card);
-- this is the bespoke promotional "FREE PICKS from Earl" card rendered on the Feature action.
ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS premium_social_card TEXT;
ALTER TABLE nfl.game_writeups ADD COLUMN IF NOT EXISTS premium_social_card TEXT;
ALTER TABLE nba.game_writeups ADD COLUMN IF NOT EXISTS premium_social_card TEXT;
