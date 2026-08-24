-- Migration: Add kind + token_amount to subscription_plans for one-time token top-ups
-- Date: 2026-08-23
--
-- kind: "subscription" (recurring, default) or "token_topup" (one-time token purchase)
-- token_amount: the one-time token grant for a token_topup plan (nullable for subscriptions)

ALTER TABLE "subscription_plans"
  ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'subscription';
ALTER TABLE "subscription_plans"
  ADD COLUMN IF NOT EXISTS token_amount BIGINT;

-- Make token-topup plans easy to find
CREATE INDEX IF NOT EXISTS idx_subscription_plans_kind ON "subscription_plans"("kind");

GRANT ALL ON "subscription_plans" TO CURRENT_USER;
