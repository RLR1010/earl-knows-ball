"""Backfill sort_order for mlb/nfl/nba.features.

Assigns an initial display order for the Detailed Analysis Stats frontend.
Ordering is DISPLAY-ONLY: it never affects training/inference, which load
features in their own order via the data_loader.

Default policy:
  - pick_card features come first, ordered by section in the Detailed Analysis
    render order (game_context -> betting_lines -> home_stats -> away_stats),
    then by a stable tie-break (display_name, then name).
  - non-pick-card features follow, ordered by display_name/name.
  - Any existing sort_order is preserved for rows that already have one; only
    NULL sort_order rows get filled. Rows are then re-ranked to dense 0..N-1.

Run: cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/backfill_feature_sort_order.py
"""

import psycopg2

DB = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

# Section render order for Detailed Analysis -> Stats.
SECTION_ORDER = {
    "game_context": 0,
    "betting_lines": 1,
    "home_stats": 2,
    "away_stats": 3,
    "other": 4,
}


def main():
    conn = psycopg2.connect(DB)
    conn.autocommit = False
    cur = conn.cursor()
    for sport in ("mlb", "nfl", "nba"):
        cur.execute(
            f"SELECT name, display_name, pick_card, pick_card_section, sort_order "
            f"FROM {sport}.features"
        )
        rows = cur.fetchall()
        # rows with an existing sort_order: keep as-is (respect admin edits)
        anchored = [r for r in rows if r[4] is not None]
        # rows needing a default
        unranked = [r for r in rows if r[4] is None]

        def key(r):
            name, display_name, pick_card, section, _ = r
            disp = (display_name or name).lower()
            if pick_card:
                # pick-card first, grouped by section render order, name tie-break
                return (0, SECTION_ORDER.get(section, 4), disp)
            return (1, 0, disp)

        unranked_sorted = sorted(unranked, key=key)

        # Re-rank EVERYTHING (anchored + newly assigned) densely by final order:
        # anchored rows keep their relative sort_order; unranked rows go after all
        # anchored rows unless their computed key slots earlier. For a clean first
        # backfill (all NULL) that's simply the sorted default order.
        final = []
        final += sorted(anchored, key=lambda r: r[4])
        final += unranked_sorted

        for i, (name, *_ ) in enumerate(final):
            cur.execute(f"UPDATE {sport}.features SET sort_order = %s WHERE name = %s", (i, name))

        print(f"{sport}: {len(rows)} features ranked (anchored={len(anchored)}, defaulted={len(unranked)})")
    conn.commit()
    print("done")


if __name__ == "__main__":
    main()
