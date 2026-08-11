# MLB Imputation Table — Draft for Review

**Goal:** Separate what the **model sees** (feature vector) from what the **user sees** (pick-card handicapping info). The raw data layer must preserve real values / NULL. Imputation (filling missing values) happens only for the model input, using a *reasoned prior* — never a blind `0` that the model could misread as "great/dominant" when the truth is "no data."

**Rules (same for every feature):**
- **User path:** show the real value when present; show blank/`—`/N/A when missing. Never a fallback, never 0.
- **Model path:** missing → imputed value per the table below. NULL-able only if the model is trained to ingest NULLs (it currently isn't — all features are numeric/0-filled).
- No retrain needed now (Rich handles it). These make the live model *closer* to correct in the interim and correct after retrain.

---

## A. Pitcher stats (the biggest offenders)

| Feature | What it is | Current fallback | Model imputation |
|---|---|---|---|
| `h_pitcher_venue_era` / `a_pitcher_venue_era` | ERA at THIS park, multi-season | `<home_era_ytd>` / `<road_era_ytd>` proxy (now real venue ERA) | **Road ERA** (away pitcher) / **Home ERA** (home pitcher) — closest true prior |
| `h_pitcher_venue_starts` / `a_pitcher_venue_starts` | # starts at this park | — (new) | `0` is CORRECT (genuinely never pitched there) — keep 0 |
| `h_pitcher_home_era` / `a_pitcher_road_era` | Home / road ERA split | season ytd ERA | **season ytd ERA** |
| `h_pitcher_day_era` / `a_pitcher_day_era` | Day-split ERA | season ytd ERA | **season ytd ERA** |
| `h_pitcher_night_era` / `a_pitcher_night_era` | Night-split ERA | season ytd ERA | **season ytd ERA** |
| `h_p*_kbb` / `a_p*_kbb` (20,10) | K/BB split windows | `0.0` | **season ytd K/BB** |
| `h_pitcher_rest` / `a_pitcher_rest` | days rest | `0` | clamped `0`… but **use 5 (standard)** if absent? see ⚠️ below |

⚠️ `rest` fallback to `0` (= pitched back-to-back) is misleading — a missing rest (e.g. opener/unknown) isn't "0 days." Recommend **league-avg rest (~4-5)**.

## B. Team rolling stats (early-season sparsity)

| Feature | Current fallback | Model imputation |
|---|---|---|
| `h_avg_5/10`, `a_avg_5/10` | prior-season avg (`h_prior_avg`) | **prior-season avg** (currently correct — keep) |
| `h_ops_5/10`, `a_ops_5/10` | prior-season OPS | prior-season OPS (keep) |
| `h_era_5/10`, `a_era_5/10` | prior-season ERA | prior-season ERA (keep) |
| `h_whip_5/10`, `a_whip_5/10` | prior-season WHIP | prior-season WHIP (keep) |
| `h_cum_avg/ops/era/whip`, `a_cum_*` | prior-season | prior-season (keep) |
| `h_home_rf` / `a_away_rf` | runs/0 → 0 | **league avg runs/game** (not 0) |
| team venue win% (`a_team_venue_winpct`) | `0.5` | `0.5` (neutral — correct) |

## C. Team season stats / perc
| Feature | Current fallback | Model imputation |
|---|---|---|
| `h_winpct` / `a_winpct` | `0.5` | `0.5` neutral (correct) |
| `h_over_freq`, `h_over_freq5`, `a_over_freq`, `a_over_freq5` | `0.5` | `0.5` neutral (correct) — could use league over rate |
| `h_implied` / `a_implied` (ML→prob) | `0.5` | `0.5` neutral (correct) |

## D. Park/venue
| Feature | Current fallback | Model imputation |
|---|---|---|
| `park_factor`, `home_park_factor`, `away_park_factor` | `100` | `100` neutral (correct) |
| `is_dome` | `0` | `0` (correct) |

## E. Weather (pre-game missing)
| Feature | extract default | Model imputation |
|---|---|---|
| `temperature` / `temp` | `80.0` | **season/league avg temp** (~69) — 80 overstates summer heat |
| `humidity` | `50.0` | **league avg humidity** (~55) |
| `wind_speed` | `5.0` | **5 mph** (reasonable — keep, maybe league avg) |
| `wind_calculated` | `0` | `0` when no wind direction (correct) |

## F. Situational / misc (0-fills — mostly CORRECT, verify)
| Feature | Current | Verdict |
|---|---|---|
| `travel_miles`, `tz_diff`, `rest_h_hours`, `rest_a_hours`, `rest_diff_hours` | `0` | OK (0 = no travel/neutral) — verify |
| `week_number` | `0` | should compute, not 0 |
| `is_home_fav`, `is_home_underdog` | `0` | OK (binary) |
| `ml_implied_movement`, `combo_era_r10_diff` | `0.0` | OK if genuinely no line movement |
| `has_verified_ou` | `fillna(False)` | OK (bool) |
| `day_night` | `'N'` (night) | verify against actual start time |
| `h_bp` / `a_bp` bullpen ERA/IP | `0` | **league-avg bullpen ERA**, not 0 |
| `era_l5` long-bullpen | `4.5` | OK as neutral |

---

## Summary of changes requiring your sign-off
1. **Venue ERA** → real value / NULL for user; **home/road ERA** for model. (+ keep `venue_starts` 0 = genuine)
2. **Split ERA (home/road/day/night)** missing → season ytd ERA (not 0).
3. **K/BB split** missing → season ytd K/BB (not 0).
4. **Pitcher rest** missing → **~4-5 days** (not 0).
5. **Weather defaults** → league-avg temp/humidity (not 80/50), wind 5.
6. **Team runs/game (`home_rf`/`away_rf`)** missing → league avg (not 0).
7. **Bullpen ERA** missing → league avg (not 0).
8. **week_number** → compute real week instead of 0.
9. Everything currently at a **neutral 0.5 / 100 / 0 (binary)** stays as-is (already sensible).

**Questions for you:**
- Pitcher rest: OK to use ~4-5 league-avg days instead of 0? 
- Weather: OK to switch temp/humidity defaults to league averages?
- Do you want real **venue bullpen** or venue **team-OPS** style stats added too (beyond what's listed), or scope to fixing existing fallbacks only?
