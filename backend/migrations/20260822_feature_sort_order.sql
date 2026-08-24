-- 2026-08-22 sort_order column on features tables
-- Add a display-ordering column to each sport's `features` table used ONLY for
-- the Detailed Analysis Stats frontend ordering (and the admin Features page).
-- It does NOT affect training/inference: the data_loader continues to load
-- features in its own order for model input. This column only drives the order
-- features are rendered in the frontend JSON (enriched read-time).
--
-- Nullable + unconstrained; backfilled by app/scripts/backfill_feature_sort_order.py.

ALTER TABLE mlb.features
    ADD COLUMN IF NOT EXISTS sort_order INTEGER;

ALTER TABLE nfl.features
    ADD COLUMN IF NOT EXISTS sort_order INTEGER;

ALTER TABLE nba.features
    ADD COLUMN IF NOT EXISTS sort_order INTEGER;
