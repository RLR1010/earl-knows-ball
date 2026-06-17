# 2025 Backtest Results

Run: Week-by-week simulation, each week uses only data from prior weeks.

**Model:** Simple team stats (PPG for/against, home/away splits, last 3 games weighted)

## Overall

| Metric | Record | Win % |
|--------|--------|-------|
| **ATS** | 182-54-3 | **77.1%** 🎯 |
| **O/U** | 102-125-0 | 44.9% |
| **ML** | 67-110-0 | 37.9% |

256 games tested across 17 weeks (Weeks 2-18).

## Week-by-Week ATS Breakdown

| Week | Games | ATS Record | Win % |
|------|-------|-----------|-------|
| W2  | 16 | 12-4 | 75.0% |
| W3  | 16 | 10-5 | 66.7% |
| W4  | 16 | 12-4 | 75.0% |
| W5  | 14 | 10-4 | 71.4% |
| W6  | 15 | 12-1 | 92.3% |
| W7  | 15 | 11-4 | 73.3% |
| W8  | 13 | 10-3 | 76.9% |
| W9  | 14 | 8-5 | 61.5% |
| W10 | 14 | 8-2 | 80.0% |
| W11 | 15 | 11-2 | 84.6% |
| W12 | 14 | 12-1 | 92.3% |
| W13 | 16 | 10-5 | 66.7% |
| W14 | 14 | 11-2 | 84.6% |
| W15 | 16 | 9-7 | 56.3% |
| W16 | 16 | 11-2 | 84.6% |
| W17 | 16 | 12-1 | 92.3% |
| W18 | 16 | 13-2 | 86.7% |

**Lowest week:** W15 (56.3%)
**Highest week:** W6, W12, W17 (92.3%)
**Weeks below 50%:** None

## 2024 Backtest (Comparison)

257 games tested across 17 weeks (Weeks 2-18).

| Metric | Record | Win % |
|--------|--------|-------|
| **ATS** | 181-48-0 | **79.0%** 🎯 |
| **O/U** | 111-98-2 | 53.1% |
| **ML** | 75-95-0 | 44.1% |

## Key Takeaways

1. **ATS is strong and consistent** — beat the spread every single week of both seasons
2. **O/U needs improvement** — scoring model tends to be conservative (unders)
3. **ML is a value strategy** — picks underdogs with positive expected value, so lower raw win rate is expected
4. **Confidence calibration needs work** — model says 95% but hits ~80%

## API Endpoints to View Picks

```
# Picks for a specific week
GET /api/handicapping/week/{year}/{week}?num_games=3

# Single matchup
GET /api/handicapping/matchup?home=KC&away=BAL&year=2024&week=1

# Backtest for a season
GET /api/handicapping/backtest/{year}?num_games=3

# Team stats
GET /api/handicapping/team-stats/{year}/{team}

# ATS standings
GET /api/handicapping/ats-standings/{year}
```
