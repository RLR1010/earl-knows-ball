-- User IP + usage activity tracking (day-level presence per user + IP)
-- One row per (user, calendar day, ip_address). hit_count increments on
-- repeat hits the same day from the same IP, so the table stays tiny.
-- Added 2026-08-14 per Rich: log user IP addresses + daily usage.

CREATE TABLE IF NOT EXISTS public.user_activity (
    id            BIGSERIAL PRIMARY KEY,
    user_id       VARCHAR(36) NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    activity_date DATE NOT NULL DEFAULT CURRENT_DATE,
    ip_address    VARCHAR(45) NOT NULL,               -- IPv4 or IPv6
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    hit_count     INTEGER NOT NULL DEFAULT 1,
    UNIQUE (user_id, activity_date, ip_address)
);

-- Primary lookup path: per-user activity drilled down by date.
CREATE INDEX IF NOT EXISTS idx_user_activity_user_date
    ON public.user_activity (user_id, activity_date DESC);

-- Admin scan: find most recently active users site-wide.
CREATE INDEX IF NOT EXISTS idx_user_activity_date
    ON public.user_activity (activity_date DESC);
