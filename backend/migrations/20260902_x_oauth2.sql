-- X OAuth2 user-context read capability (confidential client) for @earl_knows_ball.
-- Adds token columns to the existing x_account row so we can read the timeline of
-- accounts we follow / find reply-worthy + idea seeds. Idempotent.
-- Applies AFTER 20260902_x_social.sql.

ALTER TABLE public.x_account ADD COLUMN IF NOT EXISTS oauth2_access_token  TEXT;   -- short-lived read token (tweet.read/users.read)
ALTER TABLE public.x_account ADD COLUMN IF NOT EXISTS oauth2_refresh_token TEXT;   -- confidential-client refresh (offline.access)
ALTER TABLE public.x_account ADD COLUMN IF NOT EXISTS oauth2_token_type    TEXT DEFAULT 'bearer';
ALTER TABLE public.x_account ADD COLUMN IF NOT EXISTS oauth2_scope         TEXT;
ALTER TABLE public.x_account ADD COLUMN IF NOT EXISTS oauth2_expires_at    TIMESTAMPTZ; -- when access token expires
ALTER TABLE public.x_account ADD COLUMN IF NOT EXISTS oauth2_connected_at  TIMESTAMPTZ;

-- PKCE state is short-lived (minutes) - only needed during the authorize->callback hop,
-- so store it in a tiny volatile table rather than clutter x_account.
CREATE TABLE IF NOT EXISTS public.x_oauth_state (
    state          TEXT PRIMARY KEY,
    created_at     TIMESTAMPTZ DEFAULT now(),
    redirect_to    TEXT,
    code_verifier  TEXT
);
