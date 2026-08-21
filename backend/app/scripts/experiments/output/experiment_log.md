# MLB ATS Sign-Aware Experiment — Running Results Log
# alpha=1.0 == current production RMSE objective (baseline/control)
# Other alphas blend winner-error into the margin objective.
# Capture from background sweep session (2026-08-20, test years 2021-2026).

## ALPHA = 1.0 (BASELINE — production objective)
| year | MAE   | ML%  | ATS%  | n_test | iter |
|------|-------|------|-------|--------|------|
| 2021 | 3.359 | 58.19| 58.75 | 1069   | 133  |
| 2022 | 3.332 | 58.80| 55.92 | 2466   | 105  |
| 2023 | 3.479 | 54.31| 56.38 | 2469   | 67   |
| 2024 | 3.417 | 56.56| 57.49 | 2470   | 64   |
| 2025 | 3.495 | 55.11| 61.45 | 2477   | 58   |
| 2026 | 3.531 | 54.37| 58.15 | 1909   | 75   |

## ALPHA = 0.75
| year | MAE   | ML%  | ATS%  | n_test | iter |
|------|-------|------|-------|--------|------|
| 2021 | 3.392 | 58.00| 57.34 | 1069   | 149  |
| 2022 | 3.347 | 57.70| 55.47 | 2466   | 146  |
| 2023 | 3.470 | 56.34| 55.89 | 2469   | 99   |
| 2024 | 3.417 | 55.51| 57.45 | 2470   | 114  |
| 2025 | 3.501 | 54.42| 61.61 | 2477   | 79   |
| 2026 | 3.545 | 53.80| 58.46 | 1909   | 82   |

## ALPHA = 0.5
| year | MAE   | ML%  | ATS%  | n_test | iter |
|------|-------|------|-------|--------|------|
| 2021 | 3.406 | 56.97| 57.34 | 1069   | 247  |
| 2022 | 3.372 | 57.83| 55.64 | 2466   | 253  |
| 2023 | 3.468 | 56.87| 56.34 | 2469   | 142  |
| 2024 | 3.435 | 56.52| 57.25 | 2470   | 189  |
| 2025 | 3.500 | 54.38| 61.28 | 2477   | 202  |
| 2026 | 3.554 | 54.85| 58.67 | 1909   | 221  |

## ALPHA = 0.25
| year | MAE   | ML%  | ATS%  | n_test | iter |
|------|-------|------|-------|--------|------|
| 2021 | 3.445 | 57.06| 56.59 | 1069   | 378  |
| 2022 | 3.405 | 56.89| 55.43 | 2466   | 353  |
| 2023 | 3.485 | 55.97| 56.87 | 2469   | 278  |
| 2024 | 3.461 | 55.30| 56.96 | 2470   | 306  |
| 2025 | 3.514 | 54.22| 61.20 | 2477   | 492  |
| 2026 | 3.564 | 54.11| 58.62 | 1909   | 517  |

## ALPHA = 0.0 (pure winner classification)
| year | MAE   | ML%  | ATS%  | n_test | iter |
|------|-------|------|-------|--------|------|
| 2021 | 3.491 | 55.47| 56.41 | 1069   | 359  |
| 2022 | 3.432 | 55.64| 55.60 | 2466   | 327  |
| 2023 | 3.540 | 54.84| 56.01 | 2469   | 299  |
| 2024 | 3.503 | 55.47| 57.00 | 2470   | 127  |
| 2025 | 3.567 | 55.35| 60.56 | 2477   | 124  |
| 2026 | 3.586 | 53.59| 58.62 | 1909   | 210  |

---
## AGGREGATED AVERAGES (unweighted across 2021-2026)
| alpha | avg ML% | avg ATS% | avg MAE |
|-------|---------|----------|---------|
| 1.00  | 56.22   | 58.02    | 3.435   |
| 0.75  | 55.96   | 57.70    | 3.445   |
| 0.50  | 56.24   | 57.75    | 3.456   |
| 0.25  | 55.59   | 57.61    | 3.479   |
| 0.00  | 55.06   | 57.37    | 3.520   |

---

# TWO-MODEL FUSION EXPERIMENT (2026-08-20) — test years 2021-2026, train-from 2016

Strategies (all on same held-out test years):
- BASE : regressor-only, ATS = sign(pred_margin + spread)   <- current production decision
- F1   : classifier picks winner side; regressor margin only decides cover on that side
- F2   : shrink regressor margin toward 0 on classifier disagreement (heuristic)
- F3   : near-push spreads (|margin+spread|<=1.5) defer to classifier cover signal

| year | n    | BASE%  | F1%    | F2%    | F3%    | F1-BASE |
|------|------|--------|--------|--------|--------|---------|
| 2021 | 1069 | 58.19  | 64.55  | 58.28  | 58.75  | +6.36   |
| 2022 | 2466 | 55.96  | 62.33  | 55.92  | 58.19  | +6.37   |
| 2023 | 2469 | 55.93  | 63.18  | 55.93  | 57.63  | +7.25   |
| 2024 | 2470 | 57.45  | 63.40  | 57.29  | 55.71  | +5.95   |
| 2025 | 2477 | 60.84  | 65.04  | 60.84  | 55.43  | +4.20   |
| 2026 | 1909 | 58.04  | 62.23  | 58.04  | 53.75  | +4.19   |
| AVG  |      | 57.74  | 63.46  | 57.72  | 56.58  | +5.72   |

VERDICT: F1 (classifier decides winner, regressor decides cover) beats baseline
in ALL 6 years, avg +5.7pt ATS. F2 (shrink heuristic) = no-op. F3 (near-push
defer) HURTS — deferring to raw classifier cover signal on close games is worse.
