"""
Plain-assert unit tests for the parlay engine (matches repo test style — no pytest).
Run:  cd backend && venv/bin/python test_parlay.py
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))
import math

from app.handicapping.parlay import (
    american_to_decimal, decimal_to_american,
    combine, Leg, LEG_ML, LEG_SPREAD, LEG_TOTAL,
)

passed = 0

def check(name, cond, extra=""):
    global passed
    assert cond, f"FAIL: {name} {extra}"
    passed += 1
    print(f"  ok  {name}")


# --- odds conversion ---
check("+120->2.20", abs(american_to_decimal(120) - 2.20) < 1e-9)
check("-130->1.7692", abs(american_to_decimal(-130) - 1.7692307692307692) < 1e-9)
check("+100->2.00", abs(american_to_decimal(100) - 2.00) < 1e-9)
check("None->1.0", american_to_decimal(None) == 1.0)
check("dec 2.20->+120", decimal_to_american(2.20) == 120)
check("dec 1.769->-130", decimal_to_american(1.7692307) == -130)

# --- single leg roundtrip ---
l = Leg(
    game_id=1, sport="mlb", kind=LEG_ML, label="LAD ML (-174)",
    pick="home", side="LAD", prob=0.61, odds=-174, ev=33.02,
)
r = combine([l])
check("single leg fair_prob == prob", abs(r.fair_probability - 0.61) < 1e-6)
check("single leg book_decimal == dec", abs(r.book_decimal - american_to_decimal(-174)) < 1e-6)
check("single leg ev_pct ~= ev_dollars", abs(r.ev_pct - r.ev_dollars) < 1e-9)

# --- independent two-leg math (hand-computed) ---
# p1=0.55 @ +100 (2.0), p2=0.50 @ +100 (2.0)
a = Leg(game_id=1, sport="mlb", kind=LEG_ML, label="A ML", pick="home", side="A", prob=0.55, odds=100)
b = Leg(game_id=2, sport="mlb", kind=LEG_ML, label="B ML", pick="home", side="B", prob=0.50, odds=100)
r2 = combine([a, b])
exp_fair = 0.55 * 0.50  # 0.275
exp_fair_dec = 1.0 / exp_fair  # ~3.636
exp_book = 2.0 * 2.0  # 4.0
exp_ev = (exp_book / exp_fair_dec - 1.0) * 100.0  # (4.0/3.636-1)*100 = +10%
check("two-leg fair_prob", abs(r2.fair_probability - exp_fair) < 1e-6)
check("two-leg book_decimal", abs(r2.book_decimal - exp_book) < 1e-6)
check("two-leg ev_pct ~ +10%", abs(r2.ev_pct - 10.0) < 1e-6)
check("two-leg vig_drag", abs(r2.combined_implied - 0.25) < 1e-9)  # 1/4.0
check("two-leg no blocks", r2.correlation_blocks == [])
check("two-leg no warnings (different games)", r2.correlation_warnings == [])

# --- negative-EV parlay from positive-EV singles (the receipt moment) ---
# Two legs each 52% at -110 (dec 1.909). Single-leg EV: 0.52*1.909-1 = -0.7% (slight neg at -110).
# But at +115 (dec 2.15): single EV = .52*2.15-1 = +11.8%. Two-leg book = 2.15^2 = 4.6225,
# fair dec = 1/(.52^2) = 3.698. ev = (4.6225/3.698-1)*100 = +25%.
# Use a genuinely negative two-leg to show stacking loss is surfaced only mathematically:
m1 = Leg(game_id=1, sport="nfl", kind=LEG_ML, label="X", pick="away", side="X", prob=0.52, odds=-110)
m2 = Leg(game_id=2, sport="nfl", kind=LEG_ML, label="Y", pick="away", side="Y", prob=0.52, odds=-110)
rm = combine([m1, m2])
# book dec = 1.909^2 = 3.644 ; fair dec = 1/(.2704) = 3.698 => slightly negative EV
check("two-leg neg EV when book vig compounds",
      rm.ev_pct < 0, f"got {rm.ev_pct}")
check("single-leg EV at -110/52% is close to -0.7%",
      abs((0.52*american_to_decimal(-110) - 1) * 100 + 0.72) < 0.1)

# --- same-game correlation: ML + spread same side => blocked ---
c1 = Leg(game_id=9, sport="mlb", kind=LEG_ML, label="LAD ML", pick="home", side="LAD", prob=0.61, odds=-174)
c2 = Leg(game_id=9, sport="mlb", kind=LEG_SPREAD, label="LAD -1.5", pick="LAD -1.5", side="LAD", prob=0.45, odds=135)
rc = combine([c1, c2])
check("same-game ML+spread same side => blocked", len(rc.correlation_blocks) == 1, f"got {rc.correlation_blocks}")

# --- same-game ML + spread on OPPOSITE side => allowed ---
c3 = Leg(game_id=10, sport="mlb", kind=LEG_ML, label="LAD ML", pick="home", side="LAD", prob=0.61, odds=-174)
c4 = Leg(game_id=10, sport="mlb", kind=LEG_SPREAD, label="SFG +1.5", pick="SFG +1.5", side="SFG", prob=0.50, odds=-110)
rc2 = combine([c3, c4])
check("same-game ML + opp spread side => allowed", rc2.correlation_blocks == [], f"got {rc2.correlation_blocks}")

# --- same-game correlation via EMPIRICAL table (V2: heuristic removed) ---
d1 = Leg(game_id=11, sport="mlb", kind=LEG_ML, label="NYY ML", pick="home", side="NYY", prob=0.62, odds=-190,
         meta={"favorite_side": "NYY"})
d2 = Leg(game_id=11, sport="mlb", kind=LEG_TOTAL, label="Over", pick="Over", side=None, prob=0.50, odds=-110)
# no correlation payload => favorite ML + Over treated as independent (no warn)
rn = combine([d1, d2])
check("fav ML+Over w/o empir table => no warning (heuristic removed)",
      rn.correlation_warnings == [], f"got {rn.correlation_warnings}")
# with an empirical corr above threshold => data-driven warning
corr_table = {"ml_fav:total_over": {"corr": 0.09, "n": 500}}
rc3 = combine([d1, d2], correlations=corr_table)
check("fav ML+Over w/ empir corr=0.09 => data-driven warning",
      len(rc3.correlation_warnings) == 1, f"got {rc3.correlation_warnings}")
# sub-threshold empirical corr => no warning (honest independent)
corr_low = {"ml_fav:total_over": {"corr": 0.005, "n": 5000}}
rn2 = combine([d1, d2], correlations=corr_low)
check("fav ML+Over w/ empir corr=0.005 => no warning",
      rn2.correlation_warnings == [], f"got {rn2.correlation_warnings}")

# --- as_dict serialization is JSON-safe ---
j = r2.to_dict()
check("to_dict has n_legs", j["n_legs"] == 2)
check("to_dict fair_american int", isinstance(j["fair_american"], int))
check("to_dict legs list", isinstance(j["legs"], list) and len(j["legs"]) == 2)

print(f"\nAll {passed} checks passed ✓")
