-- 20260804: Add UNIQUE constraints to nfl/nba betting_lines for the
-- two-row opening/closing upsert pattern (option A — mirror MLB).
--
-- MLB.betting_lines already has UNIQUE(game_id, sportsbook, is_opening),
-- which snapshot_mlb_opening_lines relies on for its ON CONFLICT DO NOTHING
-- opening-row insert. NFL and NBA lacked it, so lines-and-picks couldn't use
-- the same ON CONFLICT logic. This aligns all three sports.
--
-- Backfill notes: verified no duplicate (game_id, sportsbook, is_opening)
-- tuples existed in either table prior to applying.

ALTER TABLE nfl.betting_lines
    ADD CONSTRAINT uq_nfl_betting_lines_game_book_opening
    UNIQUE (game_id, sportsbook, is_opening);

ALTER TABLE nba.betting_lines
    ADD CONSTRAINT uq_nba_betting_lines_game_book_opening
    UNIQUE (game_id, sportsbook, is_opening);
