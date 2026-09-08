-- "Earl's Winners" — cross-sport snapshot of recently-cashed picks (home +
-- sport-home "Earl's Winners" block). PROD first-time rollout (2026-09-06).
--
-- Combines the base table (20260906_earl_winners.sql) + the ev column
-- (20260906_earl_winners_ev.sql) into ONE idempotent migration because prod
-- never had the table at all. Idempotent: safe to re-run.
--
-- ev = the winning market's *_ev at tip time (ats_ev for spread/runline wins,
-- ou_ev for over/under wins, ml_ev for moneyline). Used to filter NEGATIVE-EV
-- picks out (a win on a -EV pick is not a winner to showcase) and shown on the
-- cards. Units: expected $ profit per $100 stake, NOT a %.
--
-- sort_key = stable display order. The refresh job now writes recency-first
-- (newest game_date first; 0 = newest) per Rich 2026-09-06. Index (sport,
-- refreshed_at DESC, sort_key DESC) supports the sport/all listing.

CREATE TABLE IF NOT EXISTS public.earl_winners (
    id              BIGSERIAL PRIMARY KEY,
    sport           TEXT NOT NULL,                 -- mlb | nba | nfl
    game_id         BIGINT NOT NULL,               -- the sport's games.id
    external_id     TEXT,                          -- mlb/nba espn game id (passthrough, optional)
    market          TEXT NOT NULL,                 -- spread | total | moneyline
    pick_text       TEXT NOT NULL,                 -- "BOS -5.5" | "Over 46.5" | "HOU ML"
    odds_at_tip     TEXT,                          -- clean display odds ("-110") @ tip
    profit          NUMERIC,                       -- *_profit column (units of $ / $100 stake)
    home_team       TEXT,                          -- abbrev @ tip
    away_team       TEXT,
    home_score      INT,
    away_score      INT,
    game_date       DATE,                          -- America/Chicago game date (when it cashed proxy)
    winning_side    TEXT,                          -- which side won: home|away|over|under|ml-home|ml-away
    sort_key        INT,                           -- recency-first display order (0 = newest)
    refreshed_at    DATE,                          -- once-per-day (Chicago) snapshot week/date it entered
    created_at      TIMESTAMPTZ DEFAULT now(),
    ev              NUMERIC,                       -- winning market's *_ev at tip ($ profit / $100)
    UNIQUE (sport, game_id, market)
);

CREATE INDEX IF NOT EXISTS idx_earl_winners_sport ON public.earl_winners (sport, refreshed_at DESC, sort_key DESC);
CREATE INDEX IF NOT EXISTS idx_earl_winners_mkt   ON public.earl_winners (market);
