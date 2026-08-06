-- 20260806: Add LLM-generation metadata to original_articles.
--
-- Stores the reasoning level, requested word range, and the actual word count
-- of the final article so the admin can see (and reproduce) how each article
-- was generated.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS reasoning      TEXT NOT NULL DEFAULT 'medium',
    ADD COLUMN IF NOT EXISTS word_min       INTEGER,
    ADD COLUMN IF NOT EXISTS word_max       INTEGER,
    ADD COLUMN IF NOT EXISTS word_count     INTEGER;
