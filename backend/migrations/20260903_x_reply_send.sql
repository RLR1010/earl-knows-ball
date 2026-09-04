-- 2026-09-03 — Approve-and-send reply flow (Rich)
-- x_reply_suggestions gains traceability columns for the POSTED reply:
--   posted_tweet_id : the real reply's tweet id returned by POST /2/tweets
--   posted_at       : when we actually sent the reply to X
-- These are written only when the approved reply is actually posted via the
-- "Approve and send" action. Manual approvals leave them NULL.
ALTER TABLE public.x_reply_suggestions
    ADD COLUMN IF NOT EXISTS posted_tweet_id text,
    ADD COLUMN IF NOT EXISTS posted_at timestamptz;

COMMENT ON COLUMN public.x_reply_suggestions.posted_tweet_id
    IS 'Tweet id of the real reply posted to X (Approve-and-send). NULL for manual approves/rejects.';
COMMENT ON COLUMN public.x_reply_suggestions.posted_at
    IS 'Timestamp the reply was actually posted to X. NULL until sent.';
