-- 20260806: Store teams mentioned in each original article, most-mentioned first.
-- Populated by the LLM at article generation/update time. JSONB array of team
-- abbreviations (e.g. ["NYY","BOS","TB"]), ordered from most to least mentioned.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS teams JSONB;

COMMENT ON COLUMN public.original_articles.teams IS
    'Team abbreviations mentioned in the article, most-mentioned first (extracted by the LLM).';
