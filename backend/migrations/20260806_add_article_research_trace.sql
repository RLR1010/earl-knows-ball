-- 20260806: Add prompt + research trace storage to original_articles.
-- Each article now stores the exact prompt (system + user) that built it and
-- the research (tool calls + results) performed before the final article.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS prompt_json JSONB;

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS research_json JSONB;

COMMENT ON COLUMN public.original_articles.prompt_json IS
    'The exact system + user prompt messages sent to the LLM.';
COMMENT ON COLUMN public.original_articles.research_json IS
    'The research performed before writing: every tool call made + its result, in order.';
