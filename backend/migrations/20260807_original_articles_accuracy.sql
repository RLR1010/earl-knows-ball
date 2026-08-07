-- 20260807: Track the LLM accuracy-verification pass on original articles.
-- accuracy_check        : JSON { passed, findings, retries_used }
-- accuracy_check_tokens : LLM tokens consumed by the accuracy pass.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS accuracy_check JSON;

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS accuracy_check_tokens INTEGER;

COMMENT ON COLUMN public.original_articles.accuracy_check IS
    'JSON result of the post-generation accuracy-verification pass.';
COMMENT ON COLUMN public.original_articles.accuracy_check_tokens IS
    'LLM tokens consumed by the accuracy-verification pass.';
