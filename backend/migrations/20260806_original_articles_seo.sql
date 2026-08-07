-- Add SEO meta fields to public.original_articles.
-- seo_description : meta description for <head> (LLM-generated, ~155 chars).
-- seo_keywords    : comma-separated keyword tags for <head> (LLM-generated).
ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS seo_description TEXT,
    ADD COLUMN IF NOT EXISTS seo_keywords    TEXT;
