-- Per-config "previous-coverage context" toggle for auto-generated articles.
-- When TRUE, the generation scheduler feeds back the last N published articles
-- in the config's scope (sport + section) so the LLM writes fresh, non-repetitive
-- content. Defaults to FALSE (opt-in per config from the admin UI).
ALTER TABLE public.auto_generation_configs
    ADD COLUMN IF NOT EXISTS recency_context BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.auto_generation_configs.recency_context IS 'feed back previously-published articles in this scope so the LLM writes new, non-repetitive content';
