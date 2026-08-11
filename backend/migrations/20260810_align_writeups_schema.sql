-- Align nfl.game_writeups and nba.game_writeups columns to match mlb.game_writeups.
--
-- Column names are already identical across all three schemas (27 cols each,
-- incl. prop_*). The remaining differences are data types and defaults:
--
--   NFL (differs most):
--     * research_brief / quality_checks are `json`, MLB is `jsonb`       -> convert
--     * missing defaults on public_content, premium_content (''),
--       status ('draft'), version (1), is_historical (false),
--       created_at / updated_at (now())                                  -> add defaults
--
--   NBA:
--     * research_brief / quality_checks are `json`, MLB is `jsonb`       -> convert
--       (all NBA defaults already match MLB; NBA title default '' is kept — harmless)
--
-- Idempotent. All existing json values are valid, so `USING col::jsonb` is safe.
-- Also drops NBA's extra title default ('') so NBA matches MLB exactly.

-- ---------- NFL ----------
ALTER TABLE nfl.game_writeups
    ALTER COLUMN research_brief TYPE jsonb USING research_brief::jsonb,
    ALTER COLUMN quality_checks TYPE jsonb USING quality_checks::jsonb;

ALTER TABLE nfl.game_writeups
    ALTER COLUMN public_content    SET DEFAULT '',
    ALTER COLUMN premium_content   SET DEFAULT '',
    ALTER COLUMN status            SET DEFAULT 'draft',
    ALTER COLUMN version           SET DEFAULT 1,
    ALTER COLUMN is_historical     SET DEFAULT false,
    ALTER COLUMN created_at        SET DEFAULT now(),
    ALTER COLUMN updated_at        SET DEFAULT now();

-- ---------- NBA ----------
ALTER TABLE nba.game_writeups
    ALTER COLUMN research_brief TYPE jsonb USING research_brief::jsonb,
    ALTER COLUMN quality_checks  TYPE jsonb USING quality_checks::jsonb;

ALTER TABLE nba.game_writeups
    ALTER COLUMN title DROP DEFAULT;
