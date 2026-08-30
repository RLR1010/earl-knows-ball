#!/usr/bin/env python3
"""Fix nfl.features display names.

1. Strip the redundant trailing " H" / " A" suffix from display_name.
   The features are grouped Home/Away in the pick-card, so the suffix is noise.
2. Set curated, human-readable display names for features that were left with
   their raw snake_case feature name as the display name.

Dry-run by default; pass --apply to write to the DB.
"""
from __future__ import annotations

import sys
import psycopg2
from psycopg2.extras import RealDictCursor


# name -> human-readable display name for features whose display_name was the
# raw snake_case feature name (display_name == name).
GOOD_DISPLAY_NAMES: dict[str, str] = {
    # ---- Home/away advanced team ranks (home_stats / away_stats) ----
    "home_off_scoring_rank": "Off Scoring Rank",
    "home_off_yardage_rank": "Off Yardage Rank",
    "home_off_passing_rank": "Off Passing Rank",
    "home_off_rushing_rank": "Off Rushing Rank",
    "home_def_scoring_rank": "Def Scoring Rank",
    "home_def_yardage_rank": "Def Yardage Rank",
    "home_def_passing_rating_rank": "Def Pass Rating Rank",
    "home_def_rushing_rank": "Def Rushing Rank",
    "away_off_scoring_rank": "Off Scoring Rank",
    "away_off_yardage_rank": "Off Yardage Rank",
    "away_off_passing_rank": "Off Passing Rank",
    "away_off_rushing_rank": "Off Rushing Rank",
    "away_def_scoring_rank": "Def Scoring Rank",
    "away_def_yardage_rank": "Def Yardage Rank",
    "away_def_passing_rating_rank": "Def Pass Rating Rank",
    "away_def_rushing_rank": "Def Rushing Rank",
    # ---- Advanced adjusted / recent-weighted / EPA team stats (None) ----
    "home_adj_off_ppg": "Adj Off PPG",
    "home_adj_off_ypg": "Adj Off YPG",
    "home_adj_def_ppg": "Adj Def PPG",
    "home_adj_def_ypg": "Adj Def YPG",
    "away_adj_off_ppg": "Adj Off PPG",
    "away_adj_off_ypg": "Adj Off YPG",
    "away_adj_def_ppg": "Adj Def PPG",
    "away_adj_def_ypg": "Adj Def YPG",
    "home_rw_off_ppg": "Rec-Wk Off PPG",
    "home_rw_off_ypg": "Rec-Wk Off YPG",
    "home_rw_def_ppg": "Rec-Wk Def PPG",
    "home_rw_def_ypg": "Rec-Wk Def YPG",
    "away_rw_off_ppg": "Rec-Wk Off PPG",
    "away_rw_off_ypg": "Rec-Wk Off YPG",
    "away_rw_def_ppg": "Rec-Wk Def PPG",
    "away_rw_def_ypg": "Rec-Wk Def YPG",
    "home_off_epa_per_play": "Off EPA/Play",
    "home_def_epa_per_play": "Def EPA/Play",
    "away_off_epa_per_play": "Off EPA/Play",
    "away_def_epa_per_play": "Def EPA/Play",
    "home_off_pts_stddev_5": "Off Pts SD (5G)",
    "home_off_yds_stddev_5": "Off Yds SD (5G)",
    "home_def_pts_stddev_5": "Def Pts SD (5G)",
    "home_def_yds_stddev_5": "Def Yds SD (5G)",
    "away_off_pts_stddev_5": "Off Pts SD (5G)",
    "away_off_yds_stddev_5": "Off Yds SD (5G)",
    "away_def_pts_stddev_5": "Def Pts SD (5G)",
    "away_def_yds_stddev_5": "Def Yds SD (5G)",
    # ---- Metadata (None) ----
    "game_id": "Game ID",
    "season_id": "Season ID",
    "week": "Week",
    "status": "Status",
    "home_abbr": "Home Abbr",
    "away_abbr": "Away Abbr",
    "home_team_id": "Home Team ID",
    "away_team_id": "Away Team ID",
    "home_conf": "Home Conference",
    "away_conf": "Away Conference",
    "home_div": "Home Division",
    "away_div": "Away Division",
    "home_ats_stats": "Home ATS Stats",
    "away_ats_stats": "Away ATS Stats",
}


def load_url() -> str:
    for line in open(".env"):
        if line.startswith("ADMIN_DATABASE_URL="):
            return line.strip().split("=", 1)[1].replace(
                "postgresql+asyncpg://", "postgresql://")
    raise SystemExit("ADMIN_DATABASE_URL not found in .env")


def main() -> None:
    apply = "--apply" in sys.argv
    url = load_url()
    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # ---- 1. Strip redundant trailing " H" / " A" suffix ----
    cur.execute("""
        SELECT name, display_name
        FROM nfl.features
        WHERE display_name ~ ' [HA]$'
        ORDER BY name
    """)
    strip_rows = cur.fetchall()
    strip_plan = []
    for r in strip_rows:
        new_disp = r["display_name"][:-2]  # drop trailing " X"
        strip_plan.append((r["name"], r["display_name"], new_disp))

    # ---- 2. Set curated display names for raw-snake_case rows ----
    set_plan = []
    cur.execute("""
        SELECT name, display_name FROM nfl.features WHERE display_name = name
    """)
    for r in cur.fetchall():
        good = GOOD_DISPLAY_NAMES.get(r["name"])
        if good and good != r["display_name"]:
            set_plan.append((r["name"], r["display_name"], good))
        elif not good:
            print(f"WARN: no curated name for {r['name']!r} (current: {r['display_name']!r})")

    # ---- Report ----
    print(f"Strip H/A suffix: {len(strip_plan)} rows")
    for name, old, new in strip_plan:
        print(f"  {name:<38} {old!r:24} -> {new!r}")
    print(f"\nSet display name: {len(set_plan)} rows")
    for name, old, new in set_plan:
        print(f"  {name:<38} {old!r:26} -> {new!r}")

    if not apply:
        print("\nDRY-RUN: no changes written. Re-run with --apply to commit.")
        return

    # ---- Apply (single transaction) ----
    for name, _old, new in strip_plan:
        cur.execute("UPDATE nfl.features SET display_name=%s WHERE name=%s", (new, name))
    for name, _old, new in set_plan:
        cur.execute("UPDATE nfl.features SET display_name=%s WHERE name=%s", (new, name))
    conn.commit()
    print(f"\nAPPLIED: {len(strip_plan)} stripped, {len(set_plan)} display names set.")


if __name__ == "__main__":
    main()
