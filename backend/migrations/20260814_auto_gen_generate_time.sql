-- 20260814: Add generate_time to auto_generation_configs
-- Per-config preferred time-of-day (HH:MM, config timezone America/Chicago) for
-- cadence-based generation. When set, a daily config is due once per calendar
-- day at/after this time instead of on a rolling 24h window.
ALTER TABLE public.auto_generation_configs
    ADD COLUMN IF NOT EXISTS generate_time text;
