#!/usr/bin/env python3
"""
Backfill historical MLB betting lines into mlb.betting_lines_old.

Source: Kaggle "Major League Baseball Vegas Data" by christophertreasure
  (oddsDataMLB.csv)

One row per team per game (two rows per game). ML has opening lines only.

Usage:
    python -m backend.app.ingestion.backfill_mlb_lines_old
    python -m backend.app.ingestion.backfill_mlb_lines_old --dry-run
"""

import argparse
import asyncio
import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

load_dotenv()

logger = logging.getLogger("backfill_mlb_lines_old")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)

KAGGLE_VERSION = "versions/13"
KAGGLE_PATH = os.path.expanduser(
    f"~/.cache/kagglehub/datasets/christophertreasure/major-league-baseball-vegas-data/{KAGGLE_VERSION}/oddsDataMLB.csv"
)

# MLB team abbreviations — same as our DB
TEAM_ABBREV_MAP = {
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BOS": "BOS",
    "CHC": "CHC", "CIN": "CIN", "CLE": "CLE", "COL": "COL",
    "CWS": "CWS", "DET": "DET", "HOU": "HOU", "KC": "KC",
    "LAA": "LAA", "LAD": "LAD", "MIA": "MIA", "MIL": "MIL",
    "MIN": "MIN", "NYM": "NYM", "NYY": "NYY", "OAK": "OAK",
    "PHI": "PHI", "PIT": "PIT", "SD": "SD", "SEA": "SEA",
    "SF": "SF", "STL": "STL", "TB": "TB", "TEX": "TEX",
    "TOR": "TOR", "WSH": "WSH",
    # Historical
    "MON": "WSH",  # Montreal Expos → Nationals
    "FLA": "MIA",  # Florida Marlins → Miami
}
TABLE = "mlb.betting_lines_old"


def _implied_probability(american_odds):
    if american_odds is None:
        return None
    try:
        ao = int(american_odds)
    except (ValueError, TypeError):
        return None
    if ao > 0:
        return round(100 / (ao / 100 + 1) * 100, 2)
    else:
        return round(abs(ao) / (abs(ao) + 100) * 100, 2)


async def _build_game_lookup(db) -> dict:
    """Build (home_abbr, away_abbr, date) → game_id lookup."""
    r = await db.execute(
        sql_text("""
            SELECT g.id, ht.abbreviation AS home_abbr, at.abbreviation AS away_abbr, g.date::date AS gdate
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
        """)
    )
    lookup = {}
    for row in r:
        key = (row.home_abbr.upper(), row.away_abbr.upper(), row.gdate)
        lookup[key] = row.id
        # Also store the reverse
        reverse_key = (row.away_abbr.upper(), row.home_abbr.upper(), row.gdate)
        lookup[reverse_key] = row.id
    return lookup


async def backfill(start_year: int, end_year: int, dry_run: bool = False):
    engine = create_async_engine(DATABASE_URL, echo=False, pool_size=5)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        r = await db.execute(sql_text("SELECT id, year FROM mlb.seasons"))
        season_map = {row.year: row.id for row in r.fetchall()}

        r = await db.execute(sql_text("SELECT abbreviation FROM mlb.teams"))
        team_map = {row.abbreviation.upper(): True for row in r.fetchall()}

        game_lookup = await _build_game_lookup(db)

        # Existing in old table
        r = await db.execute(
            sql_text(f"SELECT game_id, is_opening FROM {TABLE}")
        )
        existing = {(row.game_id, row.is_opening) for row in r.fetchall()}

    logger.info(f"MLB {start_year}-{end_year}: {len(season_map)} seasons, {len(team_map)} teams")
    logger.info(f"Game lookup: {len(game_lookup)} entries, existing: {len(existing)} rows")

    # ── Load Kaggle CSV ──
    if not os.path.exists(KAGGLE_PATH):
        logger.error(f"Kaggle dataset not found at {KAGGLE_PATH}")
        logger.error("Run: kagglehub.dataset_download('christophertreasure/major-league-baseball-vegas-data')")
        sys.exit(1)

    df = pd.read_csv(KAGGLE_PATH)
    df = df[(df['season'] >= start_year) & (df['season'] <= end_year)]
    logger.info(f"Loaded {len(df)} Kaggle rows for {start_year}-{end_year}")

    # ── Group rows by game ──
    # Each game has two rows: one for each team.
    # Group by (date, game_hash) where game_hash is the sorted tuple of (team, opponent)
    game_groups = defaultdict(list)
    for _, row in df.iterrows():
        teams = sorted([str(row['team']).strip().upper(), str(row['opponent']).strip().upper()])
        key = (str(row['date']), teams[0], teams[1])
        game_groups[key].append(row)

    logger.info(f"Grouped into {len(game_groups)} games")

    counts = {"loaded": 0, "no_match": 0, "duplicate": 0, "no_odds": 0, "error": 0}
    batch = []

    for (date_str, team_a, team_b), rows in game_groups.items():
        try:
            if len(rows) < 2:
                counts["no_match"] += 1
                continue

            parsed_date = date.fromisoformat(date_str)

            # Try matching as (home, away, date) — home is first
            home_abbr = TEAM_ABBREV_MAP.get(team_a)
            away_abbr = TEAM_ABBREV_MAP.get(team_b)
            gid = game_lookup.get((home_abbr, away_abbr, parsed_date))

            if not gid:
                # Try reverse (team_b is home)
                home_abbr = TEAM_ABBREV_MAP.get(team_b)
                away_abbr = TEAM_ABBREV_MAP.get(team_a)
                gid = game_lookup.get((home_abbr, away_abbr, parsed_date))

            if not gid:
                counts["no_match"] += 1
                continue

            if not home_abbr or not away_abbr:
                counts["no_match"] += 1
                continue

            # Find which row belongs to home team and which to away
            home_row = None
            away_row = None
            for r in rows:
                team_str = str(r['team']).strip().upper()
                mapped = TEAM_ABBREV_MAP.get(team_str)
                if mapped == home_abbr:
                    home_row = r
                elif mapped == away_abbr:
                    away_row = r

            if home_row is None or away_row is None:
                counts["no_match"] += 1
                continue

            home_ml = int(home_row['moneyLine']) if pd.notna(home_row['moneyLine']) else None
            away_ml = int(away_row['moneyLine']) if pd.notna(away_row['moneyLine']) else None

            # Run line = spread in MLB
            spread = float(home_row['runLine']) if pd.notna(home_row['runLine']) else None
            run_line_odds = float(home_row['runLineOdds']) if pd.notna(home_row['runLineOdds']) else None
            opp_run_line_odds = float(away_row['runLineOdds']) if pd.notna(away_row['runLineOdds']) else None

            over_under = float(home_row['total']) if pd.notna(home_row['total']) else None
            over_odds = int(home_row['overOdds']) if pd.notna(home_row['overOdds']) else None
            under_odds = int(home_row['underOdds']) if pd.notna(home_row['underOdds']) else None

            if spread is None and over_under is None and home_ml is None and away_ml is None:
                counts["no_odds"] += 1
                continue

            # Convert runLineOdds to spread_odds format
            # If spread is negative (favorite has -1.5), runLineOdds is for backing that spread
            spread_home_odds = int(run_line_odds) if run_line_odds is not None else None
            spread_away_odds = int(opp_run_line_odds) if opp_run_line_odds is not None else None

            home_prob = _implied_probability(home_ml)
            away_prob = _implied_probability(away_ml)

            # MLB table uses BOOLEAN for is_opening. This dataset has opening lines only.
            is_opening = True
            key = (gid, is_opening)
            if key in existing:
                counts["duplicate"] += 1
                continue

            batch.append({
                "game_id": gid,
                "sportsbook": None,
                "spread": spread,
                "spread_home_odds": spread_home_odds,
                "spread_away_odds": spread_away_odds,
                "over_under": over_under,
                "over_odds": over_odds,
                "under_odds": under_odds,
                "home_moneyline": home_ml,
                "away_moneyline": away_ml,
                "home_implied_probability": home_prob,
                "away_implied_probability": away_prob,
                "is_opening": True,
                "recorded_at": datetime.now(timezone.utc),
            })
            counts["loaded"] += 1
            existing.add(key)

            if len(batch) >= 200:
                if not dry_run:
                    async with Session() as db:
                        await db.execute(
                            sql_text(
                                "INSERT INTO mlb.betting_lines_old "
                                "(game_id, sportsbook, spread, spread_home_odds, spread_away_odds, "
                                "over_under, over_odds, under_odds, home_moneyline, away_moneyline, "
                                "home_implied_probability, away_implied_probability, is_opening, recorded_at) "
                                "VALUES (:game_id, :sportsbook, :spread, :spread_home_odds, :spread_away_odds, "
                                ":over_under, :over_odds, :under_odds, :home_moneyline, :away_moneyline, "
                                ":home_implied_probability, :away_implied_probability, :is_opening, :recorded_at) "
                                "ON CONFLICT (game_id, is_opening) DO NOTHING"
                            ),
                            batch,
                        )
                        await db.commit()
                logger.info(f"  {counts['loaded']} lines loaded...")
                batch = []

        except Exception as e:
            logger.warning(f"Game error ({date_str} {team_a} {team_b}): {e}")
            counts["error"] += 1

    # Flush final batch
    if batch and not dry_run:
        async with Session() as db:
            await db.execute(
                sql_text(
                    "INSERT INTO mlb.betting_lines_old "
                    "(game_id, sportsbook, spread, spread_home_odds, spread_away_odds, "
                    "over_under, over_odds, under_odds, home_moneyline, away_moneyline, "
                    "home_implied_probability, away_implied_probability, is_opening, recorded_at) "
                    "VALUES (:game_id, :sportsbook, :spread, :spread_home_odds, :spread_away_odds, "
                    ":over_under, :over_odds, :under_odds, :home_moneyline, :away_moneyline, "
                    ":home_implied_probability, :away_implied_probability, :is_opening, :recorded_at) "
                    "ON CONFLICT (game_id, is_opening) DO NOTHING"
                ),
                batch,
            )
            await db.commit()

    logger.info(
        f"Done: {counts['loaded']} loaded, {counts['no_match']} no match, "
        f"{counts['no_odds']} no odds, {counts['duplicate']} duplicate, {counts['error']} errors"
    )

    await engine.dispose()


def main():
    parser = argparse.ArgumentParser(
        description="Backfill historical MLB lines into mlb.betting_lines_old from Kaggle"
    )
    parser.add_argument("--start", type=int, default=2012, help="Start season (default: 2012)")
    parser.add_argument("--end", type=int, default=2020, help="End season (default: 2020)")
    parser.add_argument("--dry-run", action="store_true", help="Don't insert")
    args = parser.parse_args()

    asyncio.run(backfill(args.start, args.end, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
