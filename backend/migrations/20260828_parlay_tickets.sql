-- ---------------------------------------------------------------------------
-- 20260828_parlay_tickets.sql
-- Saved parlay tickets (premium feature). A ticket holds a user's parlay
-- picks across ANY combination of sports (mlb | nfl | nba), serialized as a
-- JSONB legs array so a single ticket can mix legs from all three sports.
--
-- The legs array shape mirrors the frontend ParlayLeg shape so a saved
-- ticket round-trips verbatim:
--   {
--     "game_id": <int>, "sport": "mlb|nfl|nba", "kind": "ml|spread|total",
--     "label": "CHC ML", "pick": "...", "side": "CHC"|null,
--     "prob": <float|null>, "odds": <float|null>, "decimal": <float|null>,
--     "ev": <float|null>, "model_file": "...", "is_calibrated": <bool>,
--     "favorite_side": "...", "game_label": "...", "game_date": "..."
--   }
-- game_label/game_date snapshot display info at save time so a saved ticket
-- renders even after the games resolve / are pruned from the "upcoming" legs list.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS parlay_tickets;

CREATE TABLE parlay_tickets (
    id            BIGSERIAL PRIMARY KEY,
    user_id       VARCHAR(36) NOT NULL,
    name          TEXT NOT NULL DEFAULT 'My Parlay',
    legs          JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT parlay_tickets_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_parlay_tickets_user_id
    ON parlay_tickets (user_id, updated_at DESC);
