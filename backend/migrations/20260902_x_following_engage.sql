-- X (@earlknowsball) following ingest + engagement targets.
-- Created 2026-09-02. Flow: download who we follow (cheap) -> pick accounts actually worth
-- spending credits to read -> optionally mark ones we want to reply to.

-- 1) Snapshot of everyone @earl_knows_ball follows (refreshed on demand; inexpensive list lookup).
--    This is source data only - no per-account post reads here yet (those cost credits).
CREATE TABLE IF NOT EXISTS public.x_following (
    id          BIGSERIAL PRIMARY KEY,
    x_user_id   TEXT NOT NULL,             -- X numeric id of the followed account
    username    TEXT NOT NULL,             -- @handle
    name        TEXT,                      -- display name
    description TEXT,                      -- bio (helps decide if worth reading without credits)
    snapshot_at TIMESTAMPTZ DEFAULT now(), -- when this row was pulled
    UNIQUE (x_user_id)
);
CREATE INDEX IF NOT EXISTS idx_x_following_user ON public.x_following (x_user_id);
CREATE INDEX IF NOT EXISTS idx_x_following_snap ON public.x_following (snapshot_at);

-- 2) Accounts we've decided are worth paying attention to: read their posts (spends credits)
--    and/or are ones we want to reply to ("should we invest a reply"). Decided by human in the
--    admin UI from the x_following download; the engine may suggest candidates but never auto-enrolls.
CREATE TABLE IF NOT EXISTS public.x_engage_targets (
    id             BIGSERIAL PRIMARY KEY,
    x_user_id      TEXT NOT NULL,
    username       TEXT NOT NULL,          -- denormalized for display
    read_posts     BOOLEAN NOT NULL DEFAULT TRUE,  -- spend credits reading this account's recent posts
    want_to_reply  BOOLEAN NOT NULL DEFAULT FALSE, -- surfaced as a "possibly respond?" queue item
    reply_grounds  TEXT,                   -- note/memo: why we want to engage (e.g. "beat writer, replies to fans")
    added_by       BIGINT,                 -- user id that enrolled this target (audit)
    added_at       TIMESTAMPTZ DEFAULT now(),
    last_post_read_at TIMESTAMPTZ,         -- last time we pulled their posts (credit usage)
    UNIQUE (x_user_id)
);
CREATE INDEX IF NOT EXISTS idx_x_engage_tgt ON public.x_engage_targets (read_posts, want_to_reply);
