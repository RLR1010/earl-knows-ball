-- X (@earlknowsball) social dashboard: account/tokens + post lifecycle.
-- Created 2026-09-02. Manual composer + approve/send (Phase 0/1); scheduler-ready later.

-- 1) The single connected X account (we are @earlknowsball, acting as ourselves).
CREATE TABLE IF NOT EXISTS public.x_account (
    id              SERIAL PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT 'x',
    handle          TEXT,                      -- e.g. earlknowsball
    api_key         TEXT,                      -- OAuth1 consumer key (API Key)
    api_secret      TEXT,                      -- OAuth1 consumer secret (API Secret)
    access_token    TEXT,                      -- OAuth1 access token (user-context, "self")
    access_secret   TEXT,                      -- OAuth1 access token secret
    scopes          TEXT[],
    user_id         TEXT,                      -- X numeric user id (from whoami)
    connected_at    TIMESTAMPTZ DEFAULT now(),
    last_probe_ok   BOOLEAN,                   -- last whoami + write-budget probe success
    last_probe_at   TIMESTAMPTZ,
    last_probe_note TEXT,
    UNIQUE (platform)
);

-- Credentials stored here are secrets; the account row is written only via admin
-- (require_admin) and read only server-side on the compute/admin box. Never in the
-- frontend. (Encryption-at-rest of the token columns can be layered on later; they're
-- stored like other Earl .env-managed service creds for now.)

-- 2) Every generated textual post idea/draft, regardless of provenance.
CREATE TABLE IF NOT EXISTS public.x_post_candidates (
    id               BIGSERIAL PRIMARY KEY,
    content_type     TEXT NOT NULL,            -- pick_card | record_update | pick_result | insight | content_link
    sport            TEXT,                     -- mlb | nfl | nba | null
    source_ref       JSONB,                    -- traceability back to real rows (game_id/pick id/etc). NO fabrication.
    draft_text       TEXT NOT NULL,            -- the tweet body
    card_image_ref   TEXT,                     -- rendered media path / already-uploaded media_id
    media_id         TEXT,                     -- X media_id once uploaded (if image)
    status           TEXT NOT NULL DEFAULT 'draft',  -- draft|queued|approved|scheduled|sent|failed|discarded
    schedule_for     TIMESTAMPTZ,              -- when approved+scheduled
    suggested_by     TEXT DEFAULT 'engine',    -- engine | human
    human_edited_at  TIMESTAMPTZ,              -- last manual edit (for review/accountability)
    human_edited_by  BIGINT,                   -- user id (FK users) of last editor
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now(),
    expires_at       TIMESTAMPTZ,              -- drop stale candidates (e.g. game start) automatically
    posted_error     TEXT,
    posted_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_x_cand_status    ON public.x_post_candidates (status, schedule_for);
CREATE INDEX IF NOT EXISTS idx_x_cand_type_sport ON public.x_post_candidates (content_type, sport);

-- 3) Publish log (idempotent): one success row per X post.
CREATE TABLE IF NOT EXISTS public.x_sent_posts (
    id           BIGSERIAL PRIMARY KEY,
    candidate_id BIGINT REFERENCES public.x_post_candidates(id),
    x_tweet_id   TEXT NOT NULL,
    text         TEXT NOT NULL,
    media_id     TEXT,
    link_url     TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_x_sent_cand ON public.x_sent_posts (candidate_id);
