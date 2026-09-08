-- Reconcile DEV public.x_account to match PROD schema (OAuth2 columns).
--
-- Background: the X account is **@earl_knows_ball** (authoritative per Rich, and
-- consistent across x_oauth.py/social_x.py/etc.). The current X integration uses
-- OAuth2 tokens stored on public.x_account (single canonical row, platform='x').
-- Prod's table already has the oauth2_* columns (added when the account was
-- connected there); DEV's table was still the older OAuth1-era schema and is
-- MISSING the 6 oauth2_* columns, so the OAuth2 connect/write path (upsert keyed
-- on platform) could not run on dev. This brings dev's x_account up to prod parity.
--
-- Idempotent: each ADD COLUMN IF NOT EXISTS. Safe to run on dev only.
-- Column types copied from PROD (verified 2026-09-06):
--   oauth2_access_token text | oauth2_refresh_token text | oauth2_token_type text
--   oauth2_scope text | oauth2_expires_at timestamptz | oauth2_connected_at timestamptz
-- All nullable (dev has no live row yet; prod's row carries the tokens).

ALTER TABLE public.x_account
    ADD COLUMN IF NOT EXISTS oauth2_access_token  text,
    ADD COLUMN IF NOT EXISTS oauth2_refresh_token text,
    ADD COLUMN IF NOT EXISTS oauth2_token_type    text,
    ADD COLUMN IF NOT EXISTS oauth2_scope         text,
    ADD COLUMN IF NOT EXISTS oauth2_expires_at    timestamp with time zone,
    ADD COLUMN IF NOT EXISTS oauth2_connected_at  timestamp with time zone;
