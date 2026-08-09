-- Article Ideas: brainstormed article concepts the LLM can turn into prompts
-- for the original-articles tool. Ideas are sport-specific and optionally
-- team-specific. When an idea is used, we record used_at (+ optional link to
-- the resulting public.original_articles row).

CREATE TABLE IF NOT EXISTS public.article_ideas (
    id              BIGSERIAL PRIMARY KEY,
    sport           TEXT NOT NULL CHECK (sport IN ('mlb', 'nfl', 'nba')),
    title           TEXT NOT NULL,
    description     TEXT,
    prompt          TEXT,
    -- Optional team scoping (denormalized on purpose: team ids collide across
    -- the per-sport schemas, so we keep the id + abbreviation + display name).
    team_id         BIGINT,
    team_abbr       TEXT,
    team_name       TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'used', 'archived')),
    used_at         TIMESTAMPTZ,
    used_article_id BIGINT REFERENCES public.original_articles (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_article_ideas_sport_status
    ON public.article_ideas (sport, status);
CREATE INDEX IF NOT EXISTS idx_article_ideas_used_at
    ON public.article_ideas (used_at);
