"""Register NBA moneyline + spread-odds movement features in nba.features.

Adds/updates the closing-minus-opening market-movement features computed in
``handicapping/nba/data_loader.py`` so the NBA models can train on them.

Features registered (all with `current_ats/current_ou/live_ats/live_ou/pick_card`
set to FALSE and `is_trainable=TRUE`, per Rich's instruction 2026-08-24 — they are
trainable model features but are intentionally NOT surfaced on pick cards or as
"current" features):

* ``ml_movement``            - Moneyline movement: closing home ML - opening home ML (raw American odds)
* ``away_ml_movement``       - Moneyline movement, away side
* ``ml_implied_movement``    - Moneyline implied-probability movement, home closure - opening
* ``spread_movement_implied``- Spread line move converted to win-probability units (/14.0)
* ``juice_movement_implied`` - Vig-free home-cover prob movement from spread-odds move
* ``market_move_home``       - COMBINED signal: spread_movement_implied + juice_movement_implied
                              (total market move toward home cover, in probability units)

Raw opening columns (``opening_home_ml`` / ``opening_away_ml`` / ``opening_spread_*_odds``)
are deliberately NOT registered (they exist in the DataFrame only as inputs to the
movement features), matching the existing convention for ``opening_spread``/``opening_ou``.

Idempotent: updates flags on existing rows, inserts missing ones.
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text, create_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import settings

DATABASE_URL = settings.database_url.replace("+asyncpg", "+psycopg2")

DESCRIPTIONS = {
    "ml_movement": "Moneyline movement: closing home ML - opening home ML (raw American odds points)",
    "away_ml_movement": "Moneyline movement: closing away ML - opening away ML (raw American odds points)",
    "ml_implied_movement": "Moneyline implied-probability movement: closing home implied - opening home implied",
    "spread_movement_implied": "Spread line move in win-probability units (closing-opening spread / ~14.0)",
    "juice_movement_implied": "Vig-free home-cover probability movement from spread odds (closing-prob minus opening-prob)",
    "market_move_home": "Combined market move toward home cover = spread_movement_implied + juice_movement_implied",
}

DISPLAY_NAMES = {
    "ml_movement": "ML Movement (Home)",
    "away_ml_movement": "ML Movement (Away)",
    "ml_implied_movement": "ML Implied Movement (Home)",
    "spread_movement_implied": "Spread Movement (Prob Units)",
    "juice_movement_implied": "Spread Juice Movement (Prob Units)",
    "market_move_home": "Market Move (Home Cover)",
}

FEATURE_NAMES = list(DESCRIPTIONS.keys())

# Columns that MUST be FALSE (only is_trainable is TRUE).
FALSE_BOOL_COLS = ["current_ats", "current_ou", "live_ats", "live_ou", "pick_card"]


def main() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    with engine.begin() as conn:
        max_sort = conn.execute(
            text("SELECT COALESCE(MAX(sort_order), 0) FROM nba.features")
        ).scalar() or 0
        for i, name in enumerate(FEATURE_NAMES):
            exists = conn.execute(
                text("SELECT 1 FROM nba.features WHERE name = :n"), {"n": name}
            ).scalar()
            if exists:
                print(f"  exists (flagging is_trainable=true, others false): {name}")
                sets = ", ".join(f"{c} = FALSE" for c in FALSE_BOOL_COLS)
                conn.execute(
                    text(
                        f"UPDATE nba.features SET {sets}, is_trainable = TRUE "
                        f", description = :d , display_name = :disp "
                        f", pick_card_section = NULL, sort_order = :sort "
                        f" WHERE name = :n"
                    ),
                    {
                        "d": DESCRIPTIONS[name],
                        "disp": DISPLAY_NAMES[name],
                        "sort": max_sort + (i + 1) * 10,
                        "n": name,
                    },
                )
            else:
                print(f"  inserting (is_trainable=true, others false): {name}")
                conn.execute(
                    text(
                        """INSERT INTO nba.features
                           (name, description, display_name,
                            current_ats, current_ou, is_trainable,
                            live_ats, live_ou, pick_card,
                            pick_card_section, sort_order)
                           VALUES (:n, :d, :disp,
                                   FALSE, FALSE, TRUE,
                                   FALSE, FALSE, FALSE,
                                   NULL, :sort)"""
                    ),
                    {
                        "n": name,
                        "d": DESCRIPTIONS[name],
                        "disp": DISPLAY_NAMES[name],
                        "sort": max_sort + (i + 1) * 10,
                    },
                )
    print("Done.")


if __name__ == "__main__":
    main()
