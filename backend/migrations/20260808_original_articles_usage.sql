-- 20260808: Add per-call token/cost breakdown to original_articles.
--
-- Stores the usage_log (research / write / accuracy / correction / seo calls,
-- with cache hit/miss + prompt/completion/reasoning token split) so the admin
-- can audit the cost of each generated article.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS usage_json JSONB;
