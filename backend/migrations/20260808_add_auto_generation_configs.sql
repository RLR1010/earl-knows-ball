-- Create the auto_generation_configs table (continuous article templates).
CREATE TABLE IF NOT EXISTS public.auto_generation_configs (
    id                  BIGSERIAL PRIMARY KEY,
    sport               TEXT        NOT NULL,                 -- 'mlb' | 'nfl' | 'nba'
    title               TEXT        NOT NULL,
    description         TEXT,
    instructions        TEXT,                                  -- generation prompt / instructions
    cadence             TEXT        NOT NULL DEFAULT 'daily',  -- 'daily' | 'weekly'
    scope_type          TEXT        NOT NULL DEFAULT 'sport',  -- 'team' | 'sport'
    team_id             BIGINT,                                -- FK into <sport>.teams (nullable for sport scope)
    team_abbr           TEXT,                                  -- denormalized team abbreviation
    team_name           TEXT,                                  -- denormalized team name
    template_article_id BIGINT,                                -- optional link to public.original_articles
    status              TEXT        NOT NULL DEFAULT 'active', -- 'active' | 'inactive' | 'paused'
    last_generated_at   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_auto_gen_sport   ON public.auto_generation_configs (sport);
CREATE INDEX IF NOT EXISTS idx_auto_gen_cadence ON public.auto_generation_configs (cadence);
CREATE INDEX IF NOT EXISTS idx_auto_gen_status  ON public.auto_generation_configs (status);
