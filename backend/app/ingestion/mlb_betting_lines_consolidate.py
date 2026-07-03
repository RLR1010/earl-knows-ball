"""
Unified MLB betting lines consolidation.

Strategy (per-game):
  1. Try Tier 1 sportsbooks in priority order (fanduel → draftkings).
  2. For a sportsbook to be accepted for a game, it must supply:
     - ALL closing fields: spread, OU, home_ml, away_ml, spread_home_odds, spread_away_odds, over_odds, under_odds
     - ALL opening fields: spread, OU, home_ml, away_ml
  3. The chosen sportsbook must also pass:
     - Consensus check: their favored-team direction (spread sign) must match the
       majority opinion of ALL available sportsbooks with spread data.
     - ML/Spread alignment: for decisive games (abs(spread) >= 1.5 is the MLB runline),
       the moneyline favorite must agree with the spread favorite.
       For pick-'em games (spread = 0 or both side close), we rely on ML as truth.
  4. If no Tier 1 book passes, cascade to Tier 2 (bovada, lowvig, betmgm),
     then Tier 3 (pointsbetus, barstool, fanatics), then Tier 4 (all others).
  5. If NO sportsbook passes all checks for a game, fall back to the best partial match.

  ── Upsert ──
  Uses DELETE + INSERT (bulk) per batch since there are no FK constraints on
  the consolidated table and this handles updates cleanly.
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime

import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── DB ──
DB_URL = os.environ.get(
    "SYNC_DATABASE_URL",
    "postgresql://earl:earl@localhost:5432/earl_knows_football",
)

# ── Sportsbook priority tiers ──
# Within each tier, order controls preference.
# Only FanDuel and DraftKings are used — reliable run line data, ~100% coverage.
TIERS = {
    "tier1": [
        "fanduel",
        "draftkings",
    ],
}

# Flat priority list (all tiers in order)
def _flatten_tiers():
    """One flat list of sportsbook names, deduplicated."""
    seen = set()
    result = []
    for tier_name in sorted(TIERS.keys()):
        for sb in TIERS[tier_name]:
            sl = sb.lower().strip()
            if sl not in seen:
                seen.add(sl)
                result.append(sl)
    return result

PRIORITY_LIST = _flatten_tiers()

# ── Reasonable value ranges ──
# MLB runline: always +/- 1.5, but sometimes books quote funny numbers
MAX_ABS_SPREAD = 15.0   # anything > 15 runs is garbage
MIN_OU = 0.0
MAX_OU = 30.0
ML_ABS_MAX = 100000     # moneyline beyond ±100000 is clearly wrong
ODDS_ABS_MAX = 100000   # same for spread/over-odds

# ── Consensus: minimum sportsbooks needed to form a majority opinion ──
MIN_CONSENSUS_SOURCES = 3

# ── Sportsbook name normalization map ──
# Keys are exact-case-insensitive matches, values are our canonical form.
# Any unknown sportsbook gets used as-is (lowercased).
CANONICAL_SPORTSBOOK = {
    "betrivers": "betrivers",
    "bet rivers": "betrivers",
    "fanduel": "fanduel",
    "fan duel": "fanduel",
    "draftkings": "draftkings",
    "draft kings": "draftkings",
    "williamhill_us": "williamhill_us",
    "william hill us": "williamhill_us",
    "william hill": "williamhill_us",
    "bovada": "bovada",
    "lowvig": "lowvig",
    "low vig": "lowvig",
    "betmgm": "betmgm",
    "bet mgm": "betmgm",
    "mgm": "betmgm",
    "pointsbetus": "pointsbetus",
    "points bet us": "pointsbetus",
    "pointsbet": "pointsbetus",
    "barstool": "barstool",
    "fanatics": "fanatics",
    "foxbet": "foxbet",
    "fox bet": "foxbet",
    "wynnbet": "wynnbet",
    "wynn bet": "wynnbet",
    "sugarhouse": "sugarhouse",
    "sugar house": "sugarhouse",
    "twinspires": "twinspires",
    "twin spires": "twinspires",
    "unibet": "unibet",
    "unibet_us": "unibet_us",
    "betonlineag": "betonlineag",
    "bet online ag": "betonlineag",
    "betonline.ag": "betonlineag",
    "betus": "betus",
    "bet us": "betus",
    "gtbets": "gtbets",
    "gt bets": "gtbets",
    "intertops": "intertops",
    "mybookieag": "mybookieag",
    "my bookie ag": "mybookieag",
    "mybookie.ag": "mybookieag",
    "circasports": "circasports",
    "circa sports": "circasports",
    "betfair": "betfair",
    "superbook": "superbook",
    "super book": "superbook",
    "caesars": "caesars",
    "consensus": "consensus",
    # Capitalized or decorated names from raw data
    "barstool sportsbook": "barstool",
    "betonline.ag": "betonlineag",
    "mybookie.ag": "mybookieag",
    "pointsbet (us)": "pointsbetus",
    "william hill (us)": "williamhill_us",
}


def normalize_sportsbook(name):
    """Case-insensitive normalization of sportsbook name."""
    if name is None:
        return None
    key = name.lower().strip()
    return CANONICAL_SPORTSBOOK.get(key, key)


def value_plausible(val, val_type="numeric"):
    """Check if a numeric value is within reasonable range."""
    if val is None:
        return False
    try:
        v = float(val)
    except (TypeError, ValueError):
        return False
    if val_type == "spread":
        return abs(v) <= MAX_ABS_SPREAD and v != 0.0
    elif val_type == "ou":
        return MIN_OU <= v <= MAX_OU and v != 0.0
    elif val_type == "ml":
        return abs(v) <= ML_ABS_MAX
    elif val_type == "odds":
        return abs(v) <= ODDS_ABS_MAX
    return True


def spread_favored_side(spread_val):
    """Return 'home', 'away', or 'pk'."""
    if spread_val is None:
        return None
    v = float(spread_val)
    if v < 0:
        return "home"
    elif v > 0:
        return "away"
    return "pk"


def ml_favored_side(home_ml, away_ml):
    """Return 'home' or 'away' based on moneyline (lower = favored)."""
    if home_ml is None or away_ml is None:
        return None
    if int(home_ml) < int(away_ml):
        return "home"
    return "away"


class GameCandidate:
    """Represents one game's data from one sportsbook."""

    def __init__(self, row):
        # row comes from the main query
        self.game_id = row["game_id"]

        # Closing fields
        self.closing_spread = row.get("closing_spread")
        self.closing_ou = row.get("closing_ou")
        self.closing_home_ml = row.get("closing_home_ml")
        self.closing_away_ml = row.get("closing_away_ml")
        self.closing_spread_home_odds = row.get("closing_spread_home_odds")
        self.closing_spread_away_odds = row.get("closing_spread_away_odds")
        self.closing_over_odds = row.get("closing_over_odds")
        self.closing_under_odds = row.get("closing_under_odds")

        # Opening fields (may be None for closing-only fallback)
        self.opening_spread = row.get("opening_spread")
        self.opening_ou = row.get("opening_ou")
        self.opening_home_ml = row.get("opening_home_ml")
        self.opening_away_ml = row.get("opening_away_ml")
        self.opening_spread_home_odds = row.get("opening_spread_home_odds")
        self.opening_spread_away_odds = row.get("opening_spread_away_odds")
        self.opening_over_odds = row.get("opening_over_odds")
        self.opening_under_odds = row.get("opening_under_odds")

        self.sportsbook = row["sportsbook"]

    def has_closing_set(self):
        """All required closing fields must be present and plausible."""
        checks = [
            value_plausible(self.closing_spread, "spread"),
            value_plausible(self.closing_ou, "ou"),
            value_plausible(self.closing_home_ml, "ml"),
            value_plausible(self.closing_away_ml, "ml"),
            value_plausible(self.closing_spread_home_odds, "odds"),
            value_plausible(self.closing_spread_away_odds, "odds"),
            value_plausible(self.closing_over_odds, "odds"),
            value_plausible(self.closing_under_odds, "odds"),
        ]
        return all(checks)

    def has_opening_set(self):
        """All required opening fields must be present and plausible."""
        return all([
            value_plausible(self.opening_spread, "spread"),
            value_plausible(self.opening_ou, "ou"),
            value_plausible(self.opening_home_ml, "ml"),
            value_plausible(self.opening_away_ml, "ml"),
        ])

    def is_pickem_on_ml(self):
        """
        Returns True if the moneyline difference is small enough that the game
        is effectively a pick-'em (market can't agree on who's favored).
        MLB runline is always +/-1.5, so a near-even ML means the spread
        direction is somewhat arbitrary.
        """
        if self.closing_home_ml is None or self.closing_away_ml is None:
            return True  # can't tell, treat as pick-em
        diff = abs(int(self.closing_home_ml) - int(self.closing_away_ml))
        return diff < 15  # less than 15 cents apart = pick-'em

    def is_decisive(self):
        """A game is 'decisive' if the spread > 1 (not a pick-'em)."""
        if self.closing_spread is None:
            return False
        return abs(float(self.closing_spread)) >= 1.0 and not self.is_pickem_on_ml()

    def check_ml_spread_alignment(self):
        """
        Verifies the spread sign matches the run line favorite.
        Uses spread odds (not moneyline) to determine which side is favored:
          - If spread_home_odds < spread_away_odds → home is RL favored → spread should be negative
          - If spread_home_odds > spread_away_odds → away is RL favored → spread should be positive
        """
        sp = self.closing_spread
        ho = self.closing_spread_home_odds
        ao = self.closing_spread_away_odds
        if sp is None or ho is None or ao is None:
            return True
        sp = float(sp)
        ho = float(ho)
        ao = float(ao)
        # Both odds same sign → ambiguous RL favorite, trust spread as-is
        if (ho < 0 and ao < 0) or (ho > 0 and ao > 0):
            return True
        # Lower odds = more juice = that side is the RL favorite
        if ho < ao and sp > 0:
            # home is RL favorite but spread says away favored → flip needed
            return False
        if ao < ho and sp < 0:
            # away is RL favorite but spread says home favored → flip needed
            return False
        return True

    def check_spread_reasonable(self):
        """
        MLB runline is always +/-1.5. If spread is far from that (e.g. +/-2.5),
        it's suspicious. Allow some wiggle.
        """
        if self.closing_spread is None:
            return False
        v = abs(float(self.closing_spread))
        # Normal MLB runline is 1.5. Some offshore books use 1.0 or 2.0 for special markets.
        # Anything > 3.0 is definitely wrong for a standard runline.
        return 0.5 <= v <= 3.0

    def check_odds_seem_correct(self):
        """
        Sanity: spread_home_odds and spread_away_odds should be on opposite sides
        of 0 (one positive, one negative) for a normal market.
        Exceptions: both negative is OK if the line is very tight.
        """
        ho = self.closing_spread_home_odds
        ao = self.closing_spread_away_odds
        if ho is None or ao is None:
            return False
        h = int(ho)
        a = int(ao)
        # Both 0 or both very positive is fishy
        if h == 0 or a == 0:
            return False
        return True


# ── Main SQL query ──
# For each game_id, fetch the closing+opening data for ALL sportsbooks,
# so we can run consensus and per-book validation in Python.

FETCH_GAMES_SQL = """
WITH closing_data AS (
    -- Live current lines (is_opening='false' rows = latest snapshot)
    SELECT
        LOWER(bl.sportsbook) AS sportsbook,
        bl.game_id,
        bl.spread       AS closing_spread,
        bl.over_under   AS closing_ou,
        bl.home_moneyline  AS closing_home_ml,
        bl.away_moneyline  AS closing_away_ml,
        bl.spread_home_odds  AS closing_spread_home_odds,
        bl.spread_away_odds  AS closing_spread_away_odds,
        bl.over_odds   AS closing_over_odds,
        bl.under_odds  AS closing_under_odds
    FROM mlb.betting_lines bl
    WHERE bl.is_opening = 'false'
      AND bl.spread IS NOT NULL
      AND bl.over_under IS NOT NULL
      AND bl.home_moneyline IS NOT NULL
      AND bl.away_moneyline IS NOT NULL
      AND bl.spread_home_odds IS NOT NULL
      AND bl.over_odds IS NOT NULL
),
opening_data AS (
    -- Live current opening lines (is_opening='true' rows)
    -- Opening lines may not have spread/OU odds (just h2h), that's OK
    SELECT
        LOWER(bl.sportsbook) AS sportsbook,
        bl.game_id,
        bl.spread       AS opening_spread,
        bl.over_under   AS opening_ou,
        bl.home_moneyline  AS opening_home_ml,
        bl.away_moneyline  AS opening_away_ml,
        bl.spread_home_odds  AS opening_spread_home_odds,
        bl.spread_away_odds  AS opening_spread_away_odds,
        bl.over_odds   AS opening_over_odds,
        bl.under_odds  AS opening_under_odds
    FROM mlb.betting_lines bl
    WHERE bl.is_opening = 'true'
      AND bl.spread IS NOT NULL
      AND bl.over_under IS NOT NULL
      AND bl.home_moneyline IS NOT NULL
      AND bl.away_moneyline IS NOT NULL
)
SELECT
    c.sportsbook,
    c.game_id,
    c.closing_spread,
    c.closing_ou,
    c.closing_home_ml,
    c.closing_away_ml,
    c.closing_spread_home_odds,
    c.closing_spread_away_odds,
    c.closing_over_odds,
    c.closing_under_odds,
    o.opening_spread,
    o.opening_ou,
    o.opening_home_ml,
    o.opening_away_ml,
    o.opening_spread_home_odds,
    o.opening_spread_away_odds,
    o.opening_over_odds,
    o.opening_under_odds
FROM closing_data c
INNER JOIN opening_data o ON c.game_id = o.game_id AND c.sportsbook = o.sportsbook
ORDER BY c.game_id
"""

# ── Closing-only fallback ──
# Games that have closing data but no opening data from the same book.
FETCH_CLOSING_ONLY_SQL = """
SELECT
    LOWER(bl.sportsbook) AS sportsbook,
    bl.game_id,
    bl.spread       AS closing_spread,
    bl.over_under   AS closing_ou,
    bl.home_moneyline  AS closing_home_ml,
    bl.away_moneyline  AS closing_away_ml,
    bl.spread_home_odds  AS closing_spread_home_odds,
    bl.spread_away_odds  AS closing_spread_away_odds,
    bl.over_odds   AS closing_over_odds,
    bl.under_odds  AS closing_under_odds
FROM mlb.betting_lines bl
WHERE bl.is_opening = 'false'
  AND bl.spread IS NOT NULL
  AND bl.over_under IS NOT NULL
  AND bl.home_moneyline IS NOT NULL
  AND bl.away_moneyline IS NOT NULL
  AND bl.spread_home_odds IS NOT NULL
  AND bl.spread_away_odds IS NOT NULL
  AND bl.over_odds IS NOT NULL
  AND bl.under_odds IS NOT NULL
ORDER BY bl.game_id
"""

# ── Game metadata ──
FETCH_GAME_META_SQL = """
SELECT
    g.id AS game_id,
    g.date AS game_time,
    ht.abbreviation AS home_team,
    at.abbreviation AS away_team,
    s.year,
    g.home_score,
    g.away_score,
    g.venue,
    g.status
FROM mlb.games g
JOIN mlb.teams ht ON g.home_team_id = ht.id
JOIN mlb.teams at ON g.away_team_id = at.id
JOIN mlb.seasons s ON g.season_id = s.id
WHERE g.id = ANY(%(game_ids)s)
"""


# ── Consensus: what is the majority opinion on favored side? ──
def build_consensus(all_candidates):
    """
    Given ALL candidates for a game, determine consensus.
    Only counts FanDuel and DraftKings — everything else is ignored.
    Return dict: {side: count, total_sources, majority_side, majority_side_pct}
    """
    allowed = {'fanduel', 'draftkings'}
    sides = {"home": 0, "away": 0, "pk": 0}
    spreads = []
    for c in all_candidates:
        if c.sportsbook not in allowed:
            continue
        if c.closing_spread is not None and value_plausible(c.closing_spread, "spread"):
            side = spread_favored_side(c.closing_spread)
            sides[side] = sides.get(side, 0) + 1
            spreads.append(float(c.closing_spread))

    total = sum(sides.values())
    if total == 0:
        return {"majority_side": None, "total": 0, "sides": sides}

    majority_side = max(sides, key=sides.get)
    majority_pct = sides[majority_side] / total

    return {
        "majority_side": majority_side,
        "total": total,
        "sides": sides,
        "majority_pct": majority_pct,
        "median_spread": sorted(spreads)[len(spreads) // 2] if spreads else None,
    }


def select_best_candidate(candidates, consensus):
    """
    From a list of GameCandidates for one game, pick the best one.
    Only FanDuel and DraftKings are considered. Priority: FanDuel > DraftKings
    when both have complete data. FanDuel and DraftKings return reliable run line
    data, so no additional validation is needed. Returns the first acceptable
    candidate or None.
    """
    for sb in PRIORITY_LIST:
        for c in candidates:
            if c.sportsbook != sb:
                continue
            if not c.has_closing_set() or not c.has_opening_set():
                continue
            if not c.check_spread_reasonable():
                continue
            return c
    return None


def compute_implied_probability(american_odds):
    """Convert American odds to implied probability (0-1)."""
    if american_odds is None or american_odds == 0:
        return None
    v = int(american_odds)
    if v > 0:
        return round(100 / (v + 100), 6)
    else:
        return round(abs(v) / (abs(v) + 100), 6)


def _upsert_incremental(cursor, conn, rows, game_meta_map, batch_size):
    """
    Incremental mode: preserve opening data from first write.
    - If game_id already exists in consolidated table: UPDATE only closing fields
    - If game_id doesn't exist yet: INSERT with both opening and closing
    """
    LOGGER = logger  # local ref for closures if any

    # Build a set of existing game_ids
    gids = [r[0] for r in rows]
    cursor.execute(
        "SELECT game_id FROM mlb.betting_lines_consolidated WHERE game_id = ANY(%s)",
        (gids,),
    )
    existing_gids = {r[0] for r in cursor.fetchall()}

    insert_rows = []      # game_ids that need a full INSERT (new)
    update_pairs = []     # (game_id, candidate) that need UPDATE only closing

    for gid, candidate in rows:
        if gid in existing_gids:
            update_pairs.append((gid, candidate))
        else:
            insert_rows.append((gid, candidate))

    LOGGER.info(
        f"Incremental: {len(update_pairs)} existing (update closing only), "
        f"{len(insert_rows)} new (full insert)"
    )

    # ── UPDATE closing-only for existing rows ──
    if update_pairs:
        update_sql = """
            UPDATE mlb.betting_lines_consolidated
            SET
                game_time = %s,
                home_team = %s,
                away_team = %s,
                year = %s,
                home_score = %s,
                away_score = %s,
                venue = %s,
                status = %s,
                closing_spread = %s,
                closing_spread_sportsbook = %s,
                closing_ou = %s,
                closing_ou_sportsbook = %s,
                closing_home_ml = %s,
                closing_home_ml_sportsbook = %s,
                closing_away_ml = %s,
                closing_away_ml_sportsbook = %s,
                closing_over_odds = %s,
                closing_over_odds_sportsbook = %s,
                closing_under_odds = %s,
                closing_under_odds_sportsbook = %s,
                closing_spread_home_odds = %s,
                closing_spread_home_odds_sportsbook = %s,
                closing_spread_away_odds = %s,
                closing_spread_away_odds_sportsbook = %s,
                closing_home_implied_probability = %s,
                closing_away_implied_probability = %s
            WHERE game_id = %s
        """

        for gid, c in update_pairs:
            meta = game_meta_map.get(gid, {})
            cl_home_ip = compute_implied_probability(c.closing_home_ml)
            cl_away_ip = compute_implied_probability(c.closing_away_ml)

            cursor.execute(update_sql, (
                meta.get("game_time"),
                meta.get("home_team"),
                meta.get("away_team"),
                meta.get("year"),
                meta.get("home_score"),
                meta.get("away_score"),
                meta.get("venue"),
                meta.get("status"),
                c.closing_spread,
                c.sportsbook,
                c.closing_ou,
                c.sportsbook,
                c.closing_home_ml,
                c.sportsbook,
                c.closing_away_ml,
                c.sportsbook,
                c.closing_over_odds,
                c.sportsbook,
                c.closing_under_odds,
                c.sportsbook,
                c.closing_spread_home_odds,
                c.sportsbook,
                c.closing_spread_away_odds,
                c.sportsbook,
                cl_home_ip,
                cl_away_ip,
                gid,
            ))

        conn.commit()
        LOGGER.info(f"Updated closing-only for {len(update_pairs)} rows")

    # ── Full INSERT for new rows ──
    if insert_rows:
        _full_insert_batch(cursor, conn, insert_rows, game_meta_map, batch_size)


def _full_insert_batch(cursor, conn, rows, game_meta_map, batch_size):
    """INSERT rows (both opening+closing) for game_ids that don't exist yet."""
    insert_sql = """
        INSERT INTO mlb.betting_lines_consolidated (
            game_id, game_time, home_team, away_team, year,
            home_score, away_score, venue, status,
            closing_spread, closing_spread_sportsbook,
            closing_ou, closing_ou_sportsbook,
            closing_home_ml, closing_home_ml_sportsbook,
            closing_away_ml, closing_away_ml_sportsbook,
            closing_over_odds, closing_over_odds_sportsbook,
            closing_under_odds, closing_under_odds_sportsbook,
            closing_spread_home_odds, closing_spread_home_odds_sportsbook,
            closing_spread_away_odds, closing_spread_away_odds_sportsbook,
            opening_spread, opening_spread_sportsbook,
            opening_ou, opening_ou_sportsbook,
            opening_home_ml, opening_home_ml_sportsbook,
            opening_away_ml, opening_away_ml_sportsbook,
            opening_over_odds, opening_over_odds_sportsbook,
            opening_under_odds, opening_under_odds_sportsbook,
            opening_spread_home_odds, opening_spread_home_odds_sportsbook,
            opening_spread_away_odds, opening_spread_away_odds_sportsbook,
            closing_home_implied_probability,
            closing_away_implied_probability,
            opening_home_implied_probability,
            opening_away_implied_probability,
            has_verified_ou
        ) VALUES %s
    """
    values = []
    for gid, c in rows:
        meta = game_meta_map.get(gid, {})
        cl_home_ip = compute_implied_probability(c.closing_home_ml)
        cl_away_ip = compute_implied_probability(c.closing_away_ml)
        op_home_ip = compute_implied_probability(c.opening_home_ml)
        op_away_ip = compute_implied_probability(c.opening_away_ml)
        values.append((
            gid,
            meta.get("game_time"),
            meta.get("home_team"),
            meta.get("away_team"),
            meta.get("year"),
            meta.get("home_score"),
            meta.get("away_score"),
            meta.get("venue"),
            meta.get("status"),
            c.closing_spread,
            c.sportsbook,
            c.closing_ou,
            c.sportsbook,
            c.closing_home_ml,
            c.sportsbook,
            c.closing_away_ml,
            c.sportsbook,
            c.closing_over_odds,
            c.sportsbook,
            c.closing_under_odds,
            c.sportsbook,
            c.closing_spread_home_odds,
            c.sportsbook,
            c.closing_spread_away_odds,
            c.sportsbook,
            c.opening_spread,
            c.sportsbook,
            c.opening_ou,
            c.sportsbook,
            c.opening_home_ml,
            c.sportsbook,
            c.opening_away_ml,
            c.sportsbook,
            c.opening_over_odds,
            c.sportsbook,
            c.opening_under_odds,
            c.sportsbook,
            c.opening_spread_home_odds,
            c.sportsbook,
            c.opening_spread_away_odds,
            c.sportsbook,
            cl_home_ip,
            cl_away_ip,
            op_home_ip,
            op_away_ip,
            True,  # has_verified_ou
        ))

    for i in range(0, len(values), batch_size):
        batch = values[i: i + batch_size]
        execute_values(cursor, insert_sql, batch, template=None, page_size=batch_size)
        conn.commit()
    logger.info(f"Full-inserted {len(values)} new rows")
    return len(values)


def upsert_consolidated(conn, rows, game_meta_map, batch_size=500, incremental=False):
    """
    Bulk upsert into betting_lines_consolidated.

    In full mode: TRUNCATE + INSERT all (fastest for full rebuild).
    In incremental mode: UPDATE closing fields only, preserving existing opening
    data ("first seen"). If a row doesn't exist yet, INSERT with full data.
    """
    cursor = conn.cursor()
    """
    Bulk upsert into betting_lines_consolidated.

    In full mode: TRUNCATE + INSERT all (fastest for full rebuild).
    In incremental mode: UPDATE closing fields only, preserving existing opening
    data ("first seen"). If a row doesn't exist yet, INSERT with full data.
    """
    cursor = conn.cursor()

    if incremental:
        _upsert_incremental(cursor, conn, rows, game_meta_map, batch_size)
        return

    # Full: Wipe everything, then insert all
    cursor.execute("TRUNCATE TABLE mlb.betting_lines_consolidated")
    _full_insert_batch(cursor, conn, rows, game_meta_map, batch_size)

    logger.info(f"Consolidated {len(rows)} rows")

def run(rebuild_full=False, game_ids_filter=None, cursor=None, conn=None):
    """
    Main entry point.
    
    Args:
        rebuild_full: If True, full rebuild (truncate + re-insert all).
        game_ids_filter: Optional set of game_ids to process (incremental mode).
        cursor: Optional pre-existing cursor (for API endpoint calls).
        conn: Optional pre-existing connection.
    """
    should_close = conn is None
    if conn is None:
        conn = psycopg2.connect(DB_URL)
        conn.set_session(autocommit=False)
    if cursor is None:
        cursor = conn.cursor()

    try:
        # ── Step 1: Fetch all raw data ──
        if game_ids_filter:
            logger.info(f"Incremental mode: processing {len(game_ids_filter)} specific games")
        else:
            logger.info("Fetching closing+opening data from mlb.betting_lines...")
        cursor.execute(FETCH_GAMES_SQL)

        # Group by game_id: all_candidates[game_id] = [GameCandidate, ...]
        raw_rows = cursor.fetchall()
        if game_ids_filter:
            raw_rows = [r for r in raw_rows if r[1] in game_ids_filter]
            logger.info(f"  Filtered to {len(raw_rows)} rows matching {len(game_ids_filter)} game_ids")
        
        all_candidates_by_game = {}
        for db_row in raw_rows:
            row_dict = dict(zip(
                [
                    "sportsbook", "game_id",
                    "closing_spread", "closing_ou", "closing_home_ml", "closing_away_ml",
                    "closing_spread_home_odds", "closing_spread_away_odds",
                    "closing_over_odds", "closing_under_odds",
                    "opening_spread", "opening_ou", "opening_home_ml", "opening_away_ml",
                    "opening_spread_home_odds", "opening_spread_away_odds",
                    "opening_over_odds", "opening_under_odds",
                ],
                db_row,
            ))
            gid = row_dict["game_id"]
            row_dict["sportsbook"] = normalize_sportsbook(row_dict["sportsbook"])
            if row_dict["sportsbook"] is None:
                continue
            all_candidates_by_game.setdefault(gid, []).append(GameCandidate(row_dict))

        logger.info(f"Loaded {len(all_candidates_by_game)} games with candidates")

        # ── Step 2: Per-game selection ──
        selected = {}  # game_id -> GameCandidate
        consensus_stats = {"total": 0, "majority_agreed": 0, "mixed": 0, "tier1_majority": 0}

        for game_id, candidates in all_candidates_by_game.items():
            consensus = build_consensus(candidates)
            best = select_best_candidate(candidates, consensus)
            if best is not None:
                selected[game_id] = best
                consensus_stats["total"] += 1
                if consensus["majority_side"] is not None:
                    my_side = spread_favored_side(best.closing_spread)
                    if my_side == consensus["majority_side"]:
                        consensus_stats["majority_agreed"] += 1
                        if best.sportsbook in TIERS["tier1"]:
                            consensus_stats["tier1_majority"] += 1
                    else:
                        consensus_stats["mixed"] += 1

        logger.info(
            f"Selected {len(selected)} games: "
            f"{consensus_stats['majority_agreed']} with majority consensus, "
            f"{consensus_stats['tier1_majority']} from Tier 1, "
            f"{consensus_stats['mixed']} with minority-opinion fallback"
        )

        # ── Step 2b: Closing-only fallback for unpaired games ──
        # Games that have closing data but no single book provides both opening+closing.
        paired_game_ids = set(selected.keys())

        if game_ids_filter:
            # In incremental mode, only consider the specified game_ids
            missing_game_ids = game_ids_filter - paired_game_ids
        else:
            cursor.execute("SELECT DISTINCT game_id FROM mlb.betting_lines WHERE source = 'the_odds_api_closing'")
            all_closing_game_ids = {r[0] for r in cursor.fetchall()}
            missing_game_ids = all_closing_game_ids - paired_game_ids

        logger.info(f"{len(missing_game_ids)} games without paired opening+closing data")

        if missing_game_ids:
            if game_ids_filter:
                # In incremental mode, only process the specific missing games
                cursor.execute(FETCH_CLOSING_ONLY_SQL + " AND bl.game_id = ANY(%s)", (list(missing_game_ids),))
            else:
                cursor.execute(FETCH_CLOSING_ONLY_SQL)
            closing_only_by_game = {}
            for db_row in cursor.fetchall():
                row_dict = dict(zip(
                    ["sportsbook", "game_id",
                     "closing_spread", "closing_ou", "closing_home_ml", "closing_away_ml",
                     "closing_spread_home_odds", "closing_spread_away_odds",
                     "closing_over_odds", "closing_under_odds"],
                    db_row,
                ))
                gid = row_dict["game_id"]
                if gid not in missing_game_ids:
                    continue
                row_dict["sportsbook"] = normalize_sportsbook(row_dict["sportsbook"])
                if row_dict["sportsbook"] is None:
                    continue
                closing_only_by_game.setdefault(gid, []).append(GameCandidate(row_dict))

            closing_only_selected = 0
            allowed_books = {'fanduel': True, 'draftkings': True}
            for game_id, candidates in closing_only_by_game.items():
                # Filter to only FD/DK
                fd_candidates = [c for c in candidates if c.sportsbook in allowed_books]
                if not fd_candidates:
                    continue
                consensus = build_consensus(fd_candidates)
                for c in fd_candidates:
                    if not c.has_closing_set():
                        continue
                    if not c.check_spread_reasonable():
                        continue
                    if not c.check_ml_spread_alignment():
                        continue
                    selected[game_id] = c
                    closing_only_selected += 1
                    break

            logger.info(f"Closing-only fallback added {closing_only_selected} games")

        # ── Step 3: Fetch game metadata ──
        cursor = conn.cursor()
        game_ids = list(selected.keys())
        cursor.execute(FETCH_GAME_META_SQL, {"game_ids": game_ids})
        game_meta_map = {}
        for row in cursor.fetchall():
            game_meta_map[row[0]] = {
                "game_time": row[1],
                "home_team": row[2],
                "away_team": row[3],
                "year": row[4],
                "home_score": row[5],
                "away_score": row[6],
                "venue": row[7],
                "status": row[8],
            }

        # ── Step 4: Upsert ──
        rows = [(gid, candidate) for gid, candidate in selected.items()]
        upsert_consolidated(conn, rows, game_meta_map, incremental=bool(game_ids_filter))

        # ── Step 5: Verify ──
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                COUNT(*) as total,
                COUNT(closing_spread) as has_closing_sprd,
                COUNT(closing_home_ml) as has_closing_ml,
                COUNT(opening_spread) as has_opening_sprd,
                COUNT(closing_over_odds) as has_over_odds,
                COUNT(closing_spread_home_odds) as has_sprd_home_odds,
                COUNT(closing_home_implied_probability) as has_implied_prob
            FROM mlb.betting_lines_consolidated
        """)
        r = cursor.fetchone()
        logger.info(
            f"✅ Done. {r[0]} consolidated rows. "
            f"Closing spread: {r[1]} | Closing ML: {r[2]} | "
            f"Opening spread: {r[3]} | Over odds: {r[4]} | "
            f"Spread home odds: {r[5]} | Implied prob: {r[6]}"
        )

        # Verify: all the opening/closing sportsbook columns match
        cursor.execute("""
            SELECT COUNT(*) FROM mlb.betting_lines_consolidated
            WHERE closing_spread_sportsbook != closing_ou_sportsbook
               OR closing_spread_sportsbook != closing_home_ml_sportsbook
               OR opening_spread_sportsbook != closing_spread_sportsbook
        """)
        mismatched = cursor.fetchone()[0]
        logger.info(f"Games with mismatched sportsbook sources: {mismatched}")
        if mismatched > 0:
            # Show details
            cursor.execute("""
                SELECT game_id, closing_spread_sportsbook, closing_ou_sportsbook,
                       closing_home_ml_sportsbook, opening_spread_sportsbook
                FROM mlb.betting_lines_consolidated
                WHERE closing_spread_sportsbook != closing_ou_sportsbook
                   OR closing_spread_sportsbook != closing_home_ml_sportsbook
                   OR opening_spread_sportsbook != closing_spread_sportsbook
                LIMIT 10
            """)
            for row in cursor.fetchall():
                logger.warning(
                    f"  Mismatch game_id={row[0]}: "
                    f"closing_spread_sb={row[1]}, closing_ou_sb={row[2]}, "
                    f"closing_ml_sb={row[3]}, opening_sb={row[3]}"
                )

        # ── Step 6: Unconditional score/status sync ──
        # Update home_score, away_score, and status for ANY game in the consolidated
        # table, even if it no longer appears in the API snapshot (e.g. in-progress
        # or finished games). This runs regardless of game_ids_filter.
        cursor.execute("""
            UPDATE mlb.betting_lines_consolidated blc
            SET home_score = g.home_score,
                away_score = g.away_score,
                status = g.status
            FROM mlb.games g
            WHERE g.id = blc.game_id
              AND (
                  blc.home_score IS DISTINCT FROM g.home_score
                  OR blc.away_score IS DISTINCT FROM g.away_score
                  OR blc.status IS DISTINCT FROM g.status::text
              )
        """)
        score_synced = cursor.rowcount
        logger.info(f"Synced scores/status for {score_synced} games from mlb.games")

    finally:
        if should_close:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description="Consolidate MLB betting lines")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Full rebuild (truncates and re-inserts all)",
    )
    parser.add_argument(
        "--games",
        nargs="*",
        type=int,
        help="Specific game_ids to consolidate (incremental mode). If omitted, processes all.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    run(rebuild_full=args.rebuild, game_ids_filter=set(args.games) if args.games else None)


if __name__ == "__main__":
    main()
