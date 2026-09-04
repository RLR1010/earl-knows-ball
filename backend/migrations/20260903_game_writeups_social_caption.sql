-- 20260903_game_writeups_social_caption.sql
-- Add a hand/pipeline-authored social caption for game-writeup cards/posts.
ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS social_caption TEXT;
