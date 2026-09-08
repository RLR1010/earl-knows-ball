-- 2026-09-07 (Rich): one 09:20 daily task persists the day's 6 planned tweets
-- so earlknowsball.com/admin/social/x > "Post Today" shows a stable, copyable set all day
-- (replaces the old auto-post x-daily-tweet-1..6 staggering). Nothing here posts to X.
CREATE TABLE IF NOT EXISTS public.x_daily_plan (
    plan_date   date        NOT NULL,          -- the day this lineup belongs to (America/Chicago)
    slot        int         NOT NULL,          -- 1..6 ordering as generated
    kind        text        NOT NULL,          -- 'writeup' | 'original'
    sport       text,                          -- mlb|nfl|nba (nullable)
    item_id     bigint      NOT NULL,          -- pk of the writeup/original source row
    title       text,
    url         text,
    text        text,                          -- full tweet body (caption -- "\n\n" -- url)
    posted_at   timestamptz,                   -- set when user marks "posted" (manual X post)
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_date, slot)
);
CREATE INDEX IF NOT EXISTS ix_x_daily_plan_date ON public.x_daily_plan (plan_date);
