-- Migration: Add extra (purchased) token balance to users
-- Date: 2026-08-23
--
-- Adds `extra_token_balance` to store one-time purchased tokens (token top-ups).
-- These are separate from the monthly allotment (`monthly_token_limit`) and
-- roll over between billing periods. They are used only as a fallback once the
-- monthly allotment for the current period is exhausted.

ALTER TABLE "users"
  ADD COLUMN IF NOT EXISTS extra_token_balance BIGINT NOT NULL DEFAULT 0;

GRANT ALL ON "users" TO CURRENT_USER;
