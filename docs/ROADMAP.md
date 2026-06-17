# EarlKnowsBall - Roadmap to Expert

## Goal
Make Earl an absolute expert at:
- **Handicapping NFL games** (spreads, O/U, moneylines, picks)
- **Evaluating fantasy picks/lineups** (season-long)
- **Recommending DFS lineups** (DraftKings, FanDuel)

## What We Have (Foundation)
| Asset | Detail |
|---|---|
| 101k+ NFL articles | SB Nation (73k) + Last Word on Sports (29k) + RSS (42) |
| 93,444 player weekly stats | 2005-2025, includes PPR/half/standard **and DK scoring** |
| 5,220 games | 2005-2026 (all 272 2026 games loaded) |
| 4,218 players | Linked via sleeper_id, espn_id, nflverse_id |
| 3,518 injury records | Historical injury data |
| 963 transactions | Trades, signings, cuts |
| 22 season recaps | 2005-2025, embedded in Cognee-NFL |
| Player profiles | BIO, career stats, draft info, recent seasons |
| Cross-encoder reranker | `ms-marco-MiniLM-L-6-v2` for context ranking |
| Cognee-NFL semantic search | Dedicated instance on :8002, ~254 MB |

## Data Gaps (Need Filling)
| Gap | Status | Priority |
|---|---|---|
| Betting lines | **5,012 rows loaded** (2005-2026, all spread/O/U/moneyline) | ✅ Done |
| DFS salaries (DK/FD) | **Pipeline built + test data loaded** (30 sample salaries) | 🔴 Needs live data |
| Expert consensus rankings | Nothing exists | 🟡 High |
| Game handicapping engine | **Built — see API below** | ✅ Done |
| Current-week injury updates | 3.5k historical rows, no live feed | 🟡 High |
| Draft data | Schema exists in `players` table, no source wired | 🟢 Nice-to-have |

---

## Tier 1: Close the Data Gaps

### 1. Betting Lines Pipeline
- **Schema:** `betting_lines` table (spread, O/U, moneyline, implied probability)
- **Source options:** The Odds API (free tier), nflverse CSVs, ESPN API
- **Goals:** Historical lines (2005+) for analysis + current week for predictions
- **Endpoints:** `POST /api/ingest/betting-lines/historical`, `POST /api/ingest/betting-lines/current`
- **Cron:** Weekly refresh (Tuesday after lines settle)

### 2. DFS Salary Pipeline
- **What:** DraftKings and FanDuel player salaries + position eligibility
- **Source:** Both sites expose JSON on contest pages
- **Schema:** `dfs_salaries` table (platform, player_id, salary, position, week, contest_type)
- **Endpoints:** `POST /api/ingest/dfs/salaries`
- **Cron:** Weekly (Tuesday-Wednesday when salaries drop)

### 3. Expert Consensus Rankings (ECR)
- **Approach:** Aggregate rankings from our article corpus + Sleeper API ADP
- **Schema:** `fantasy_rankings` table (player_id, week, rank, source, position_rank, tier)
- **Endpoints:** `POST /api/ingest/fantasy-rankings`

### 4. Live Injury Feed
- **Source:** Sleeper API or ESPN's injury endpoint
- **Schema:** Already have `injuries` table, just need live polling
- **Cron:** Daily during season (checks for new practice report data)

---

## Tier 1 Progress

### ✅ Betting Lines Pipeline (May 26, 2026)

### ✅ DFS Salary Scraper (May 26, 2026)
DraftKings + FanDuel salary scraper. Tested and verified with sample data.

### ✅ Game Handicapping Engine (May 26, 2026)
Full handicapping system that produces pick cards for any NFL week.
- **Module:** `backend/app/handicapping/engine.py`
- **Router:** `backend/app/routers/handicap.py`
- **Endpoints:**
  - `GET /api/handicapping/week/{year}/{week}?num_games=5` — Week picks
  - `GET /api/handicapping/matchup?home=KC&away=BAL&year=2024&week=1` — Single matchup
  - `GET /api/handicapping/team-stats/{year}/{team}` — Team strength metrics
  - `GET /api/handicapping/ats-standings/{year}` — Full ATS standings
- **Data sources:** Game scores + betting lines (5,012 rows) + nflverse schedule
- **Predictions:** Spread pick + confidence, O/U pick + confidence, Moneyline value pick
- **Features:** Team PPG for/against, home/away splits, ATS/ML/Over records, recent form, predicted score
- **Future:** No game data yet — awaits 2026 season. Historical seasons fully functional.
- **Module:** `backend/app/ingestion/dfs_salaries.py`
- **Model:** `DfsSalary` in `backend/app/models/dfs_salary.py`
- **Endpoints:** `POST /api/ingest/dfs/draftkings`, `fanduel`, `all`
- **Sample data:** 30 salaries (18 DK + 12 FD) loaded and verified with correct player/game links
- **DraftKings API:** Uses lobby → draftables pattern (standard industry approach)
- **FanDuel API:** Uses mobile API endpoint (less bot protection)
- **Off-season graceful:** Returns empty result with message; use `test_mode=True` for pipeline testing
- **Goes live:** Auto-activates when NFL contests appear (~August)
All 22 seasons (2005-2026) loaded from nflverse games.csv.
- **5,012 lines** across all 22 seasons
- Every game has spread + over/under (2005-2026)
- Moneylines present from 2007+ (complete from 2010)
- **Module:** `backend/app/ingestion/betting_lines.py`
- **Endpoints:** `POST /api/ingest/betting-lines/historical` + `current`
- **Current season ready:** The Odds API integration wired (needs free API key)
- Handles historic team relocations (LA↔LAR, SD↔LAC, OAK↔LV, STL↔LAR)

---

## Tier 2: Build the Expert Brain

### 5. Game Handicapping Module
- **Inputs:** Spread + O/U + moneyline, team offense/defense stats, player matchups, relevant articles
- **Outputs:** Structured pick card (pick, confidence, reasoning, key stat)
- **Components:**
  - Team strength model (points for/against, DVOA proxy via stats)
  - Matchup analyzer (offensive vs defensive rankings)
  - Article sentiment analysis (injury impact, narrative trends)
  - Historical ATS performance (how teams cover historically)

### 6. Fantasy Projection Engine
- **Inputs:** Player weekly stats (last N games), matchup data, injury status, article context
- **Outputs:** Projected fantasy points (floor, median, ceiling) for each scoring format
- **Approach:**
  - Rolling weighted averages (recent games matter more)
  - Defense-vs-position adjustments
  - Ceiling/floor from variance history
  - Touchdown regression indicators

### 7. DFS Lineup Optimizer
- **Inputs:** Projections + DraftKings/FanDuel salaries + contest rules
- **Outputs:** Optimal lineups for GPP, cash games, single-entry
- **Features:**
  - Salary cap optimization (knapsack solver)
  - Game stacking (QB+WR, game environments)
  - Ownership awareness (differentiate GPP vs cash)
  - Multi-entry lineup diversity (correlation optimization)
  - Leverage plays (low ownership, high ceiling)

---

## Tier 3: Make It Active

### 8. Daily Cron Jobs
| When | What |
|---|---|
| 🗓️ Tuesday | Refresh betting lines (settle after Monday night) |
| 🗓️ Wednesday | Refresh DFS salaries, update injury reports |
| 🗓️ Thursday | Pre-SNF line check, update projections |
| 🗓️ Sunday | Early game injury updates |
| 🗓️ Monday | Recap, update projections with game results |

### 9. Earl's Chat Superpowers
- "Who should I start at flex?" → ECR + projections + matchup analysis
- "Pick this week's games ATS" → handicapping module
- "Build me a DraftKings lineup" → DFS optimizer
- All backed by 101k+ articles as reasoning context

---

## Priority Order
| Step | What | Est. Effort | Impact |
|---|---|---|---|
| 1 | Betting lines pipeline (current starts now) | 1 session | 🔴 Essential for handicapping |
| 2 | DFS salary scraper | 1 session | 🔴 Essential for DFS |
| 3 | Handicapping engine | 1-2 sessions | 🟡 High |
| 4 | Fantasy projection engine | 1-2 sessions | 🟡 High |
| 5 | Live injury feed | 0.5 session | 🟡 High |
| 6 | DFS lineup optimizer | 1 session | 🟡 High |
| 7 | ECR module | 0.5 session | 🟢 Medium |
| 8 | Daily cron + chat integration | 1 session | 🟢 Medium |
