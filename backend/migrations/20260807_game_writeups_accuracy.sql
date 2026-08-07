-- 20260807: Track the LLM accuracy-verification pass on game writeups.
-- The final writeup generation step runs an accuracy check that confirms
-- every factual claim / stat / player / team is traceable to the research
-- brief and (for public content) that no betting predictions were made.
--
-- accuracy_check        : JSON with { passed, findings, retries_used }
--                         findings = [{ type, claim, detail, source }]
-- accuracy_check_tokens : LLM tokens consumed by the accuracy-verification pass
--                         (excludes draft-generation tokens).

ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS accuracy_check JSON;
ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS accuracy_check_tokens INTEGER;

ALTER TABLE nfl.game_writeups ADD COLUMN IF NOT EXISTS accuracy_check JSON;
ALTER TABLE nfl.game_writeups ADD COLUMN IF NOT EXISTS accuracy_check_tokens INTEGER;

ALTER TABLE nba.game_writeups ADD COLUMN IF NOT EXISTS accuracy_check JSON;
ALTER TABLE nba.game_writeups ADD COLUMN IF NOT EXISTS accuracy_check_tokens INTEGER;

COMMENT ON COLUMN mlb.game_writeups.accuracy_check IS
    'JSON result of the post-generation accuracy-verification pass.';
COMMENT ON COLUMN mlb.game_writeups.accuracy_check_tokens IS
    'LLM tokens consumed by the accuracy-verification pass.';
