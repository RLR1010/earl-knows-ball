-- Add pricing_visible to subscription_plans.
-- Admin toggles this from /admin/plans ("Show on site"). When false, the plan's
-- card is hidden from every public plan-offer surface (the /pricing page and the
-- subscribe gating modal) — i.e. wherever a visitor is given a choice of plans.
-- It does NOT affect active subscriptions, direct checkout links, renewals, or
-- token top-up resolution, so hiding never breaks someone mid-purchase.
ALTER TABLE public.subscription_plans
    ADD COLUMN IF NOT EXISTS pricing_visible BOOLEAN NOT NULL DEFAULT TRUE;
