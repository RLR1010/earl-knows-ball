-- 2026-08-22 pick_card_section column on features tables
-- Add a categorization column to each sport's `features` table that groups
-- pick-card features into display sections for the Detailed Analysis -> Stats view.
-- Valid values: 'home_stats', 'away_stats', 'game_context', 'betting_lines', 'other'
-- (see frontend/src/app/admin/features/page.tsx dropdown + the Detailed Stats renderer).
--
-- The column is nullable + unconstrained (values validated at the app layer) and
-- defaulted to NULL so existing rows are backfilled by the categorize script
-- (app/scripts/categorize_pick_card_sections.py) rather than guessing here.

ALTER TABLE mlb.features
    ADD COLUMN IF NOT EXISTS pick_card_section VARCHAR(32);

ALTER TABLE nfl.features
    ADD COLUMN IF NOT EXISTS pick_card_section VARCHAR(32);

ALTER TABLE nba.features
    ADD COLUMN IF NOT EXISTS pick_card_section VARCHAR(32);
