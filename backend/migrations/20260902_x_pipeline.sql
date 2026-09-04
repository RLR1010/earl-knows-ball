-- X (@earlknowsball) content pipeline — posts we read, reply suggestions, daily tweet ideas.
-- Created 2026-09-02. Layer on top of x_following (09-02 earlier migration).

-- 0) read_posts toggle ON the followed list itself (Rich: "list who we follow, toggle whether we
--    read their posts, default to reading all 59"). TRUE by default so all followed accounts are
--    read until individually switched OFF in the admin. The reader only pulls posts for accounts
--    where read_posts = TRUE (spends credits ~$0.005/post, capped 100/day).
ALTER TABLE public.x_following ADD COLUMN IF NOT EXISTS read_posts BOOLEAN NOT NULL DEFAULT TRUE;

-- 1) Posts we've READ from followed accounts. We store every post + its created_at so we NEVER
--    re-read / re-bill content already fetched (credits @ ~$0.005/post, budget-capped 100/day).
CREATE TABLE IF NOT EXISTS public.x_posts (
    id                    BIGSERIAL PRIMARY KEY,
    tweet_id              TEXT NOT NULL,        -- X post id
    author_user_id        TEXT NOT NULL,        -- who wrote it (same as x_following.x_user_id)
    author_username       TEXT,
    text                  TEXT NOT NULL,
    created_at            TIMESTAMPTZ,          -- X author timestamp (when it was posted on X)
    read_at               TIMESTAMPTZ DEFAULT now(),  -- when WE fetched/stored it (credit moment)
    likes                 INT,
    retweets              INT,
    replies               INT,
    UNIQUE (tweet_id)
);
CREATE INDEX IF NOT EXISTS idx_x_posts_author ON public.x_posts (author_user_id);
CREATE INDEX IF NOT EXISTS idx_x_posts_created ON public.x_posts (created_at DESC);

-- 2) LLM-drafted REPLY suggestions for a specific post we decided to engage. Earl drafts the text,
--    Rich reviews + approves (or edits/discards) via admin UI before it ever gets posted.
CREATE TABLE IF NOT EXISTS public.x_reply_suggestions (
    id            BIGSERIAL PRIMARY KEY,
    post_id       BIGINT REFERENCES public.x_posts(id) ON DELETE CASCADE,
    tweet_id      TEXT NOT NULL,            -- denorm: which X post this replies to
    author_username TEXT,                   -- denorm: whom we'd be replying to
    body          TEXT NOT NULL,            -- suggested reply text (280 chars)
    rationale     TEXT,                     -- why Earl thinks this works (short)
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|posted
    created_at    TIMESTAMPTZ DEFAULT now(),
    approved_at   TIMESTAMPTZ,
    approved_by_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_x_reply_status ON public.x_reply_suggestions (status);

-- 3) Daily ORIGINAL tweet ideas (independent of who we follow). Earl generates 5/day using the
--    same research stack as public writeups (compelling games + article/vector storylines),
--    no free picks — hints + analysis instead. Human reviews + approves before posting.
CREATE TABLE IF NOT EXISTS public.x_tweet_ideas (
    id               BIGSERIAL PRIMARY KEY,
    body             TEXT NOT NULL,          -- the tweet text (<= ~280 chars)
    angle            TEXT,                   -- what story/angle this captures
    sport            TEXT,                   -- nfl|nba|mlb|general
    games_focus      TEXT,                   -- which game(s)/story it's built on
    source_links     TEXT,                   -- article(s)/material it drew from (traceable)
    status           TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|posted|discarded
    generated_for    DATE NOT NULL DEFAULT CURRENT_DATE,  -- which day's batch
    created_at       TIMESTAMPTZ DEFAULT now(),
    approved_at      TIMESTAMPTZ,
    posted_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_x_ideas_date ON public.x_tweet_ideas (generated_for);
CREATE INDEX IF NOT EXISTS idx_x_ideas_status ON public.x_tweet_ideas (status);
