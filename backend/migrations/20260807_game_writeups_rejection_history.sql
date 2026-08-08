-- 20260807: Audit trail of rejected (inaccurate) drafts on game writeups.
--
-- When the post-generation accuracy check fails, the generator runs up to
-- MAX_CORRECTION_PASSES correction passes. Each draft that was REJECTED by the
-- accuracy check is snapshotted here so we can see exactly what the checker
-- flagged and what text failed before it was corrected.
--
-- rejection_history : JSON array, each element:
--   {
--     attempt,                 -- 1-based correction attempt
--     timestamp,               -- when the failing draft was produced (ISO UTC)
--     accuracy_check,          -- the JSON that caught the error { passed, findings }
--     public_content,          -- the failing public draft text
--     premium_content          -- the failing premium draft text (may be "")
--   }

ALTER TABLE mlb.game_writeups ADD COLUMN IF NOT EXISTS rejection_history JSON;
ALTER TABLE nfl.game_writeups ADD COLUMN IF NOT EXISTS rejection_history JSON;
ALTER TABLE nba.game_writeups ADD COLUMN IF NOT EXISTS rejection_history JSON;

COMMENT ON COLUMN mlb.game_writeups.rejection_history IS
    'JSON array of drafts rejected by the accuracy check (audit trail).';
COMMENT ON COLUMN nfl.game_writeups.rejection_history IS
    'JSON array of drafts rejected by the accuracy check (audit trail).';
COMMENT ON COLUMN nba.game_writeups.rejection_history IS
    'JSON array of drafts rejected by the accuracy check (audit trail).';
