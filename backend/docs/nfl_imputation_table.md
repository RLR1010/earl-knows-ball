# NFL Imputation Table — two-path (model vs pick card)

**Principle (same as MLB):** what the model trains/predicts on ≠ what the pick
card shows the user.
- **Pick card (user):** real value, or blank/`—` when missing. Never blind 0,
  never a fabricated number that a 0 would imply ("0.0 passer rating" or "0 O/U
  movement" are lies).
- **Model:** a *reasoned prior* (prior-season / season-ytd / league-avg), never
  blind 0 (a 0 reads to the model as "dominant/zero").

NFL is farther along than MLB was: its prior-season fill (`_first_fill` +
`_prior_fill`, from `nfl.prior_team_stats`) **already works** (MLB's was dead).
So most team-stats are already seeded correctly from prior-season on Week 1.
This table covers the **blind-0 / flat defaults** that still leak, and the
**model-path blind-0** (`_extract_feature_vector` line 315).

## A — build_features blind-0 fills that leak to the pick card

| # | Line(s) | Column(s) | Current | Pick-card show | Model prior (fallback) |
|---|---------|-----------|---------|----------------|------------------------|
| 1 | 1837-38 | `home_rest_days`, `away_rest_days` | `.fillna(7)` | 7 (standard rest week — OK to keep, it's a real prior) | 7 (unchanged) |
| 2 | 1854-55 | `sp_h_odds_mvmt`, `sp_a_odds_mvmt` | opening odds `.fillna(0)` → movement | **blank** (missing opening line = no movement data) | 0 via prior-season movement? OR blank→league-avg. Recommend: blank on card, prior-avg to model |
| 3 | 1891 | `*_pct_r*` opening-line-derived fills | `.fillna(0.5)` | blanket 0.5 (neutral) | prior-season cover% |
| 4 | 2042 | rolling `*_r5` | `.fillna(0.0)` | blank | prior-season value via `_first_fill` |
| 5 | 2049/2063/2069 | `*_atsp`/`*_ouv` style rates | `.fillna(0.5)` | neutral 0.5 | prior-season |
| 6 | 2166-67 | `home_ats_home_pct_r5`, `away_ats_away_pct_r5` | `.fillna(0.5)` | neutral 0.5 | prior-season home/road ATS% |
| 7 | 2214/2217 | `venue_elevation_ft` | `.fillna(0.0)` | 0 = sea level (real) | keep 0 |
| 8 | 2221-22 | injury weights | `= 0.0` | 0 = no injuries (real) | keep 0 |
| 9 | 2680 | all QB feature columns | `.fillna(0.0)` | **blank** (missing QB stat → blank, not 0.0) | season-ytd / prior-season QB value, else league-avg QB would read 0 = "abysmal QB" |
| 10 | 2546 | team-stats final `.fillna(0.0)` | blind 0 | only fires when NO prior data exists | prior-season (already filled) — last resort keep prior/league-avg |

## B — Legitimate (keep as-is)

- L2037, 2092, 2102 `.fillna(0)` on boolean flags (e.g. "won last meeting") — 0 = false, real.
- `_first_fill` / `_prior_fill` prior-season seeding — **the good pattern**, keep.
- Win-streak reset `.fillna(0)` — real.

## C — Model-path blind-0 (`_extract_feature_vector`, engine.py:315)

Current: `if val is None or nan: val = 0.0`.
Fix: route through an imputation layer `_impute_feature(row, feat)` that:
- rest days → 7
- team stats → prior-season (already in row via `_first_fill`, so rarely fires)
- QB stats → prior/season-ytd or league-avg (never 0.0)
- rates/pcts → prior-season or 0.5-neutral only as last resort
- never blind 0

---

## Rollout (mirror MLS's incremental, verify-each-group)
1. Loader: stop blind-0 filling QB (#9) + odds-movement (#2) + the rate fills
   (#3/#5/#6) that aren't prior-season; let NaN flow so pick card blanks.
2. Engine: add `_impute_feature`, route `_extract_feature_vector` through it,
   replacing the single `val=0.0`.
3. Verify per-group with a new-QB / first-game / missing-line scenario:
   model gets prior, pick card blanks.
4. Update this doc as rules land. NFL/NBA pickup is a separate later pass.
