-- Add timezone column to mlb.venues (IANA tz name) so local-time features
-- (week_number, day_night-verification, etc.) can be computed correctly.
-- Idempotent.
\set ON_ERROR_STOP off
ALTER TABLE mlb.venues ADD COLUMN IF NOT EXISTS timezone TEXT;
