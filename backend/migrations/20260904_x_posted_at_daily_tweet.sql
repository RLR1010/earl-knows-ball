-- Add X-posted tracker columns to the four tweet-source tables (idempotent).
-- Each row records when its content was last sent to X by the daily_tweet task
-- so the same writeup/original article is never tweeted twice.

ALTER TABLE public.original_articles
  ADD COLUMN IF NOT EXISTS x_posted_at TIMESTAMPTZ;

ALTER TABLE mlb.game_writeups
  ADD COLUMN IF NOT EXISTS x_posted_at TIMESTAMPTZ;

ALTER TABLE nfl.game_writeups
  ADD COLUMN IF NOT EXISTS x_posted_at TIMESTAMPTZ;

ALTER TABLE nba.game_writeups
  ADD COLUMN IF NOT EXISTS x_posted_at TIMESTAMPTZ;
