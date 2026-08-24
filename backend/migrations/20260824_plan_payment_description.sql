-- Add editable payment_description to subscription_plans.
-- Admin sets this from /admin/plans; it's what gets shown in a user's payment
-- history for membership charges (e.g. "Monthly Premium Membership").
ALTER TABLE public.subscription_plans
    ADD COLUMN IF NOT EXISTS payment_description TEXT;
