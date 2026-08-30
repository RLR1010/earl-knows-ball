"""
Recompute calibrated confidence + EV for MLB/NFL/NBA game_predictions from the
rebuilt (monotone) calibration curves.

WHY: the old calibration curves (mlb/nba/nfl_confidence_calibration.json) were
NON-monotonic and noise-fit — e.g. the MLB ATS curve mapped raw 0.562 -> 0.625
(inflating run-line confidence and EV), and raw 0.65 mapped DOWN to 0.52. After
`calibrate_confidence.py` was fixed to build monotone non-decreasing curves, the
stored `*_conf_cal` and `*_ev` columns still hold the OLD inflated values. This
script re-derives them from raw `*_conf` + the fixed curves, and recomputes EV
using the EXACT SAME `_ev()` math as the engines.

Mirror the engine exactly:  _ev(conf, odds) = conf*(100*profit_per_100(odds)) -
(1-conf)*100, with profit_per_100(odds) = 100/|odds| for negatives, odds/100 for
positives. Push rates are NOT separately modeled (the raw conf already reflects
the model's push-adjusted view; matches engine behavior).

Run:  cd backend && export $(grep -E '^DATABASE_URL=' .env)
      PYTHONPATH=. venv/bin/python app/scripts/recompute_calibration_ev.py [--sport mlb|nfl|nba] [--dry-run]
      (--dry-run prints what it WOULD update without writing)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import text

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND)

from app.database import async_session
from app.handicapping.calibrate_confidence import calibrate

# market key in the calibration JSON under which each column's curve lives
COL_CONFIG = {
    "ml": {"raw": "ml_conf", "cal": "ml_conf_cal"},
    "ats": {"raw": "rl_conf", "cal": "rl_conf_cal"},    # MLB; NFL/NBA use ats_conf
    "ou": {"raw": "ou_conf", "cal": "ou_conf_cal"},
}
# EV column per market, plus the odds column used to recompute it
EV_ODDS = {
    "mlb": {"ml": ("ml_ev", "ml_odds"), "ats": ("ats_ev", "ats_odds"), "ou": ("ou_ev", "ou_odds")},
    "nfl": {"ml": ("ml_ev", "ml_odds"), "ats": ("ats_ev", "ats_odds"), "ou": ("ou_ev", "ou_odds")},
    "nba": {"ml": ("ml_ev", "ml_odds"), "ats": ("ats_ev", "spread_odds"), "ou": ("ou_ev", "ou_odds")},
}
# NBA uses ats_conf for the spread market; MLB/NFL rl_conf
CONF_CAL_COL = {"mlb": "rl_conf_cal", "nfl": "ats_conf_cal", "nba": "ats_conf_cal"}
CONF_RAW_COL = {"mlb": "rl_conf", "nfl": "ats_conf", "nba": "ats_conf"}


def _profit_per_100(odds: float) -> float:
    odds = float(odds)
    if odds < 0:
        return 100.0 / abs(odds)
    return odds / 100.0


def _ev(conf: float, odds: float) -> float:
    profit = 100.0 * _profit_per_100(odds)
    conf = float(conf)
    return round((conf * profit) - ((1.0 - conf) * 100.0), 2)


async def recompute(sport: str, dry_run: bool) -> int:
    cal_col = CONF_CAL_COL[sport]
    ats_cal = "ats_conf_cal" if sport != "mlb" else "rl_conf_cal"
    ats_raw = "ats_conf" if sport != "mlb" else "rl_conf"

    # NFL/NBA have NO raw spread-confidence column (only ats_conf_cal), so we can
    # only re-derive markets that store a RAW column: ml + ou always; ats only
    # for MLB (rl_conf). The ats curves are still rebuilt (future predictions
    # benefit) but old NFL/NBA ats_conf_cal can't be back-recomputed from raw.
    has_ats_raw = None

    async with async_session() as db:
        col_exists = (await db.execute(text(
            f"SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            f"WHERE table_schema='{sport}' AND table_name='game_predictions' "
            f"AND column_name='{ats_raw}')"
        ))).scalar()
        has_ats_raw = bool(col_exists)
        ats_select = f"{ats_raw} AS ats_raw, {ats_cal} AS ats_cal, ats_odds" if has_ats_raw else "NULL::float AS ats_raw, NULL::double precision AS ats_cal, NULL::numeric AS ats_odds"
        ats_where = f"OR {ats_raw} IS NOT NULL" if has_ats_raw else ""
        rows = (await db.execute(text(f"""
            SELECT id,
                   ml_conf,     ml_conf_cal,     ml_odds,
                   {ats_select},
                   ou_conf,     ou_conf_cal,     ou_odds
            FROM {sport}.game_predictions
            WHERE ml_conf IS NOT NULL {ats_where} OR ou_conf IS NOT NULL
        """))).mappings().fetchall()

    updates = []
    for r in rows:
        rec = {"id": r["id"]}
        # ML
        if r["ml_conf"] is not None:
            cal = calibrate(float(r["ml_conf"]), "ml", sport)
            rec["ml_conf_cal"] = cal
            rec["ml_ev"] = _ev(cal, r["ml_odds"]) if r["ml_odds"] else None
        # ATS/spread
        if has_ats_raw and r["ats_raw"] is not None:
            cal = calibrate(float(r["ats_raw"]), "ats", sport)
            rec[ats_cal] = cal
            rec["ats_ev"] = _ev(cal, r["ats_odds"]) if r["ats_odds"] else None
        # OU
        if r["ou_conf"] is not None:
            cal = calibrate(float(r["ou_conf"]), "ou", sport)
            rec["ou_conf_cal"] = cal
            rec["ou_ev"] = _ev(cal, r["ou_odds"]) if r["ou_odds"] else None
        updates.append(rec)

    changes = [u for u in updates if any(k in u for k in ("ml_conf_cal", ats_cal, "ou_conf_cal"))]
    print(f"  {sport}: {len(rows)} rows with raw conf; {len(changes)} will have conf_cal recomputed")

    if dry_run:
        # show a few samples
        for u in updates[:4]:
            print("    sample:", u)
        return len(changes)

    async with async_session() as db:
        for u in updates:
            set_parts = []
            params = {"id": u["id"]}
            for col in ("ml_conf_cal", "ml_ev", ats_cal, "ats_ev", "ou_conf_cal", "ou_ev"):
                if col in u and u[col] is not None:
                    set_parts.append(f"{col} = :{col}")
                    params[col] = u[col]
            if not set_parts:
                continue
            await db.execute(text(
                f"UPDATE {sport}.game_predictions SET " + ", ".join(set_parts)
                + " WHERE id = :id"
            ), params)
        await db.commit()
    return len(changes)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["mlb", "nfl", "nba"], default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sports = [args.sport] if args.sport else ["mlb", "nfl", "nba"]
    for sport in sports:
        n = await recompute(sport, dry_run=args.dry_run)
        print(f"  -> {sport} done ({'dry-run' if args.dry_run else 'wrote'}) {n} updates")


if __name__ == "__main__":
    asyncio.run(main())
