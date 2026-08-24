# MLB Boxscore Audit — 2016 to 2026 (seasons 11–21)
Audit date: 2026-08-20 · DB: mlb schema · FINAL games with a recorded home/away score (25,006 games)

Method:
- **Hitting** = `mlb.batting_game_stats.runs` summed per team_side vs the team's recorded score.
- **Pitching** = `mlb.pitcher_game_stats.runs_allowed` summed per team vs the OPPONENT's score
  (a team's pitching staff allows the opponent's runs).
- `NO_BOX` = the game has no boxscore rows at all (data missing), not a mismatch.

---

## HITTING (batting_game_stats.runs vs game score)

| Year | Games | OK | NO_BOX | Real mismatch |
|------|-------|----|--------|---------------|
| 2016 | 2463 | 2278 | 185 | 0 |
| 2017 | 2468 | 2268 | 200 | 0 |
| 2018 | 2464 | 2239 | 225 | 0 |
| 2019 | 2466 | 2268 | 198 | 0 |
| 2020 |  883 |  883 |   0 | 0 |
| 2021 | 2465 | 2465 |   0 | 0 |
| 2022 | 2467 | 2467 |   0 | 0 |
| 2023 | 2471 | 2471 |   0 | 0 |
| 2024 | 2472 | 2472 |   0 | 0 |
| 2025 | 2477 | 2477 |   0 | 0 |
| 2026 | 1910 | 1910 |   0 | 0 |
| **TOT** | **25006** | **24198** | **808** | **0** |

**Hitting verdict:** Every game that HAS boxscore data reconciles **exactly** to the game total.
The only hitting-side issue is **808 games in 2016–2019 that have NO boxscore data at all**
(~185–225 games per season, likely never ingested, not a mismatch).
2020–2026 hitting is **100% perfect**.

---

## PITCHING (sum pitcher_game_stats.runs_allowed vs opponent score)

| Year | Games | OK | NO_BOX | Real mismatch |
|------|-------|----|--------|---------------|
| 2016 | 2463 | 2037 | 191 | 235 |
| 2017 | 2468 | 2006 | 209 | 253 |
| 2018 | 2464 | 1978 | 236 | 250 |
| 2019 | 2466 | 1978 | 205 | 283 |
| 2020 |  883 |  813 |   0 |  70 |
| 2021 | 2465 | 2239 |   0 | 226 |
| 2022 | 2467 | 2285 |   0 | 182 |
| 2023 | 2471 | 2313 |   0 | 158 |
| 2024 | 2472 | 2316 |   1 | 155 |
| 2025 | 2477 | 2308 |   0 | 169 |
| 2026 | 1910 | 1715 |  91 | 104 |
| **TOT** | **25006** | **21988** | **933** | **2085** |

**Pitching verdict:** Pitching does **NOT** reconcile to game totals. ~2,085 games (≈8.3%) have a
real pitching mismatch — present in **every** year, including all of 2020–2026 (70–283 games/yr).

---

## Root cause of the pitching mismatch (confirmed)

Across **48,269 pitching game-sides** (sid 11–21, with data):
- **46,115 (95.5%)** reconcile to opponent score.
- **1,701 mismatched sides (79% of all pitching mismatches)** have the signature
  `sum(runs_allowed) == sum(er) < opponent_score` → **unearned runs are missing entirely.**
- 453 (21%) are other gaps (allowed ≠ er, some missing pitcher lines).

Across all 206,146 pitcher rows: **94% have `runs_allowed == er` (exactly)**, and zero rows have
`runs_allowed < er`. A correct boxscore always has RA ≥ ER (RA = ER + unearned). Our ingested
`runs_allowed` is effectively mirroring earned runs, so whenever a game has unearned runs
(errors / passed balls / wild pitches that score), the runs_allowed sum comes up short of the
opponent's game total.

**Example:** 2024-03-30 BAL 13–4 LAA (game 42244):
- BAL pitchers: 1+1+0+0 = 2 runs_allowed (all earned) — but LAA scored 4 → 2 unearned dropped.
- LAA pitchers: 5+3+1+0 = 9 runs_allowed (all earned) — but BAL scored 13 → 4 unearned dropped.

**Mismatch magnitude:** mostly small (off-by-1: 756 sides, off-by-2: 509, off-by-3: 485), up to
off-by-9. Consistent with unearned-run counts.

---

## Summary for Rich

1. **Hitting: boxscores are solid.** All 24,198 games with batting data match totals exactly.
   The 808 games with no boxscore (2016–2019 only) are missing data, not bad data.

2. **Pitching: needs a fix.** The pitching boxscore under-counts runs vs game totals in every
   season because **unearned runs are not carried** into `pitcher_game_stats.runs_allowed`
   (it's tracking earned runs only). ~2,085 games (~8.3%) are affected. This is a data
   ingestion/completeness gap — the game totals themselves are correct.

3. **Recommended next step:** re-fetch pitching boxscores (MLB StatsAPI) for the affected games
   to populate a true `runs_allowed` (RA = ER + unearned), then re-run this audit.
