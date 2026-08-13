-- Add 'all' (site-wide / non-sport-specific) as a valid value for the
-- sport column on original_articles and article_ideas, so cross-league
-- editorial articles can be created and surface on the site home page.

-- Recreate the constraint to allow 'all'.
ALTER TABLE public.original_articles DROP CONSTRAINT IF EXISTS original_articles_sport_check;
ALTER TABLE public.original_articles ADD CONSTRAINT original_articles_sport_check
  CHECK (sport = ANY (ARRAY['all'::text, 'mlb'::text, 'nfl'::text, 'nba'::text]));

ALTER TABLE public.article_ideas DROP CONSTRAINT IF EXISTS article_ideas_sport_check;
ALTER TABLE public.article_ideas ADD CONSTRAINT article_ideas_sport_check
  CHECK (sport = ANY (ARRAY['all'::text, 'mlb'::text, 'nfl'::text, 'nba'::text]));
