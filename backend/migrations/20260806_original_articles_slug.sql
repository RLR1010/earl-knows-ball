-- 20260806: Add SEO-friendly slug to original_articles.
--
-- Slug is unique per sport and takes the form:  YYYY-MM-DD-title-slugified
-- e.g. 2026-08-06-the-dodgers-are-still-the-team-to-beat

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS slug TEXT;

-- Unique per sport (articles for different sports may share a slug text).
CREATE UNIQUE INDEX IF NOT EXISTS unique_original_articles_slug
    ON public.original_articles (sport, slug)
    WHERE slug IS NOT NULL;
