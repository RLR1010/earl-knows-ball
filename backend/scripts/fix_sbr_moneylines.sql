-- Fix SBR moneylines in-place
-- 
-- Rule: When both home and away moneylines have the same sign 
-- (both positive or both negative), the smaller absolute value has 
-- the wrong sign. Flip it.
--
-- This applies independently to closing and opening moneylines.
-- Also recalculates implied probabilities.

BEGIN;

-- ============================================================
-- 1. Fix closing moneylines (home_moneyline, away_moneyline)
-- ============================================================

-- Case A: Both positive → the smaller value should be negative
UPDATE mlb.betting_lines
SET home_moneyline = -home_moneyline
WHERE source = 'sbr_historical'
  AND home_moneyline > 0 AND away_moneyline > 0
  AND ABS(home_moneyline) <= ABS(away_moneyline);

UPDATE mlb.betting_lines
SET away_moneyline = -away_moneyline
WHERE source = 'sbr_historical'
  AND home_moneyline > 0 AND away_moneyline > 0
  AND ABS(away_moneyline) < ABS(home_moneyline);

-- Case B: Both negative → the smaller magnitude should be positive
UPDATE mlb.betting_lines
SET home_moneyline = ABS(home_moneyline)
WHERE source = 'sbr_historical'
  AND home_moneyline < 0 AND away_moneyline < 0
  AND ABS(home_moneyline) <= ABS(away_moneyline);

UPDATE mlb.betting_lines
SET away_moneyline = ABS(away_moneyline)
WHERE source = 'sbr_historical'
  AND home_moneyline < 0 AND away_moneyline < 0
  AND ABS(away_moneyline) < ABS(home_moneyline);


-- ============================================================
-- 2. Fix opening moneylines (opening_home_moneyline, opening_away_moneyline)
-- ============================================================

-- Case A: Both positive → the smaller value should be negative
UPDATE mlb.betting_lines
SET opening_home_moneyline = -opening_home_moneyline
WHERE source = 'sbr_historical'
  AND opening_home_moneyline > 0 AND opening_away_moneyline > 0
  AND ABS(opening_home_moneyline) <= ABS(opening_away_moneyline);

UPDATE mlb.betting_lines
SET opening_away_moneyline = -opening_away_moneyline
WHERE source = 'sbr_historical'
  AND opening_home_moneyline > 0 AND opening_away_moneyline > 0
  AND ABS(opening_away_moneyline) < ABS(opening_home_moneyline);

-- Case B: Both negative → the smaller magnitude should be positive
UPDATE mlb.betting_lines
SET opening_home_moneyline = ABS(opening_home_moneyline)
WHERE source = 'sbr_historical'
  AND opening_home_moneyline < 0 AND opening_away_moneyline < 0
  AND ABS(opening_home_moneyline) <= ABS(opening_away_moneyline);

UPDATE mlb.betting_lines
SET opening_away_moneyline = ABS(opening_away_moneyline)
WHERE source = 'sbr_historical'
  AND opening_home_moneyline < 0 AND opening_away_moneyline < 0
  AND ABS(opening_away_moneyline) < ABS(opening_home_moneyline);


-- ============================================================
-- 3. Recalculate implied probabilities from fixed closing moneylines
-- ============================================================

-- Helper function for American odds → implied probability
CREATE OR REPLACE FUNCTION mlb._implied_prob(american_odds integer)
RETURNS numeric AS $$
BEGIN
  IF american_odds IS NULL OR american_odds = 0 THEN
    RETURN NULL;
  END IF;
  IF american_odds > 0 THEN
    RETURN ROUND(100.0 / (american_odds + 100)::numeric, 4);
  ELSE
    RETURN ROUND((ABS(american_odds)::numeric) / (ABS(american_odds) + 100), 4);
  END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

UPDATE mlb.betting_lines
SET home_implied_probability = mlb._implied_prob(home_moneyline),
    away_implied_probability = mlb._implied_prob(away_moneyline)
WHERE source = 'sbr_historical';

DROP FUNCTION IF EXISTS mlb._implied_prob(integer);

COMMIT;

-- Report results
SELECT 'FIX COMPLETE' as status;

SELECT
  COUNT(*) as total_sbr_lines,
  COUNT(*) FILTER (WHERE home_moneyline IS NOT NULL AND away_moneyline IS NOT NULL
                   AND home_moneyline > 0 AND away_moneyline > 0) as still_both_pos,
  COUNT(*) FILTER (WHERE home_moneyline IS NOT NULL AND away_moneyline IS NOT NULL
                   AND home_moneyline < 0 AND away_moneyline < 0) as still_both_neg,
  COUNT(*) FILTER (WHERE home_moneyline IS NOT NULL AND away_moneyline IS NOT NULL
                   AND ((home_moneyline > 0 AND away_moneyline < 0) 
                        OR (home_moneyline < 0 AND away_moneyline > 0))) as now_one_each
FROM mlb.betting_lines WHERE source = 'sbr_historical';
