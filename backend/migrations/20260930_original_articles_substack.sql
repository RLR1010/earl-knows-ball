-- Substack cross-posting columns for public.original_articles.
--
-- Earl cross-posts editorial/research articles to earlknowsball.substack.com as
-- UNPUBLISHED DRAFTS (Substack has no public API; we use its internal
-- publishing endpoints via python-substack). These columns record the linkage
-- so the admin can (a) avoid creating duplicate drafts, (b) deep-link straight
-- to the Substack draft editor, and (c) show cross-post status per article.
--
--   substack_draft_id      : Substack's draft/post id (nullable)
--   substack_url           : deep link to the Substack draft/publish editor
--   substack_synced_at     : when we last pushed a draft for this article
--   substack_published_at  : set only if/when the Substack post actually goes
--                            live (auto-publish is OFF; a human publishes)
--
-- Backwards compatible: existing rows default to NULL (never cross-posted).

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS substack_draft_id BIGINT;

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS substack_url TEXT;

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS substack_synced_at TIMESTAMPTZ;

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS substack_published_at TIMESTAMPTZ;
