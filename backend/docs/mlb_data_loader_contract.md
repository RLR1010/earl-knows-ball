# MLB Data Loader — Rolling & Cumulative Stats Contract (AUTHORITATIVE)

**Status:** 🔴 Read this before touching ANY stat-loading or rolling-stats code in MLB.
**Last verified:** 2026-08-17 (empirical end-to-end check, inline-historical-data, both SCHEDULED and FINAL paths)

---

## 2026-08-17 — pgs.ip historical repair + venue-bug fix (read before continuing)

Two things landed today that invalidate older notes:

1. **`mlb.pitcher_game_stats.ip` historical corruption fixed via MLB StatsAPI re-fetch.**
   Seasons 7-14 (2012-2019) `pgs.ip` was broadly corrupted (~25% of games inflated ~3x). All
   19,724 games re-fetched from `statsapi.mlb.com` and `ip/er/runs_allowed/h/hr/k/bb/strikes/`
   `batters_faced` overwritten with authoritative decimal innings. Derived tables fully rebuilt
   (cumulative 66,308 / bullpen 65,532 / team_rolling 66,308 / pitcher_rolling 66,014).
   Run script: `backend/app/scripts/repair_pgs_ip_from_mlbapi.py`.
   **`ip` is DECIMAL innings** (6.333 = 6 1/3). Convert IP->outs with `ROUND(ip*3)`, NEVER
   baseball-notation splits. `populate_bullpen_stats.py` + `populate_rolling.py` were fixed to
   use `ROUND(pgs.ip*3)` on 2026-08-17.
   Known remaining edge: ~237 games where the MLB API bundles a doubleheader sibling starter as
   a "reliever" with >=5 IP (is_starter=FALSE). Pre-existing source quirk; tracked as follow-up.

2. **Venue-label bug in the loader's `trs_*` LATERALs fixed (see Part 1 exceptions).**
   `team_rolling_stats.team_side` is a venue label, NOT a series. The `team_side` filter was
   removed so workload/form reads are venue-agnostic. Verified game 48961 `a_bullpen_ip_l5` =
   44 outs = **14.67 IP** through `build_features` (the exact model/pick-card path).

Related: the "separate rows per team_side" text that used to be in Part 1 was WRONG and has been
corrected. `distinct(game_id, team_id) == total_rows` (66,308 == 66,308) — one row per team-game.


---

## The contract (memorize this)

> **Every row in the `*_rolling_stats` and `*_cumulative_game_stats` tables INCLUDES that game's
> own results (`ROWS BETWEEN ... AND CURRENT ROW`). To load stats for a target game — whether
> that target is SCHEDULED (live inference) or FINAL (backtest) — the data loader reads back the
> **most recent FINAL row strictly before the target game** and uses that row's values verbatim.
> That row's stats are "everything that was known going into the target game."

In one sentence: **tables are built INCLUDING the current game; the loader reads the PREVIOUS
FINAL row.** Both halves are mandatory. Breaking either half reintroduces an off-by-one.

### Why this is correct for both cases

- **SCHEDULED game (live API):** the target hasn't happened. The previous FINAL row contains every
  prior result, *including* the target team/pitcher's most recent outing. Exactly what you want to
  feed inference.
- **FINAL game (backtest/training):** the target DID happen, but we must not let its own result leak
  into the features (lookahead). The loader reads the previous FINAL row **strictly before** the
  target's timestamp, so the target's own line is excluded. Features = "stats going into that game."

The `- INTERVAL '30 minutes'` bound excludes the target itself even when the target is FINAL, and
correctly handles same-day games (a game that started earlier that day is a legitimate "previous" row).

---

## Part 1 — Table builders must use CURRENT ROW

Every cumulative/rolling stat column on a row must reflect **through and including that game**.

### `populate_rolling.py` (team + pitcher `mlb.team_rolling_stats`, `mlb.pitcher_rolling_stats`)

| Table | Window frames | Status |
|-------|--------------|--------|
| Team (`w_full`, `w5`, `w10`, `w15`) | `ROWS BETWEEN ... AND CURRENT ROW` | ✅ correct |
| Pitcher (`w`, `w5`, `w10`, `w15`, `w20`) | `ROWS BETWEEN ... AND CURRENT ROW` | ✅ **fixed 2026-08-17** |

**⚠️ Never change these back to `... AND 1 PRECEDING`.** Doing so makes each row exclude its own
game (off-by-one), and because the loader then reads the *previous* FINAL row, the most recent start
gets **double-subtracted** — e.g. Blake Snell showed ERA 12.0 / 94-days-rest instead of
ERA 5.00 / 6-days-rest on game 48961. This was the actual production bug fixed 2026-08-17.

**Deliberate exceptions (do NOT "fix" these):**
- **`team_side` in `team_rolling_stats` is a VENUE LABEL, NOT a separate series.** The table has
  exactly ONE row per (team, game); `team_side` records whether that game was at home or away for
  the team. There is NO home-series/away-series split. Do NOT filter `trs.team_side` as if it were
  a rolling series selector.
  **🔴 Bug fixed 2026-08-17:** the loader's `trs_h`/`trs_a` LATERALs previously had
  `AND trs.team_side = 'home'/'away'`. For a workload/form stat that must be venue-agnostic (e.g.
  `bullpen_ip_l5` = "how many innings the bullpen threw in the last 5 games"), this made the away
  team read its most recent game at the OPPOSITE venue as "not applicable" and skip back to its
  last AWAY game — returning stale data (LAD away showed 52 outs = last-5-away instead of 44 outs
  = 14.67 IP over last-5-actual). **Fix: removed the `team_side` filter from `trs_h`/`trs_a`** so
  they read the team's most recent Final row regardless of venue. Do NOT reintroduce it.
      - Correct venue-splits ARE made only where venue is semantically meaningful, using the
        *joined side from the games table*, not a `team_side` rolling filter — e.g. the
        `vph`/`vpa` (pitcher venue ERA) LATERALs and the `plato_*` batting LATERALs join
        `home_team_id`/`away_team_id` and filter `batting_game_stats.team_side`, which is correct.
- Any **W/L or per-game-delta** column that legitimately compares to the prior row may use a
  `1 PRECEDING` window (e.g. NFL `WINDOW w` delta expressions). Those are intentional and are NOT
  cumulative-state columns.

### `mlb.cumulative_game_stats`
Same convention — each row includes that game's results.

---

## Part 2 — The loader reads the previous FINAL row

`backend/app/handicapping/mlb/data_loader.py` — every stat read uses the same LATERAL pattern:

```sql
LEFT JOIN LATERAL (
    SELECT t.*
    FROM <scoped_table> t
    JOIN mlb.games gp ON gp.id = t.game_id
    WHERE t.<team_or_player> = <target>
      AND gp.status = 'FINAL'                  -- ALWAYS only FINAL rows
      AND gp.date   < g.date - INTERVAL '30 minutes'   -- strictly before target
    ORDER BY gp.date DESC, gp.id DESC
    LIMIT 1                                    -- most recent completed game
) alias ON TRUE
```

**⚠️ No `team_side` filter on `trs_*` reads.** `team_rolling_stats.team_side` is just each row's
venue label (one row per team-game) — filtering it turns workload/form reads venue-stale (see the
`bullpen_ip_l5` bug fixed 2026-08-17). The `trs_h`/`trs_a` LATERALs must match ONLY on `team_id` +

### Full inventory of loaded stat sources (all audited ✅ 2026-08-17)

| Feature group | LATERAL | Joins | Notes |
|--------------|---------|-------|-------|
| Team rest days | `h_last_game`/`a_last_game` | games | days since team's previous FINAL game |
| Cumulative game stats | `cgs_h`/`cgs_a` | cumulative_game_stats | previous FINAL row |
| Runs per game | `runfg_h`/`runfg_a` | games | prior FINAL games |
| Team rolling (win%/rf/...) | `trs_h`/`trs_a` | team_rolling_stats | **per team_side**, previous FINAL row |
| Prior-season blend | `pts_h`/`pts_a` | prior_team_stats | prior season (early-season fallback) |
| Pitcher game stats (starter ID) | `pgs_h`/`pgs_a` | pitcher_game_stats | most recent completed start by name |
| **Pitcher rolling (ERA/WHIP/K9...)** | `prs_h`/`prs_a` | pitcher_rolling_stats | previous FINAL row by player_id |
| Pitcher rest | `prs_h`/`prs_a` (recomputed) | — | `rest_days = g.date - prs.game_date` (see Part 3) |
| Pitcher venue ERA | `vph`/`vpa` | pitcher_rolling_stats | prior FINAL starts at that venue |
| Bullpen | `bg_h` | — | most recent FINAL game |
| Batting vs arm / OPS | various | batting_game_stats | prior through-date, leak-safe |

### Loader entry points / status handling

- `MLBDataLoader.load_games(...)`:
  - `status="FINAL"` → `WHERE g.status='FINAL'` (train/backtest set).
  - `include_upcoming=True` → includes SCHEDULED/PREGAME games (live set).
  - `game_ids=[...]` → loads specific games regardless of status.
  - In **every** case the inner LATERALs still filter `gp.status='FINAL'` on the *looked-back* rows —
    the status filter only ever applies to the OUTER target rows. This is what makes it leak-safe.

---

## Part 3 — Pitcher rest days are recomputed (NOT the stored column) ⚠️

`pitcher_rolling_stats.rest_days` stores the gap between a start and the pitcher's **prior** start
(e.g. Snell 8/12 row = 94 days since his 5/10 start). That is **wrong** as a "rest going into the
target game" value for a scheduled game.

**The loader recomputes it:** in the `prs_h` / `prs_a` LATERALs, `rest_days` is projected as
`EXTRACT(DAY FROM (g.date - prs.game_date))::int` — i.e. days from the pitcher's most recent
completed start to the **target** game date. For scheduled game 48961 that gives Snell **6** days
(wrong: stored 94).

**Implementation detail:** these two LATERALs must project pitcher columns **explicitly**, NOT
`prs.*`, because `prs.*` + `AS rest_days` yields an ambiguous duplicate `rest_days` column
(Postgres error). If you add a `pitcher_rolling_stats` column, add it to BOTH explicit projections.

---

## Part 4 — Rebuild + deploy checklist after ANY window/semantic change

```bash
cd backend
export XDG_RUNTIME_DIR=/run/user/$(id -u)
PYTHONPATH=$PWD ../venv/bin/python app/handicapping/mlb/populate_rolling.py --pitcher-only   # pitcher rolling (truncate+recompute, ~66k rows, ~26s)
PYTHONPATH=$PWD ../venv/bin/python app/handicapping/mlb/populate_rolling.py --team-only       # team rolling
systemctl --user restart earl-compute.service   # compute role + scheduler (8002)
systemctl --user restart earl-api.service       # API role (8001)
```

- `--pitcher-only` / `--team-only` truncate and fully recompute that table (off-by-one fixes need
  a full rebuild, not incremental).
- Always restart both services after a rebuild so the running code picks up new table values.

### Standard verification (do this every time)

Build one SCHEDULED game and one FINAL game through the loader and byte-compare every rolling/
cumulative feature against an independent query of the previous FINAL row. Expect **exact match
if and only if** there is no off-by-one. Known-good reference (2026-08-17):

| gid | type | team_h win% / rf | team_a win% / rf | pitcher_h ERA / pitcher_a ERA |
|-----|------|------------------|------------------|-------------------------------|
| 48961 | SCHEDULED | .39130 / 4.79130 | .59322 / 4.95763 | 3.8357 (Sugano) / 5.0 (Snell) |
| 48883 | FINAL | .59664 / 4.96639 | .40833 / 4.05 | 12.0 (Snell, prior-start-only...) / 3.4225 (Wacha) |

_Note: FINAL game 48883 correctly shows Snell ERA 12.0 — that's the value *as of before* that game
(his prior start 5/10). The 8/12 line is excluded for leak-safety. Once that game becomes a past
row, later games (e.g. 48961) read its row and correctly see ERA 5.00._

---

## Cross-sport note

- **NBA** `populate_team_rolling_stats.py`: all windows `CURRENT ROW`. ✅
- **NFL** `populate_team_rolling_stats.py`: all rolling windows `CURRENT ROW`. NFL `WINDOW w`
  (line ~274) is a per-game-delta expression using `1 PRECEDING` — intentional, NOT a cumulative
  stat. Audit confirmed no pitcher-style off-by-one in NBA or NFL. ✅

`cumulative_game_stats` for MLB is REG+POST+PLAYIN (no preseason), matching the NBA/NFL convention.
