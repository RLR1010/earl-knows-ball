-- 20260818_mlb_pgs_lookup_indexes.sql
--
-- Add read-path indexes to mlb.pitcher_game_stats to eliminate the per-row
-- full-table scans in mlb.data_loader.load_games()'s heavy LATERALs.
--
-- These are PURELY ADDITIVE, non-unique B-tree indexes on READ columns. They do
-- NOT change any table structure, row semantics, unique constraint, or the
-- ingestion write path — boxscore_ingest keeps INSERT/ON CONFLICT behavior
-- byte-identical; the new indexes are simply maintained automatically by the
-- existing writes (slightly faster reads, negligible insert overhead). This
-- cannot alter ingested data.
--
-- Which LATERALs they serve (mlb/data_loader.py):
--   * pgs_h / pgs_a  : WHERE pitcher_name = g.home_pitcher_name / away_pitcher_name
--                      AND is_starter = TRUE  -> needs (pitcher_name, is_starter)
--   * vph   / vpa    : WHERE pitcher_mlb_id = pgs_h/a.pitcher_mlb_id AND is_starter = TRUE
--                      (venue ERA, joined to games for venue_id + date) -> needs
--                      (pitcher_mlb_id, is_starter)
--   (game_id is appended so the subsequent games join + ORDER BY date is served
--    cheaply; prs_h/prs_a already have covering indexes on pitcher_rolling_stats.)
--
-- Prior state: the only pgs index keyed by pitcher was the partial unique
-- (mlb_game_id, pitcher_mlb_id) WHERE pitcher_mlb_id IS NOT NULL — which cannot
-- be used for "all starts by this pitcher" lookups, so every LATERAL fell back
-- to a full 147k-row scan, ~69,678 times per load_games().
-- -------------------------------------------------------------------------------

BEGIN;

CREATE INDEX IF NOT EXISTS ix_mlb_pgs_pitcher_name_starter
    ON mlb.pitcher_game_stats (pitcher_name, is_starter, game_id);

CREATE INDEX IF NOT EXISTS ix_mlb_pgs_pitcher_id_starter
    ON mlb.pitcher_game_stats (pitcher_mlb_id, is_starter, game_id);

-- Also let the venue-ERA lookups pick the target park without a games row scan:
-- pgs (pitcher_mlb_id) -> player's start sets, joined to games for venue. The
-- (pitcher_mlb_id, is_starter, game_id) index above already narrows those start
-- sets; no further index is required for correctness.

COMMIT;
