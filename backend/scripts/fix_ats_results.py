#!/usr/bin/env python3
"""
Fix ATS results in nfl.game_predictions that were saved with wrong cover formula.

The bug: home_covered used `(score_diff) < spread` for favored teams (wrong direction).
Should use `(score_diff + spread) > 0` which matches _evaluate_year_model().

Run: python3 scripts/fix_ats_results.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import sync_url
import psycopg2

QUERY = """
SELECT gp.id, gp.game_id,
       g.home_score, g.away_score, g.home_team_id, g.away_team_id,
       gp.ats_result, gp.spread_pick,
       bl.closing_spread
FROM nfl.game_predictions gp
JOIN nfl.games g ON gp.game_id = g.id
LEFT JOIN nfl.betting_lines_consolidated bl ON g.id = bl.game_id
WHERE bl.closing_spread IS NOT NULL AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
"""

QUERY_TEAMS = """
SELECT id, abbreviation FROM nfl.teams
"""

UPDATE = """
UPDATE nfl.game_predictions
SET ats_result = %s
WHERE id = %s
"""


def fix_ats():
    conn = psycopg2.connect(sync_url.replace("+psycopg2", "").replace("+asyncpg", ""))
    cur = conn.cursor()

    # Build team abbreviation lookup
    cur.execute(QUERY_TEAMS)
    team_abbr = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute(QUERY)
    rows = cur.fetchall()

    fixed = 0
    total = len(rows)

    for row in rows:
        pred_id, game_id, home_score, away_score, home_team_id, away_team_id, \
            ats_result, spread_pick, spread = row

        # Correct formula (matches _evaluate_year_model)
        home_covered = (home_score - away_score + spread) > 0

        # Determine what the model predicted from spread_pick
        home_abbr = team_abbr.get(home_team_id, "")
        if spread_pick and spread_pick.strip() == home_abbr:
            predicted_home_cover = True
        else:
            predicted_home_cover = False

        new_result = "Win" if home_covered == predicted_home_cover else "Loss"

        if new_result != ats_result:
            print(f"  Fixing game {game_id}: {ats_result} -> {new_result} "
                  f"(home_score={home_score}, away={away_score}, spread={spread}, "
                  f"covered={home_covered}, predicted_home={predicted_home_cover})")
            cur.execute(UPDATE, (new_result, pred_id))
            fixed += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"\nChecked {total} predictions, fixed {fixed}")
    return fixed


if __name__ == "__main__":
    fix_ats()
