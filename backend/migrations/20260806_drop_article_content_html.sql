-- 20260806: Drop unused content_html column from original_articles.
-- The app renders markdown directly (frontend react-markdown); the column was
-- speculative and never populated.

ALTER TABLE public.original_articles
    DROP COLUMN IF EXISTS content_html;
