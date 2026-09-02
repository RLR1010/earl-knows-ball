-- 20260901: Add paid-trial support to subscription_plans + seed the
-- $1.95 / 2-day Premium trial plan (converts to $29.95/mo).
--
-- trial_fee_price_id: for PAID trials, a one-time Stripe Price ID charged
--   up front at checkout alongside the recurring (conversion) price.
--   The recurring price (premium-trial -> $29.95/mo) starts billing after
--   trial_days elapses, so the trial converts to full Premium.

ALTER TABLE public.subscription_plans
    ADD COLUMN IF NOT EXISTS trial_fee_price_id VARCHAR(100);

COMMENT ON COLUMN public.subscription_plans.trial_fee_price_id IS
    'For paid trials: one-time Stripe Price ID charged up front at checkout, '
    'alongside the recurring conversion price. NULL = free trial.';

-- Seed the $1.95 / 2-day Premium trial plan. It maps to the SAME $29.95/mo
-- Stripe price as premium-monthly (price_1U7k6cF6Ga31sgSZWZF5fU48) so that
-- after the 2-day trial it converts to full Premium at $29.95/month.
-- The $1.95 trial fee is the one-time price price_1UB1Y3F6Ga31sgSZIvxNcIWw.
INSERT INTO public.subscription_plans (
    id, name, slug, description, payment_description,
    price_cents, currency, interval, kind, token_amount,
    trial_days, features, monthly_token_limit, is_active, sort_order,
    stripe_price_id, stripe_product_id, trial_fee_price_id
) VALUES (
    'trial-2d-195',
    'Premium 2-Day Trial',
    'premium-trial',
    'Full Premium access for 2 days for $1.95. After the trial, converts automatically to Premium at $29.95/month. Cancel anytime.',
    'Earl Knows Ball Premium 2-Day Trial ($1.95)',
    2995, 'usd', 'month', 'subscription', NULL,
    2,
    '["AI Handicapping Chat (NFL/MLB/NBA)", "Model Picks with Probabilities", "MLB / NFL / NBA Advanced Stats", "Daily Best Bets & Run Line Picks", "Parlay EV Guardrail", "Cancel Anytime"]',
    50000000, TRUE, 10,
    'price_1U7k6cF6Ga31sgSZWZF5fU48',  -- $29.95/mo conversion price
    'prod_V806F6rTBLTG73',
    'price_1UB1Y3F6Ga31sgSZIvxNcIWw'   -- $1.95 one-time trial fee
) ON CONFLICT (slug) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    price_cents = EXCLUDED.price_cents,
    interval = EXCLUDED.interval,
    trial_days = EXCLUDED.trial_days,
    features = EXCLUDED.features,
    is_active = EXCLUDED.is_active,
    stripe_price_id = EXCLUDED.stripe_price_id,
    stripe_product_id = EXCLUDED.stripe_product_id,
    trial_fee_price_id = EXCLUDED.trial_fee_price_id;
