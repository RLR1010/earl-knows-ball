-- Add a "section" destination to original articles + auto-generation configs.
--
-- Removes the assumption that every original article lives in the generic
-- "Articles" section. A section designates where an article should surface
-- (e.g. a "Daily Picks" block on the MLB home page).
--
--   section = 'article'      -> the normal Articles section (default/legacy)
--   section = 'daily_picks'  -> the daily-picks block on the sport home page
--
-- Backwards compatible: existing rows default to 'article' so nothing moves.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS section TEXT NOT NULL DEFAULT 'article';

ALTER TABLE public.auto_generation_configs
    ADD COLUMN IF NOT EXISTS section TEXT NOT NULL DEFAULT 'article';

-- Fast lookup of the latest article for a given (sport, section).
CREATE INDEX IF NOT EXISTS idx_orig_articles_sport_section_pub
    ON public.original_articles (sport, section, published_at DESC);
