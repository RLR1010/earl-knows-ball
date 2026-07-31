# NBA Rolling Stats & Feature Redesign

Status: DRAFT for review (2026-07-31)
Goal: Bring the NBA engine to parity with MLB/NFL — pre-compute stats in the DB,
replace the pandas-side rolling computation in `data_loader.py`, and complete the
data coverage back to 2016+.

---

## 1. Current State Audit (run 2026-07-31)

All queries against `earl_knows_football`, `nba` schema. `season_id = year - 1990`,
so season 26 = 2016-17, season 35 = 2025-26.

### Coverage by table (REG season games)

| Table | Earliest | Coverage | Notes |
|---|---|---|---|
| `nba.games` (scores) | 2006-07 | ✅ 100% | 20 full REG seasons, ~23k games + PRE/POST |
| `nba.games` (team box) | 2006-07 | ⚠️ ~98.7% | Seasons 22-27 (2012-13 → 2017-18) missing detailed home box (FGM/FGA/3PM/…) on **~164 games/season (~13%)**. Scores ARE present. |
| `nba.player_season_stats` | 2006-07 | ✅ full | ~460-615 players/season |
| `nba.player_game_stats` | **2020-21 only** | ❌ | **ZERO rows before season 30.** ~25k rows/season for 30-35 (looks complete there) |
| `nba.betting_lines_consolidated` | 2006-07 | ❌ sparse | Complete for 30-35 (2020-21+). Seasons 16-29 only **~13-31%** of games have lines (157-377/season). |
| `nba.betting_lines` (raw per-book) | **2020-21 only** | ❌ | 0 rows before season 30 |
| `nba.cumulative_game_stats` | 2006-07 | ⚠️ all seasons | But row counts exceed 2×games in seasons 22-27 (e.g. 2840 rows vs 2464 = 2.3×) → duplicates/mixed content; needs full redo |
| `nba.features` (catalog) | — | ✅ | 210 feature definitions (catalog only, not values) |
| `nba.dfs_salaries` | — | ❌ **0 rows** | Scraper target, never populated |
| `nba.team_props` / `nba.player_season_props` / `nba.player_daily_props` | — | ❌ **0 rows** | FD scraper targets, never populated for NBA |
| `nba.game_predictions` | — | ✅ | 6,605 rows (backtest predictions) |
| `nba.training_runs` | — | ✅ | 147 rows |

### Gaps that block "complete back to 2016"

1. **❌ Betting lines 2016-17 → 2019-20** (seasons 26-29): only ~15-20% of games had
   consolidated lines. **✅ FIXED 2026-07-31** — Kaggle "Basketball Betting Dataset"
   (visualize25) has opening + closing spread/OU/ML for every game 2007-08 → 2020-21.
   - `backfill_nba_lines_old.py`: fixed date-match bug (UTC timestamptz → ET date
     conversion) — match rate went from 6,590 rows to **32,158 rows (16,079 games)**.
   - Fixed `_implied_probability()` positive-odds bug (+900 → 0.1, was 1000) and
     aligned convention to 0-1 fraction (matches odds-API rows).
   - NEW `nba_betting_lines_consolidate_historical.py`: rebuilds consolidated for
     season_id < 30 from `betting_lines_old` (16,012 games, provenance `'nba_old'`).
   - Coverage now: **seasons 17-29 (2007-08 → 2019-20) ~99.9% REG**; seasons 30+
     unchanged (The Odds API).
2. **❌ Player game stats before 2020-21** — no player-level features pre-2020-21.
   **🔄 BACKFILLING 2026-07-31** — `nba_player_game_stats_run.py` (ESPN core API,
   resume-able) extended to accept season args; running seasons 2016-2019 in bg.
   ESPN core API verified working for 2016-17 games (per-athlete stats endpoints).
3. **⚠️ Missing detailed team box** on ~990 games (both sides) in 2012-13 → 2017-18 —
   blocked possession-based stats for those games (scores still usable).
   **🔄 FIX PLAN**: `nba_repair_team_boxes.py` sums player_game_stats → team box
   (verified 1:1 exact against games where both exist). Runs after player backfill;
   covers seasons 26-27 in the 2016+ window. Seasons 22-25 stay partially missing
   (below the 2016 requirement — reingest later if desired).
4. **❌ DFS salaries / team props / player props empty** — market-based signals not present.
5. **❌ `nba.features` is a catalog only** — per-game feature VALUES are not persisted
   anywhere; `build_features()` recomputes everything from scratch in pandas each run.
   (NFL/MLB store derived per-game features in `game_predictions` JSON during backtest;
   NBA needs the same discipline.)

### Where the current loader computes things in pandas
`data_loader.py::build_features()` currently computes on the fly (per MEMORY + code read):
- Rolling windows (5/10/20) of scoring, ORTG/DRTG/net, pace, shooting splits
- Recency-weighted (rw3/rw5) team stats
- ATS/SU/OU streaks + form, rest days, B2B, travel, altitude
- Opponent-adjusted stats

This is exactly what MLB/NFL moved into `team_rolling_stats` tables. NBA should do the same.

---

## 2. Target Architecture (mirror MLB/NFL)

### 2a. NEW `nba.rolling_game_stats` — the workhorse table

One row per `(game_id, team_id, team_side)`, **computed through the PRIOR game only**
(no look-ahead). All stats pre-computed via SQL window functions in one backfill,
then incremental refresh each day.

Schema (modeled on `nfl.team_rolling_stats` + `mlb.team_rolling_stats`):

```
game_id            int      PK
team_id            int      PK
team_side          text     'home' | 'away'
season_id          int
game_date          date
games_played       int      games this season before this game

-- Season cumulative (through prior game)
cum_ppg, cum_oppg, cum_net_margin, cum_fg_pct, cum_3p_pct, cum_ft_pct,
cum_reb, cum_ast, cum_stl, cum_blk, cum_tov, cum_pf,
cum_ortg, cum_drtg, cum_net_rtg, cum_pace, cum_ts_pct, cum_efg_pct, cum_poss

-- Rolling windows L5 / L10 / L20  (stat_r5 / stat_r10 / stat_r20)
off_pts_*, off_fg_pct_*, off_3p_pct_*, off_ft_pct_*, off_reb_*, off_ast_*,
off_stl_*, off_blk_*, off_tov_*, off_pf_*,
def_pts_*, def_fg_pct_*, def_3p_pct_*, def_ft_pct_*, def_reb_*, def_ast_*,
def_stl_*, def_blk_*, def_tov_*, def_pf_*,
ortg_*, drtg_*, net_rtg_*, pace_*, ts_pct_*, efg_pct_*, poss_*,
win_pct_*, cover_pct_*, ou_over_pct_*, margin_*, ats_margin_*, ou_margin_*

-- Betting form (season)
season_wins, season_losses, season_win_pct, season_ats_pct, season_ou_over_pct
win_streak, loss_streak, cover_streak, ou_streak

-- Volatility & ranks
off_pts_stddev_r5, def_pts_stddev_r5, off_pts_stddev_r10, def_pts_stddev_r10
off_scoring_rank, def_scoring_rank, off_rating_rank, def_rating_rank, pace_rank

-- Situational (rolling)
home_win_pct_r10, away_win_pct_r10 (computed over that split where meaningful)
rest_days          int      days since last game
is_b2b             boolean
games_last_7d      int      schedule density

feeds_into_game_id int      the game whose features this row feeds (mirror NFL)
```

Notes:
- Windows: use **3/5/10/20** — NBA season is 82 games, 20-game windows capture
  a "quarter-season" of form; 3-catch short-term momentum.
- Rolling = strictly prior games only (the row for game X is computed from games
  before X, so the loader can join directly without shift tricks).
- Index: `(team_id, game_date)`, `(game_id)`.

### 2b. REDO `nba.cumulative_game_stats` — pure per-game raw + season accumulators

Strip the rolling/streak/rank content out of it. Keep it as the atomic per-game
team stat layer (mirror `mlb.cumulative_game_stats`):

- One row per `(game_id, team_id)` = the box score + derived per-game metrics
  (possessions, ORTG, DRTG, pace, TS%, eFG%, margin).
- Plus season-cumulative running totals (points, FGM/FGA/3PM/3PA/FTM/FTA, REB,
  AST, STL, BLK, TOV, PF, games_played, minutes).
- The rolling table is computed FROM this table. Both get rebuilt together.

This fixes the current duplication/mixed-content issues (2.3 rows/game in some
seasons) — the rebuild truncates and regenerates deterministically.

### 2c. `data_loader.py` refactor

`build_features()` shrinks to:
1. Load games + lines (unchanged).
2. LEFT JOIN `nba.rolling_game_stats` twice (home side / away side via
   `feeds_into_game_id` or `(team_id, game_date)`).
3. Add cross-team differential features (home_off_pts_r5 - away_def_pts_r5, etc.)
   and game-level combos (total pace, sum of ORTG, rest differential).
4. Persist per-game feature values (new `nba.feature_values` or into
   `game_predictions` JSON) so backtests/training are reproducible from the DB.

---

## 3. Brainstorm — additional tables/data/features for a robust NBA feature set

### Priority 1 — needed for a complete team handicapping model
| Item | Table | Why |
|---|---|---|
| Closing lines 2016-2020 backfill | `nba.betting_lines_consolidated` | Can't train/backtest ATS/OU without it. **Source needed: Sportsbook Review (like NFL), Kaggle NBA odds archives, or `nba_api` historical lines.** |
| Prior-season team stats | `nba.prior_team_stats` (NEW, mirror MLB) | Early-season signal before rolling windows fill; carry-over form + roster continuity |
| Team splits | `nba.team_splits` (NEW, mirror MLB) | Home/away, vs .500+, division/conference, day-of-week, rest categories — situational edges |
| Venues/travel | `nba.team_venues` (NEW, static) | Arena, city, lat/long, altitude (Denver!), capacity, timezone — travel miles + altitude features |
| Injuries/availability | `nba.injuries` (exists?) + `nba.player_status` | Load management + star-player availability is THE NBA edge; drives lines |
| Feature values persistence | `nba.feature_values` or game_predictions JSON | Reproducible training/backtest (see 2c) |

### Priority 2 — player-level (needs player_game_stats backfill first)
| Item | Table | Why |
|---|---|---|
| Player game stats 2016-2020 | `nba.player_game_stats` backfill | Source: nba_api (stats.nba.com) or basketball-reference scrape |
| Player rolling stats | `nba.player_rolling_stats` (NEW, mirror mlb.pitcher_rolling_stats) | Star player form: top-5 scorers' PPG/USG/MPG trends, usage, availability |
| Player season props / daily props | FD scraper targets (exist, 0 rows) | Market player props — Earl's props chat features |
| DFS salaries | `nba.dfs_salaries` | Market valuation signal per player per night |

### Priority 3 — advanced/context
| Item | Why |
|---|---|
| ELO/ratings table | 538-style rating + recency — strong prior for net rating |
| Rest/schedule density | games in last 7/14 days, miles traveled, altitude swings |
| All-star break / trade deadline flags | Post-deadline roster changes break rolling stats |
| New coach / roster continuity | Offseason churn vs prior_team_stats |
| Betting market context | Line movement (open→close), CLV, public% — from odds history |
| 4 Factors (Oliver) | eFG%, TOV%, ORB%, FTA rate — the canonical NBA team descriptors; computable from existing box data |

### Feature set summary for the model
Team level: ORTG/DRTG/net (rolling 5/10/20 + season), pace, 4 factors, shooting
splits, rebounding %, ATS/OU form + margins, streaks, rest/B2B/travel/altitude,
home/away splits, prior-season carryover, line movement.
Player level (later): star availability, top-5 usage/MPG form, injury-weighted team strength.

---

## 4. Implementation Plan (proposed order)

1. **Write `nba/rolling_stats.sql`** — DDL for `nba.rolling_game_stats` (+ redone
   `nba.cumulative_game_stats`). ~2-3h including column list sign-off.
2. **Write `nba/populate_rolling_stats.py`** — SQL-window backfill (all seasons)
   + incremental refresh (today's games only). Mirrors MLB `populate_rolling.py`.
3. **Redo `nba/cumulative_stats.py`** — clean per-game raw + season accumulators.
4. **Refactor `nba/data_loader.py`** — replace pandas rolling with joins;
   persist feature values; keep `nba.features` catalog in sync.
5. **Backfill betting lines 2016-2020** — find source (SBR prediction-market lines
   like NFL, or nba_api historical). Blocks meaningful ATS/OU backtest pre-2020.
6. **Backfill player_game_stats 2016-2020** (nba_api) + build player_rolling_stats.
7. **prior_team_stats + team_splits + venues** tables.
8. **Injuries/availability + FD props/DFS** — wire the scrapers that already exist.

## 5. Open Decisions for Rich

1. Rolling window set: **3/5/10/20** OK, or stick to 5/10/20?
2. Betting-lines backfill source for 2016-2020 (SBR like NFL? Kaggle? nba_api?).
3. Player-game-stats backfill source (nba_api preferred?).
4. Where to persist per-game feature values: new `nba.feature_values` table vs
   `game_predictions` JSON columns (NFL pattern).
5. Keep `nba.cumulative_game_stats` as the raw per-game layer, or fold it into
   `rolling_game_stats` and drop the table entirely?
