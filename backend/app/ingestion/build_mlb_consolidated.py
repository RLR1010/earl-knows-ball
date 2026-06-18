"""
Build mlb.betting_lines_consolidated — one row per game.
Prefers FanDuel data. Falls back to other sportsbooks only when FanDuel is missing.
Uses SQL DISTINCT ON with ordered priority so all heavy lifting is in the DB.
"""
import os, logging
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DB_URL = os.environ.get(
    "SYNC_DATABASE_URL",
    "postgresql+psycopg2://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)

# Ordered priority: first available wins
SPORTSBOOK_ORDER = (
    "fanduel, draftkings, betmgm, betrivers, caesars, "
    "williamhill_us, pointsbetus, fanatics, wynnbet, foxbet, sugarhouse, "
    "bovada, lowvig, betonlineag, betus, superbook, twinspires, barstool, "
    "unibet, unibet_us, gtbets, intertops, mybookieag, circasports, betfair"
)

PRIORITY_CASE = f"""CASE sportsbook
    WHEN 'fanduel' THEN 1
    WHEN 'draftkings' THEN 2
    WHEN 'betmgm' THEN 3
    WHEN 'betrivers' THEN 4
    WHEN 'caesars' THEN 5
    WHEN 'williamhill_us' THEN 6
    WHEN 'pointsbetus' THEN 7
    WHEN 'fanatics' THEN 8
    WHEN 'wynnbet' THEN 9
    WHEN 'foxbet' THEN 10
    WHEN 'sugarhouse' THEN 11
    WHEN 'bovada' THEN 12
    WHEN 'lowvig' THEN 13
    WHEN 'betonlineag' THEN 14
    WHEN 'betus' THEN 15
    WHEN 'superbook' THEN 16
    WHEN 'twinspires' THEN 17
    WHEN 'barstool' THEN 18
    WHEN 'unibet' THEN 19
    WHEN 'unibet_us' THEN 20
    WHEN 'gtbets' THEN 21
    WHEN 'intertops' THEN 22
    WHEN 'mybookieag' THEN 23
    WHEN 'circasports' THEN 24
    WHEN 'betfair' THEN 25
    ELSE 99
END"""


SQL_TEMPLATE = """
INSERT INTO mlb.betting_lines_consolidated (game_id, {col_name}, {col_name}_sportsbook)
SELECT DISTINCT ON (game_id) game_id, {col_expr} AS {col_name}, sportsbook AS {col_name}_sportsbook
FROM mlb.betting_lines
WHERE source = '{source}'
  AND {col_expr} IS NOT NULL
  {odds_filter}
ORDER BY game_id, {priority}, id
ON CONFLICT (game_id) DO UPDATE SET
    {col_name} = EXCLUDED.{col_name},
    {col_name}_sportsbook = EXCLUDED.{col_name}_sportsbook;
"""

# Columns that store American odds (integers): reject absurd values
# Reasonable MLB range: typically -5000 to +5000, anything beyond -10000 or +10000 is garbage
ODDS_COLUMNS = {"home_moneyline", "away_moneyline", "over_odds", "under_odds", "spread_home_odds", "spread_away_odds"}



COLUMNS = [
    # (col_name, col_expr, source)
    ("closing_spread",        "spread",              "the_odds_api_closing"),
    ("closing_ou",            "over_under",          "the_odds_api_closing"),
    ("closing_home_ml",       "home_moneyline",      "the_odds_api_closing"),
    ("closing_away_ml",       "away_moneyline",      "the_odds_api_closing"),
    ("closing_over_odds",     "over_odds",           "the_odds_api_closing"),
    ("closing_under_odds",    "under_odds",          "the_odds_api_closing"),
    ("closing_spread_home_odds", "spread_home_odds",  "the_odds_api_closing"),
    ("closing_spread_away_odds", "spread_away_odds",  "the_odds_api_closing"),
    ("opening_ou",            "over_under",          "the_odds_api_opening"),
    ("opening_spread",        "spread",              "the_odds_api_opening"),
    ("opening_home_ml",       "home_moneyline",      "the_odds_api_opening"),
    ("opening_away_ml",       "away_moneyline",      "the_odds_api_opening"),
    ("opening_over_odds",     "over_odds",           "the_odds_api_opening"),
    ("opening_under_odds",    "under_odds",          "the_odds_api_opening"),
    ("opening_spread_home_odds", "spread_home_odds",  "the_odds_api_opening"),
    ("opening_spread_away_odds", "spread_away_odds",  "the_odds_api_opening"),
]


def run():
    logger.info("Connecting to DB...")
    engine = create_engine(DB_URL)

    # ── Create table if it doesn't exist ──
    logger.info("Ensuring table exists...")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS mlb.betting_lines_consolidated (
                game_id INTEGER PRIMARY KEY,
                game_time TIMESTAMPTZ,
                home_team TEXT,
                away_team TEXT,
                year INTEGER,
                home_score INTEGER,
                away_score INTEGER,
                venue TEXT,
                status TEXT,
                closing_spread NUMERIC,
                closing_spread_sportsbook TEXT,
                closing_ou NUMERIC,
                closing_ou_sportsbook TEXT,
                closing_home_ml INTEGER,
                closing_home_ml_sportsbook TEXT,
                closing_away_ml INTEGER,
                closing_away_ml_sportsbook TEXT,
                opening_ou NUMERIC,
                opening_ou_sportsbook TEXT,
                opening_spread NUMERIC,
                opening_spread_sportsbook TEXT,
                opening_home_ml INTEGER,
                opening_home_ml_sportsbook TEXT,
                opening_away_ml INTEGER,
                opening_away_ml_sportsbook TEXT,
                has_verified_ou BOOLEAN
            );
        """))

    # ── Seed game info into consolidated table ──
    logger.info("Seeding game info...")
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO mlb.betting_lines_consolidated (game_id, game_time, home_team, away_team,
                year, home_score, away_score, venue, status)
            SELECT g.id, g.date, ht.abbreviation, at.abbreviation,
                   s.year, g.home_score, g.away_score, g.venue, g.status
            FROM mlb.games g
            JOIN mlb.seasons s ON s.id = g.season_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.date >= '2021-01-01'
              AND g.date < NOW()
            ORDER BY g.id
            ON CONFLICT (game_id) DO NOTHING
        """))
        r = conn.execute(text("SELECT COUNT(*) FROM mlb.betting_lines_consolidated")).scalar()
        logger.info(f"  {r} games seeded")

    # ── Fill each column with FanDuel-first priority ──
    for col_name, col_expr, source in COLUMNS:
        logger.info(f"  Filling {col_name}...")
        # Filter absurd American odds: reject values > +10000 or < -10000
        if col_expr in ODDS_COLUMNS:
            odds_filter = f"AND {col_expr} >= -10000 AND {col_expr} <= 10000"
        elif col_expr in ("spread",):
            odds_filter = f"AND ABS({col_expr}) <= 50"
        elif col_expr in ("over_under",):
            # Also filter: opening OU shouldn't differ from closing OU by more than 50%
            # But we do a simple absolute range check here
            odds_filter = f"AND {col_expr} >= 0 AND {col_expr} <= 25"
        else:
            odds_filter = ""
        sql = SQL_TEMPLATE.format(
            col_name=col_name,
            col_expr=col_expr,
            source=source,
            priority=PRIORITY_CASE,
            odds_filter=odds_filter,
        )
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
            logger.info(f"    ✅ {col_name} done")
        except Exception as e:
            logger.error(f"    ❌ {col_name} failed: {e}")

    # ── Set has_verified_ou ──
    logger.info("Setting has_verified_ou...")
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE mlb.betting_lines_consolidated
            SET has_verified_ou = (closing_ou IS NOT NULL)
        """))

    # ── Sanity: remove opening lines that differ wildly from closing lines (garbage data) ──
    # FanDuel and other sportsbooks sometimes return opening OU of 18 when the real OU is 8
    logger.info("Sanity-checking opening_ou values against closing_ou...")
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE mlb.betting_lines_consolidated
            SET opening_ou = NULL, opening_ou_sportsbook = NULL
            WHERE closing_ou IS NOT NULL
              AND opening_ou IS NOT NULL
              AND ABS(closing_ou - opening_ou) > 3.5
        """))
        logger.info(f"  Cleared {result.rowcount} garbage opening OU values")

    # ── Fallback: fill closing_* nulls from opening source, opening_* nulls from closing source ──
    logger.info("Filling fallback gaps (closing <=> opening cross-fill)...")
    for col_name, col_expr, primary_source in COLUMNS:
        # Determine the opposite source
        if "closing" in col_name:
            fallback_source = "the_odds_api_opening"
        elif "opening" in col_name:
            fallback_source = "the_odds_api_closing"
        else:
            continue

        if col_expr in ODDS_COLUMNS:
            odds_filter = f"AND bl.{col_expr} >= -10000 AND bl.{col_expr} <= 10000"
        elif col_expr == "spread":
            odds_filter = f"AND ABS(bl.{col_expr}) <= 50"
        elif col_expr == "over_under":
            odds_filter = f"AND bl.{col_expr} >= 0 AND bl.{col_expr} <= 25"
        else:
            odds_filter = ""

        fallback_sql = f"""
            UPDATE mlb.betting_lines_consolidated bc
            SET
                {col_name} = sub.{col_expr},
                {col_name}_sportsbook = sub.sportsbook
            FROM (
                SELECT DISTINCT ON (bl.game_id) bl.game_id, bl.{col_expr}, bl.sportsbook
                FROM mlb.betting_lines bl
                WHERE bl.source = '{fallback_source}'
                  AND bl.{col_expr} IS NOT NULL
                  {odds_filter}
                ORDER BY bl.game_id, {PRIORITY_CASE}, bl.id
            ) sub
            WHERE bc.game_id = sub.game_id
              AND bc.{col_name} IS NULL
        """
        with engine.begin() as conn:
            conn.execute(text(fallback_sql))

    # ── Implied probabilities from moneyline odds ──
    # American odds → implied probability:
    #   positive odds: 100 / (odds + 100)
    #   negative odds: |odds| / (|odds| + 100)
    logger.info("Computing implied probabilities...")
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE mlb.betting_lines_consolidated
            SET
                closing_home_implied_probability = ROUND(
                    CASE
                        WHEN closing_home_ml > 0 THEN 100.0 / (closing_home_ml + 100)
                        WHEN closing_home_ml < 0 THEN closing_home_ml::numeric * -1.0 / (closing_home_ml::numeric * -1.0 + 100)
                    END, 3
                ),
                closing_away_implied_probability = ROUND(
                    CASE
                        WHEN closing_away_ml > 0 THEN 100.0 / (closing_away_ml + 100)
                        WHEN closing_away_ml < 0 THEN closing_away_ml::numeric * -1.0 / (closing_away_ml::numeric * -1.0 + 100)
                    END, 3
                ),
                opening_home_implied_probability = ROUND(
                    CASE
                        WHEN opening_home_ml > 0 THEN 100.0 / (opening_home_ml + 100)
                        WHEN opening_home_ml < 0 THEN opening_home_ml::numeric * -1.0 / (opening_home_ml::numeric * -1.0 + 100)
                    END, 3
                ),
                opening_away_implied_probability = ROUND(
                    CASE
                        WHEN opening_away_ml > 0 THEN 100.0 / (opening_away_ml + 100)
                        WHEN opening_away_ml < 0 THEN opening_away_ml::numeric * -1.0 / (opening_away_ml::numeric * -1.0 + 100)
                    END, 3
                )
            WHERE closing_home_ml IS NOT NULL OR opening_home_ml IS NOT NULL
        """))

    # ── Indexes ──
    logger.info("Creating indexes...")
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_mlb_consolidated_year ON mlb.betting_lines_consolidated (year)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_mlb_consolidated_verified ON mlb.betting_lines_consolidated (has_verified_ou)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_mlb_consolidated_game_time ON mlb.betting_lines_consolidated (game_time)"))

    # ── Stats ──
        r = conn.execute(text("""
            SELECT COUNT(*) as total,
                   COUNT(closing_ou) as has_closing_ou,
                   COUNT(opening_ou) as has_opening_ou,
                   COUNT(closing_spread) as has_closing_spread,
                   COUNT(closing_over_odds) as has_over_odds,
                   COUNT(closing_spread_home_odds) as has_spread_home_odds,
                   COUNT(closing_home_implied_probability) as has_implied_prob
            FROM mlb.betting_lines_consolidated
        """)).fetchone()

    logger.info(f"✅ Done. {r[0]} consolidated rows. "
                f"Closing OU: {r[1]} | Opening OU: {r[2]} | Closing spread: {r[3]} | "
                f"Over odds: {r[4]} | Spread home odds: {r[5]} | Implied prob: {r[6]}")


if __name__ == "__main__":
    run()
