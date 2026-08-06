-- 20260806: Add original_articles table for the "Original Articles" admin feature.
--
-- These are LLM-written editorial articles (not RSS imports, not game previews).
-- A single shared table in the `public` schema with a `sport` column keeps the
-- cross-sport admin list simple; public reads filter by sport.
--
-- See: backend/app/routers/original_articles.py + frontend admin/original-articles.

CREATE TABLE IF NOT EXISTS public.original_articles (
    id           BIGSERIAL PRIMARY KEY,
    sport        TEXT        NOT NULL CHECK (sport IN ('mlb', 'nfl', 'nba')),
    title        TEXT        NOT NULL,
    summary      TEXT,
    content      TEXT        NOT NULL,          -- markdown body (article)
    content_html TEXT,                          -- optional rendered HTML
    instructions TEXT,                          -- the user's instructions snapshot
    status       TEXT        NOT NULL DEFAULT 'published',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_original_articles_sport
    ON public.original_articles (sport, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_original_articles_sport_status
    ON public.original_articles (sport, status);

COMMENT ON TABLE public.original_articles IS
    'LLM-written editorial articles per sport, published to /{sport}/articles.';
