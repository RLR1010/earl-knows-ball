-- "Earl's Winners" — cross-sport snapshot of recently-cashed picks for the
-- home + sport home pages ("Earl's Winners 🏆") and eventual X receipt posts.
--
-- Design intent (see plan):
--   * Not a live query of the three sport prediction tables. This is a
--     denormalized SNAPSHOT so the winners set is pin-stable within a day:
--     the homepage block and any X post always show the SAME numbers, and the
--     "refresh every >=10 new wins but at most once per day" cadence rule is
--     owned in ONE place (the refresh job), not raced against on every page hit.
--   * A "winner" = a settled FINAL-game pick whose per-market result column
--     equals 'Win', regardless of source (api OR backtest — per Rich, do NOT
--     filter by source). Push is excluded (not a win to brag). Loss excluded.
--   * Row identity is (sport, game_id, market) so a rematerialize never
--     double-counts a pick and is idempotent.
--   * settlement is keyed on the sport's game_predictions.*_result columns
--     being set for a g.status='FINAL' game (same contract the settle/
--     update_prediction_results writers use). game_date (America/Chicago) is
--     the "when it cashed" proxy for the once-per-day + >=10 gate.
--
-- Backwards compatible / safe: CREATE TABLE IF NOT EXISTS. New feature; no
-- existing rows to alter.

CREATE TABLE IF NOT EXISTS public.earl_winners (
    id              BIGSERIAL PRIMARY KEY,
    sport           TEXT NOT NULL,                 -- mlb | nba | nfl
    game_id         BIGINT NOT NULL,               -- the sport's games.id
    external_id     TEXT,                          -- mlb/nba espn game id (passthrough, optional)
    market          TEXT NOT NULL,                 -- spread | total | moneyline
    pick_text       TEXT NOT NULL,                 -- "BOS -5.5" | "Over 46.5" | "HOU ML" (for rendering)
    odds_at_tip     TEXT,                          -- clean display odds ("-110") @ tip
    profit          NUMERIC,                       -- *_profit column (units of $ / $100 stake) if present
    home_team       TEXT,                          -- abbrev @ tip
    away_team       TEXT,
    home_score      INT,
    away_score      INT,
    game_date       DATE,                          -- America/Chicago game date (when it cashed proxy)
    winning_side    TEXT,                          -- which side won: home | away | over | under | ml-home | ml-away
    sort_key        INT,                           -- stable display order (bigger profit first, then recency)
    refreshed_at    DATE,                          -- the once-per-day (Chicago) snapshot week/date it last entered
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (sport, game_id, market)
);
CREATE INDEX IF NOT EXISTS idx_earl_winners_sport  ON public.earl_winners (sport, refreshed_at DESC, sort_key DESC);
CREATE INDEX IF NOT EXISTS idx_earl_winners_mkt    ON public.earl_winners (market);
