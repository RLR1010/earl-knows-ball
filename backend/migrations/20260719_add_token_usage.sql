-- Migration: Add token usage tracking
-- Date: 2026-07-19
--
-- Creates:
--   1. user_token_usage table (monthly token tracking per user)
--   2. monthly_token_limit column on users table

-- Add monthly_token_limit column to users table (null = unlimited)
ALTER TABLE "users"
  ADD COLUMN IF NOT EXISTS monthly_token_limit BIGINT;

-- Create user_token_usage table
CREATE TABLE IF NOT EXISTS user_token_usage (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month DATE NOT NULL,
    tokens_used BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, month)
);

-- Index for fast lookups by user + current month
CREATE INDEX IF NOT EXISTS idx_token_usage_user_month
    ON user_token_usage (user_id, month);

-- Index for admin queries across users
CREATE INDEX IF NOT EXISTS idx_token_usage_month
    ON user_token_usage (month);

-- Grant permissions (assuming the app role already has access to public schema)
GRANT ALL ON user_token_usage TO CURRENT_USER;
GRANT ALL ON SEQUENCE user_token_usage_id_seq TO CURRENT_USER;
