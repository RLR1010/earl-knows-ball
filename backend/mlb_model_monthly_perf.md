# MLB Model Performance by Month — ATS & OU (CORRECTED)
Audit date: 2026-08-20 · Source: `mlb.game_predictions` (seasons 2021–2026, sid 16–21)
Method: flat $100/bet per pick, real-odds picks only; Win/Loss counted excluding pushes.
ROI = profit / (n × $100).

---

## 🔴 IMPORTANT DATA FIX — 1,396 phantom wins in season 2021

The ORIGINAL by-month numbers were corrupted: **season 2021 (sid 16) has 1,396 of its 2,465
backtest picks with `ou_odds = 0` AND `ats_odds = 0`, all scored `result='Win'`, `ou_ev=NULL`**
(no closing odds recorded when the 2021 backtest was run). Those rows inflated the W-L wins but
contributed **zero profit/EV**. All other seasons (2022–2026) are clean (0–2 such rows).

Because the 2021 regular season ran April–July, the phantom wins cluster there:
Apr 380 · May 422 · Jun 398 · Jul 186 · Aug 7 · Sep 9.

This caused two illusions:
- **June "won 59.1% yet lost money"** → 398 of June's OU wins were zero-odds phantoms that
  paid nothing. June's real win rate is ~50.5%.
- **"Aug/Sep are uniquely terrible"** → Apr–Jul win rates were flattered by phantoms; Aug/Sep had
  almost none, so they showed honest ~49%, looking worse only by comparison. They are NOT uniquely bad.

**Recommendation:** re-run or repair the 2021 backtest with real closing odds, or exclude
`odds=0` / `ou_ev IS NULL` rows from all model-performance metrics going forward.

---

## CLEAN monthly performance (real-odds picks only, 13,347 picks for ATS / 13,347 for OU)

### ATS (run line)
| Month | n | W–L | Win% | Profit | ROI% |
|-------|---|-----|------|--------|------|
| Mar | 204 | 121–83 | 59.3% | –241 | –1.18% |
| Apr | 1,900 | 1,067–833 | 56.2% | –11,888 | –6.26% |
| May | 2,068 | 1,237–831 | 59.8% | –898 | –0.43% |
| Jun | 1,983 | 1,134–849 | 57.2% | –9,413 | –4.75% |
| Jul | 2,052 | 1,223–829 | 59.6% | –1,154 | –0.56% |
| Aug | 2,333 | 1,325–1,008 | 56.8% | –8,477 | –3.63% |
| Sep | 1,965 | 1,122–843 | 57.1% | –7,001 | –3.56% |
| Oct | 346 | 212–134 | 61.3% | **+277** | **+0.80%** |
| Nov | 9 | 4–5 | 44.4% | –153 | –16.97% |
| **ALL** | **12,860** | **7,445–5,415** | **57.9%** | **–38,948** | **–3.03%** |

### OU (over/under)
| Month | n | W–L | Win% | Profit | ROI% |
|-------|---|-----|------|--------|------|
| Mar | 204 | 100–96 | 51.0% | –519 | –2.54% |
| Apr | 1,900 | 899–927 | 49.2% | –11,487 | –6.05% |
| May | 2,068 | 990–1,012 | 49.5% | –11,762 | –5.69% |
| Jun | 1,983 | 964–944 | 50.5% | –7,509 | –3.79% |
| Jul | 2,052 | 996–983 | 50.3% | –8,439 | –4.11% |
| Aug | 2,333 | 1,098–1,133 | 49.2% | –14,415 | –6.18% |
| Sep | 1,965 | 930–957 | 49.3% | –11,803 | –6.01% |
| Oct | 346 | 166–164 | 50.3% | –1,483 | –4.29% |
| Nov | 9 | 2–7 | 22.2% | –519 | –57.63% |
| **ALL** | **12,860** | **6,145–6,223** | **49.7%** | **–67,936** | **–5.28%** |

---

## Real findings

1. **The "Aug/Sep collapse" was mostly an artifact.** After removing season-2021 phantom wins,
   Aug and Sep are not uniquely bad — **every** month is bad.
2. **OU has no edge at all:** 49.7% overall — *below* the ~55% juice break-even, negative in every
   month (−3.8% to −6.2% ROI). Worst are Apr/Jul/Aug/Sep (~−6%); "best" is June (−3.8%), still a loss.
3. **ATS has a genuine edge but loses on juice:** 57.9% win rate vs ~55% break-even, so directionally
   correct, but it bets heavy run-line favorites (−117 to −149 avg), and the juice turns it to −3.0% ROI.
   April is the worst ATS month (−6.26%); only October is positive (+0.80%). May/Jul near break-even.
4. **Data-quality bug to fix:** season-2021 backtest committed 1,396 picks with `odds=0` + `result='Win'`.
   Any performance metric must filter those, or the 2021 backtest must be rebuilt with real odds.
