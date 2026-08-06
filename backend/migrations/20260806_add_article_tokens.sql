-- 20260806: Track tokens used to generate an article.
-- Since not all articles will be regenerated, the column is nullable.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS tokens_used INTEGER;

COMMENT ON COLUMN public.original_articles.tokens_used IS
    'Total LLM tokens consumed to generate the article.';
