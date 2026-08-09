-- Add article-generation settings to auto_generation_configs.
ALTER TABLE public.auto_generation_configs
    ADD COLUMN IF NOT EXISTS reasoning     TEXT NOT NULL DEFAULT 'medium',
    ADD COLUMN IF NOT EXISTS visibility    TEXT NOT NULL DEFAULT 'public',
    ADD COLUMN IF NOT EXISTS word_min      INTEGER NOT NULL DEFAULT 400,
    ADD COLUMN IF NOT EXISTS word_max      INTEGER NOT NULL DEFAULT 700;

COMMENT ON COLUMN public.auto_generation_configs.reasoning  IS 'reasoning lever: minimal/low/medium/high/xhigh';
COMMENT ON COLUMN public.auto_generation_configs.visibility IS 'public (no betting advice) or premium (betting advice OK)';
COMMENT ON COLUMN public.auto_generation_configs.word_min   IS 'target word-count range lower bound';
COMMENT ON COLUMN public.auto_generation_configs.word_max   IS 'target word-count range upper bound';

-- Title mode for auto-generation configs.
ALTER TABLE public.auto_generation_configs
    ADD COLUMN IF NOT EXISTS title_mode TEXT NOT NULL DEFAULT 'fixed';

COMMENT ON COLUMN public.auto_generation_configs.title_mode IS 'fixed (same title each run) or llm (LLM invents title each run)';
