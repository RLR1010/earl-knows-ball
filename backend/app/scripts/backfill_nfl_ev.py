"""
Backfill EV (ats_ev / ou_ev / ml_ev) for NFL game_predictions where they're NULL.

The NFL engine only computes EV at prediction time when odds were available
(`_ev()` in engine.py). For near-term/preseason games the Odds API odds weren't
captured yet, so the EV columns stayed NULL even though the prediction + calibrated
confidence were saved. Now that closing odds exist in betting_lines_consolidated,
this recomputes EV using the SAME logic as the engine.

MIRROR THE ENGINE EXACTLY (the _ev/profit math above is identical to engine.py).

Idempotent: only updates rows whose EV is NULL (or --force to recompute all).
Run: PYTHONPATH=$PWD ../venv/bin/python app/scripts/backfill_nfl_ev.py [--force]
"""

import asyncio
import sys

from sqlalchemy import text

from app.database import async_session

FORCE = "--force" in sys.argv


def _profit_per_100(odds: float) -> float:
    """Convert American odds to profit per $100 risked (mirrors engine.py)."""
    odds = float(odds)
    if odds < 0:
        return 100.0 / abs(odds)
    return odds / 100.0


def _ev(conf: float, odds: float) -> float:
    profit = 100.0 * _profit_per_100(odds)
    conf = float(conf)
    return round((conf * profit) - ((1.0 - conf) * 100.0), 2)


BACKFILL_SQL = text(
    """
    SELECT gp.id AS pred_id,
           gp.spread_pick, gp.ats_conf_cal,
           gp.ou_pick, gp.ou_conf_cal,
           gp.ml_pick, gp.ml_conf_cal,
           ht.abbreviation AS home_abbr,
           at.abbreviation AS away_abbr,
           blc.closing_spread_home_odds, blc.closing_spread_away_odds,
           blc.closing_over_odds, blc.closing_under_odds,
           blc.closing_home_ml, blc.closing_away_ml
    FROM nfl.game_predictions gp
    JOIN nfl.games g ON g.id = gp.game_id
    JOIN nfl.teams ht ON ht.id = g.home_team_id
    JOIN nfl.teams at ON at.id = g.away_team_id
    LEFT JOIN nfl.betting_lines_consolidated blc ON blc.game_id = g.id
    WHERE (gp.ats_ev IS NULL OR gp.ou_ev IS NULL OR gp.ml_ev IS NULL OR {force})
    ORDER BY gp.id
    """.format(force="TRUE" if FORCE else "FALSE")
)


async def main() -> int:
    async with async_session() as s:
        rows = (await s.execute(BACKFILL_SQL)).mappings().all()
        print(f"Found {len(rows)} NFL predictions with missing EV.")

        updated = 0
        filled = {"ats_ev": 0, "ou_ev": 0, "ml_ev": 0}

        for r in rows:
            home = r["home_abbr"]
            away = r["away_abbr"]

            # ATS
            ats_ev = None
            if r["ats_conf_cal"] is not None and r["spread_pick"]:
                if r["spread_pick"] == home and r["closing_spread_home_odds"] is not None:
                    ats_ev = _ev(r["ats_conf_cal"], r["closing_spread_home_odds"])
                elif r["spread_pick"] == away and r["closing_spread_away_odds"] is not None:
                    ats_ev = _ev(r["ats_conf_cal"], r["closing_spread_away_odds"])

            # OU
            ou_ev = None
            if r["ou_conf_cal"] is not None and r["ou_pick"]:
                if r["ou_pick"] == "Over" and r["closing_over_odds"] is not None:
                    ou_ev = _ev(r["ou_conf_cal"], r["closing_over_odds"])
                elif r["ou_pick"] == "Under" and r["closing_under_odds"] is not None:
                    ou_ev = _ev(r["ou_conf_cal"], r["closing_under_odds"])

            # ML
            ml_ev = None
            if r["ml_conf_cal"] is not None and r["ml_pick"]:
                if r["ml_pick"] == home and r["closing_home_ml"] is not None:
                    ml_ev = _ev(r["ml_conf_cal"], r["closing_home_ml"])
                elif r["ml_pick"] == away and r["closing_away_ml"] is not None:
                    ml_ev = _ev(r["ml_conf_cal"], r["closing_away_ml"])

            if ats_ev is None and ou_ev is None and ml_ev is None:
                continue

            await s.execute(
                text(
                    "UPDATE nfl.game_predictions SET ats_ev = :ats, ou_ev = :ou, ml_ev = :ml WHERE id = :id"
                ),
                {"ats": ats_ev, "ou": ou_ev, "ml": ml_ev, "id": r["pred_id"]},
            )
            updated += 1
            for k, v in (("ats_ev", ats_ev), ("ou_ev", ou_ev), ("ml_ev", ml_ev)):
                if v is not None:
                    filled[k] += 1

        await s.commit()
        print(f"Updated {updated} rows. Filled: {filled}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
