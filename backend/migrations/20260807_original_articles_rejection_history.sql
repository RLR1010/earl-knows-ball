-- 20260807: Audit trail of rejected (inaccurate) drafts on original articles.
--
-- Mirrors the game_writeups.rejection_history feature. When the post-draft
-- accuracy check fails, the generator runs up to MAX_ORIGINAL_CORRECTION_PASSES
-- correction passes. Each draft that was REJECTED is snapshotted here so we can
-- see exactly what the checker flagged before it was corrected.

ALTER TABLE public.original_articles
    ADD COLUMN IF NOT EXISTS rejection_history JSONB;

COMMENT ON COLUMN public.original_articles.rejection_history IS
    'JSON array of drafts rejected by the accuracy check (audit trail). Each: '
    '{attempt, timestamp, accuracy_check, title, summary, content}.';
