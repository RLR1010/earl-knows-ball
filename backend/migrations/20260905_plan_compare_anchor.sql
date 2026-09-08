-- Add compare_against_monthly_cents to subscription_plans.
-- Optional per-plan monthly-rate anchor used to compute a dynamic "save X%"
-- badge on longer-term plans (e.g. an annual plan compared against the
-- standard $29.95/mo rate). If NULL, no save-% badge is shown for that plan.
ALTER TABLE public.subscription_plans
    ADD COLUMN IF NOT EXISTS compare_against_monthly_cents INTEGER;

-- Premium Annual compares against the $29.95 monthly membership rate.
UPDATE public.subscription_plans
SET compare_against_monthly_cents = 2995
WHERE slug = 'premium-annual';
