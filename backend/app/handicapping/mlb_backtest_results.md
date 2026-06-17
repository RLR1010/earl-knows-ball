# MLB XGBoost Backtest Results — Full Feature Set (32 features)

Trained on 2011-Y, tested on Y+1. Rolling team stats + betting lines + situational features.

## Summary

| Year | Games | MAE (runs) | ATS% (RL) | O/U% | ML% |
|------|-------|-----------|-----------|------|-----|
| 2015 | 2,392 | 3.38 | 62.2% | 59.0% | 51.7% |
| 2016 | 2,404 | 3.40 | 60.9% | 59.8% | 54.8% |
| 2017 | 2,392 | 3.62 | 61.4% | 56.1% | 53.6% |
| 2018 | 2,380 | 3.47 | 60.9% | 58.1% | 55.8% |
| 2019 | 2,392 | 3.58 | 59.3% | 58.8% | 56.0% |
| 2021 | 2,356 | 3.37 | 55.5% | 50.6% | 59.4% |
| 2022 | 2,382 | 3.34 | 57.1% | 52.0% | 58.6% |
| 2023 | 2,391 | 3.47 | 56.2% | 51.1% | 55.9% |
| 2024 | 2,395 | 3.44 | 57.9% | 51.7% | 56.7% |
| **AVG** | **21,484** | **3.45** | **59.0%** | **55.2%** | **55.8%** |

## Key Observations

1. **ATS (Run Line) consistently above 55%** — 59.0% average across all years. This is strong performance for a run line model. The run line in MLB is typically -1.5/+1.5.
2. **ML (Moneyline) averages 55.8%** — solid, above the 52.4% break-even for -110 juice. Best in recent years (2021-2024).
3. **O/U (Over/Under) averages 55.2%** — also above break-even, though the simple predicted total formula could be improved.
4. **MAE of 3.45 runs** with ~54% of predictions within 3 runs and ~76% within 5 runs.
5. **Performance dip in 2021-2023** on ATS — likely because sportsbook data (2021+) has more granular lines than SBR aggregate (2011-2019). The model was partly learning SBR line patterns.

## Top Features (Consistently)

1. `h_implied` — Home team moneyline implied probability (avg importance ~0.06)
2. `a_implied` — Away team moneyline implied probability (~0.05)
3. `is_home_fav` — Binary: is the home team the ML favorite? (~0.05)
4. `a_ra20` — Away team runs allowed, last 20 games (rolling defense)
5. `h_ra20` — Home team runs allowed, last 20 games
6. `winpct_diff` — Home win% minus away win%

Implied probabilities from betting lines are the single strongest signal, followed by defensive rolling stats and win percentage differential.

## Feature Sets

Available feature sets for experimentation:
- **simple** (6 feats): 5-game rolling + rest_diff + is_home_fav
- **rolling** (19 feats): 5/10/20 game rolling + home/away splits
- **full** (32 feats): All rolling + betting lines + situational + travel
- **ml_only** (12 feats): Rolling + implied probabilities + win pct diff

Example: `docker exec earl-knows-football-api-1 python -m app.handicapping.mlb_backtest --features ml_only --test-year 2023`

## Next Experiments

1. Add starting pitcher data (ERA, K/9, BB/9 from previous season)
2. Park factors (stadium-specific run adjustments)
3. Bullpen metrics (rest days for key relievers)
4. Weather effects (wind direction, temperature)
5. Day/night splits
6. Interleague play adjustment
7. Save all predictions to DB for analysis

## Script Location

`backend/app/handicapping/mlb_backtest.py`

Run:
```bash
# Single year
docker exec earl-knows-football-api-1 python -m app.handicapping.mlb_backtest --test-year 2024 --features full

# All years comparison
docker exec earl-knows-football-api-1 python -m app.handicapping.mlb_backtest --mode all --features full
```
