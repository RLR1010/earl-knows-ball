-- Add EV-at-tip to "Earl's Winners" so the block can curate/rank by the pick's
-- model expected value (Rich 2026-09-06: "focus on our EV scores and choose the
-- picks that cashed in with our highest EV recommendations").
--
-- ev = the winning market's *_ev column at tip time (ats_ev for spread/runline
-- wins, ou_ev for over/under wins, ml_ev for moneyline wins). Stored for:
--   1) ranking winners by recommendation strength (highest EV first), and
--   2) filtering NEGATIVE-EV picks out of the block (a win on a bad-for-us pick
--      is not a "winner" to showcase).
-- Units: model expected value in $ profit per $100 stake (e.g. ml_ev 23.14).
-- Idempotent. Public-schema snapshot owned here (dev); prod keeps same shape.

ALTER TABLE public.earl_winners
    ADD COLUMN IF NOT EXISTS ev numeric;
