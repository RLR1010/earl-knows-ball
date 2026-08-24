#!/usr/bin/env python3
"""
Categorize pick_card features into display sections for the Detailed Analysis
-> Stats view. Values: home_stats | away_stats | game_context | betting_lines | other.

Sections on the frontend:
  - Home Team Stats   -> home_stats
  - Away Team Stats   -> away_stats
  - Game Context      -> game_context
  - Betting Lines     -> betting_lines

The categorizer is deterministic and idempotent: it only writes
pick_card_section for rows where pick_card = TRUE (features shown on the pick
card / Detailed Stats). Non-pick-card rows are left NULL.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

import psycopg2

DB_URL = os.environ.get("DATABASE_URL", "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football")

# ── betting-lines keywords (grep on lowercased name) ─────────────────────
BETTING_HINTS = [
    "moneyline", "implied", "implied_score", "spread", "ou_", "_ou", "closing",
    "opening", "_over_", "_under_", "over_odds", "under_odds", "odds_mvmt",
    "_odds", "cover_pct", "ats_", "ats_pct", "_ats", "ml_spread", "_total",
    "over_freq", "over_pct", "under_", "movement", "ou_margin", "ou_wins",
    "ou_over_pct", "away_moneyline", "home_moneyline", "spread_movement",
    "closing_spread", "closing_total", "implied_margin", "spread_diff",
    "over_implied", "ou_payout",
]

# ── game-context keywords (team-neutral, about the game/situation) ───────
CONTEXT_HINTS = [
    "rest_diff", "_rest", "b2b", "back_to_back", "travel", "tz_diff", "dome",
    "roof", "temp", "weather", "wind", "elevation", "venue", "surface", "grass",
    "precip", "cold_warm", "cold_", "_cold", "primetime", "_short", "is_division",
    "div_game", "day_night", "season_avg_pts", "combo_era", "combo_", "_diff",
    "diff_5", "diff_10", "mismatch", "five_in_eight", "four_in_five",
    "three_in_four", "game_date", "season_week", "_week", "venue_winpct",
    "team_venue", "games_played", "away_team", "home_team", "team_id",
    "away_abbr", "home_abbr", "record", "det_train", "distance",
]

# ── curated overrides for genuinely ambiguous team-neutral features ──────
# name (exact) -> section
OVERRIDES = {
    # MLB
    "away_pitcher_name": "away_stats",
    "home_pitcher_name": "home_stats",
    "home_pitcher": "home_stats",
    "away_pitcher": "away_stats",
    "aa": "game_context",
    "adiv": "game_context",
    "day_night": "game_context",
    "closing_ou": "betting_lines",
    "closing_over_odds": "betting_lines",
    "closing_under_odds": "betting_lines",
    "closing_spread_home_odds": "betting_lines",
    "closing_spread_away_odds": "betting_lines",
    "combo_era_r10": "game_context",
    "combo_era_r10_diff": "game_context",
    # NFL
    "is_dome": "game_context",
    "is_primetime": "game_context",
    "is_short": "game_context",
    "is_division_game": "game_context",
    "rest_diff": "game_context",
    "oudiff": "betting_lines",
    "spread": "betting_lines",
    "spread_movement": "betting_lines",
    "ou_movement": "betting_lines",
    "opening_ou": "betting_lines",
    "opening_spread": "betting_lines",
    "closing_spread": "betting_lines",
    "closing_ou": "betting_lines",
    # NBA
    "spread": "betting_lines",
    "spread_movement": "betting_lines",
    "ou_movement": "betting_lines",
    "closing_ou": "betting_lines",
    "over_odds": "betting_lines",
    "under_odds": "betting_lines",
    "over_implied_prob": "betting_lines",
    "implied_margin": "betting_lines",
    "ml_spread_mismatch": "betting_lines",
    "implied": "betting_lines",
    "rest_diff": "game_context",
    "rest_h": "game_context",
    "rest_a": "game_context",
    "home_b2b": "game_context",
    "away_b2b": "game_context",
    "team_id": "game_context",
    "home_team": "game_context",
    "away_team": "game_context",
    "home_abbr": "game_context",
    "away_abbr": "game_context",
    "date": "game_context",
    "travel_miles": "game_context",
    "away_implied": "away_stats",
    "home_implied": "home_stats",
    # MLB extra (from dry-run "other" review)
    "rest_h_hours": "game_context",
    "rest_a_hours": "game_context",
    "total_avg_team_r10": "game_context",
    "park_factor": "game_context",
    "ha": "game_context",
    "hdiv": "game_context",
    "is_div": "game_context",
    "is_home_fav": "betting_lines",
    "over_under": "betting_lines",
    # NFL extra (from dry-run "other" review)
    "hpa": "home_stats",
    "hpf": "home_stats",
    "apa": "away_stats",
    "apf": "away_stats",
    "aimp": "betting_lines",
    "himp": "betting_lines",
}


def has_hint(name, hints):
    ln = name.lower()
    return any(h.lower() in ln for h in hints)


def categorize(name):
    s = name.lower()
    # 1. explicit override
    if name in OVERRIDES:
        return OVERRIDES[name]
    if s in {x.lower() for x in OVERRIDES}:
        for k, v in OVERRIDES.items():
            if k.lower() == s:
                return v

    # 2. betting lines (before home/away prefix so home_moneyline hits betting)
    if has_hint(name, BETTING_HINTS):
        return "betting_lines"

    # 3. home/away team stats by prefix
    if name.startswith(("h_", "home_")) or s.startswith("home_"):
        return "home_stats"
    if name.startswith(("a_", "away_")) or s.startswith("away_"):
        return "away_stats"

    # 4. game context
    if has_hint(name, CONTEXT_HINTS):
        return "game_context"

    # 5. fallback
    return "other"


def main():
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    dry = "--dry-run" in sys.argv
    summary = {s: {} for s in ("mlb", "nfl", "nba")}
    for sport in ("mlb", "nfl", "nba"):
        cur.execute(f"SELECT name FROM {sport}.features WHERE pick_card = TRUE")
        names = [r[0] for r in cur.fetchall()]
        # update all rows regardless of their current value, so re-runs are idempotent
        for n in names:
            cat = categorize(n)
            summary[sport][cat] = summary[sport].get(cat, 0) + 1
            if not dry:
                cur.execute(
                    f"UPDATE {sport}.features SET pick_card_section = %s WHERE name = %s",
                    (cat, n),
                )
        # optionally null out non-pick-card rows so they're excluded from sections
        if not dry:
            cur.execute(f"UPDATE {sport}.features SET pick_card_section = NULL WHERE pick_card = FALSE")
        print(f"[{sport}] categorized {len(names)} pick-card features:", summary[sport])
        print(f"    {sum(summary[sport].values())} total")
    conn.close()


if __name__ == "__main__":
    main()
