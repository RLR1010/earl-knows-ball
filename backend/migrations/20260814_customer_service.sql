-- Customer Service chat tables.
-- cs_messages:    user support chat thread (user + assistant turns), used for
--                 per-user, per-month token accounting (200k cap).
-- cs_knowledge:   grounding documents the support bot answers from
--                 (FAQ entries, Terms & Conditions, Privacy Statement).

CREATE TABLE IF NOT EXISTS public.cs_messages (
    id          BIGSERIAL PRIMARY KEY,
    user_id     VARCHAR(36) NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    role        VARCHAR(16) NOT NULL,            -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    model       VARCHAR(100),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cs_messages_user_created
    ON public.cs_messages (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_cs_messages_user_id
    ON public.cs_messages (user_id);

CREATE TABLE IF NOT EXISTS public.cs_knowledge (
    id         BIGSERIAL PRIMARY KEY,
    category   VARCHAR(40) NOT NULL,             -- 'faq' | 'terms' | 'privacy'
    title      VARCHAR(255) NOT NULL,
    content    TEXT NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cs_knowledge_category_active
    ON public.cs_knowledge (category, active);
