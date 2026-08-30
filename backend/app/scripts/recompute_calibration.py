"""Recompute stored calibrated confidence + EV from the CURRENT calibration curve.

WHY: stored *_conf_cal / *_ev columns are frozen snapshots written when each batch
of predictions was generated, using whatever calibration curve was live at that time.
Because the curve has been rebuilt repeatedly, the stored values are a mix of
vintages (and NBA's old curve was non-monotone/broken). This recomputes every row
against the current, restored naive curve so the results page is consistent.

SAFE: default is DRY-RUN (read-only, no writes). Use --apply to commit.
  - Recomputes *_conf_cal = calibrate(raw_conf, market, sport)
  - Recomputes *_ev      = _ev(cal, odds)   (same formula as the engines)
  - Idempotent: re-running is a no-op once applied.
  - Batched UPDATEs in a transaction.

Usage:
  python -m app.scripts.recompute_calibration                  # DRY-RUN
  python -m app.scripts.recompute_calibration --apply          # commit
"""
import argparse
import asyncio

from sqlalchemy import text

from app.database import get_db
from app.handicapping.calibrate_confidence import calibrate

# per sport: list of (raw_conf_col, cal_col, ev_col, odds_col, calibrate-market-type)
SPORT_MARKETS = {
    "mlb": [
        ("rl_conf",  "rl_conf_cal",  "ats_ev", "ats_odds", "ats"),
        ("ml_conf",  "ml_conf_cal",  "ml_ev",  "ml_odds",  "ml"),
        ("ou_conf",  "ou_conf_cal",  "ou_ev",  "ou_odds",  "ou"),
    ],
    "nfl": [
        ("margin_conf", "ats_conf_cal", "ats_ev", "ats_odds", "ats"),
        ("ml_conf",     "ml_conf_cal",  "ml_ev",  "ml_odds",  "ml"),
        ("ou_conf",     "ou_conf_cal",  "ou_ev",  "ou_odds",  "ou"),
    ],
    "nba": [
        ("margin_conf", "ats_conf_cal", "ats_ev", "ats_odds", "ats"),
        ("ml_conf",     "ml_conf_cal",  "ml_ev",  "ml_odds",  "ml"),
        ("ou_conf",     "ou_conf_cal",  "ou_ev",  "ou_odds",  "ou"),
    ],
}


def profit_per_100(odds: float) -> float:
    """Profit on a $100 bet at *odds* (American). Mirrors engines."""
    odds = float(odds)
    if odds < 0:
        return 100.0 / abs(odds)
    return odds / 100.0


def ev(conf: float, odds: float) -> float:
    """EV at $100 stake. Mirrors engines: conf*profit - (1-conf)*100."""
    return round((conf * 100.0 * profit_per_100(odds)) - ((1.0 - conf) * 100.0), 2)


async def recompute(apply: bool = False) -> None:
    total_changed = 0
    async for db in get_db():
        for sport, markets in SPORT_MARKETS.items():
            print(f"\n=== {sport.upper()} ===")
            for raw_col, cal_col, ev_col, odds_col, mkt in markets:
                rows = (await db.execute(text(
                    f"SELECT id, {raw_col} AS raw, {cal_col} AS cal, {ev_col} AS ev, {odds_col} AS odds "
                    f"FROM {sport}.game_predictions "
                    f"WHERE {raw_col} IS NOT NULL AND {raw_col}::text NOT IN ('NaN','inf','-inf')"
                ))).fetchall()

                plan = []
                n_change_cal = 0
                n_change_ev = 0
                for row in rows:
                    try:
                        raw = float(row.raw)
                    except Exception:
                        continue
                    new_cal = round(calibrate(raw, mkt, sport=sport), 4)
                    new_ev = None
                    if row.odds is not None:
                        try:
                            new_ev = ev(new_cal, float(row.odds))
                        except Exception:
                            new_ev = None
                    cal_ch = row.cal is None or abs(float(row.cal) - new_cal) > 1e-9
                    ev_ch = False
                    if new_ev is not None:
                        ev_ch = row.ev is None or abs(float(row.ev or 0) - new_ev) > 1e-9
                    if cal_ch or ev_ch:
                        plan.append(row.id)
                        n_change_cal += int(cal_ch)
                        n_change_ev += int(ev_ch)

                print(f"  [{mkt:4}] {raw_col:<12}->{cal_col:<14} {ev_col:<8}: rows={len(rows):6d}  "
                      f"would_change_cal={n_change_cal:6d}  ev_change={n_change_ev:6d}")
                total_changed += n_change_cal

                if apply and plan:
                    # batched UPDATE
                    for i in range(0, len(plan), 500):
                        chunk_ids = plan[i:i + 500]
                        # re-fetch the chunk values, recompute, and write
                        chunk = (await db.execute(text(
                            f"SELECT id, {raw_col} AS raw, {odds_col} AS odds "
                            f"FROM {sport}.game_predictions WHERE id = ANY(:ids)"
                        ).bindparams(ids=chunk_ids))).fetchall()
                        for row in chunk:
                            raw = float(row.raw)
                            new_cal = round(calibrate(raw, mkt, sport=sport), 4)
                            new_ev = ev(new_cal, float(row.odds)) if row.odds is not None else None
                            await db.execute(text(
                                f"UPDATE {sport}.game_predictions SET {cal_col}=:c, {ev_col}=:e WHERE id=:id"
                            ).bindparams(c=new_cal, e=new_ev, id=row.id))
                    await db.commit()
        print(f"\nTOTAL predictions whose stored calibrated confidence would change: {total_changed}")
        if apply:
            print("COMMITTED (--apply).")
        else:
            print("DRY-RUN — nothing written. Re-run with --apply to commit.")
        break


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    asyncio.run(recompute(apply=args.apply))
