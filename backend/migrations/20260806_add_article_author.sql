-- 20260806: Add author column to original_articles, default 'Earl'.
-- Admins can change the displayed author per article.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS author TEXT NOT NULL DEFAULT 'Earl';

COMMENT ON COLUMN public.original_articles.author IS
    'Author byline shown on the published article. Defaults to Earl.';
