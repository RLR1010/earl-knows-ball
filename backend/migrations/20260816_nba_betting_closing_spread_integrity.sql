-- 20260816_nba_betting_closing_spread_integrity.sql
--
-- Fix missing/corrupt closing_spread in nba.betting_lines_consolidated.
-- Every FINAL game must have a non-NULL, non-NaN closing_spread.
--
-- 1) Backfill the 22 final games missing closing_spread with their opening_spread
--    (best available data; all 22 have a valid opening line).
-- 2) Normalize status casing ('FINAL' -> 'final') so the invariant check is uniform.
-- 3) Add a CHECK constraint enforcing closing_spread NOT NULL (and not NaN) for final games.

BEGIN;

-- ── 1. Backfill closing_spread from opening_spread where missing / NaN / NULL ──
UPDATE nba.betting_lines_consolidated
SET closing_spread = opening_spread
WHERE (closing_spread IS NULL OR closing_spread = 'NaN'::numeric)
  AND opening_spread IS NOT NULL;

-- ── 2. Normalize status casing ──
UPDATE nba.betting_lines_consolidated
SET status = 'final'
WHERE lower(status) = 'final' AND status <> 'final';

-- ── 3. Enforce the invariant going forward ──
ALTER TABLE nba.betting_lines_consolidated
  DROP CONSTRAINT IF EXISTS betting_lines_consolidated_closing_spread_notnull_chk;

ALTER TABLE nba.betting_lines_consolidated
  ADD CONSTRAINT betting_lines_consolidated_closing_spread_notnull_chk
  CHECK (
    status IS NULL OR lower(status) <> 'final'
    OR (closing_spread IS NOT NULL AND closing_spread <> 'NaN'::numeric)
  );

-- ── 4. Sample: show any remaining violations + a backfill spot-check ──
DO $$
DECLARE
  v_bad int;
BEGIN
  SELECT count(*)
    INTO v_bad
    FROM nba.betting_lines_consolidated
   WHERE status IS NOT NULL AND lower(status) = 'final'
     AND (closing_spread IS NULL OR closing_spread = 'NaN'::numeric);
  RAISE NOTICE 'Remaining final games with missing/NaN closing_spread: %', v_bad;
END $$;

COMMIT;
