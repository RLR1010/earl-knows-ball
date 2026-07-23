#!/usr/bin/env python3
"""
Backfill historical NBA betting lines into nba.betting_lines_old.

Source: Kaggle "Basketball Betting Dataset" by visualize25
  (BettingOdds_History table from basketball-final.sqlite)

Has opening AND closing spreads and over/under.

Usage:
    python -m backend.app.ingestion.backfill_nba_lines_old
    python -m backend.app.ingestion.backfill_nba_lines_old --dry-run
"""

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

load_dotenv()

logger = logging.getLogger("backfill_nba_lines_old")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)

KAGGLE_CACHE = os.path.expanduser("~/.cache/kagglehub/datasets/visualize25/basketball-betting-dataset/versions/2/basketball-final.sqlite")

# NBA team abbreviation mapping: Kaggle dataset → our DB
TEAM_ABBREV_MAP = {
    "ATL": "ATL", "BKN": "BKN", "BOS": "BOS", "CHA": "CHA",
    "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN",
    "DET": "DET", "GSW": "GSW", "HOU": "HOU", "IND": "IND",
    "LAC": "LAC", "LAL": "LAL", "MEM": "MEM", "MIA": "MIA",
    "MIL": "MIL", "MIN": "MIN", "NOP": "NOP", "NYK": "NYK",
    "OKC": "OKC", "ORL": "ORL", "PHI": "PHI", "PHX": "PHX",
    "POR": "POR", "SAC": "SAC", "SAS": "SAS", "TOR": "TOR",
    "UTA": "UTA", "WAS": "WAS",
    # Historical team names
    "NJN": "BKN",  # New Jersey Nets → Brooklyn
    "NOH": "NOP",  # New Orleans Hornets → Pelicans
    "NOK": "NOP",  # New Orleans/Oklahoma City Hornets
    "SEA": "OKC",  # Seattle Supersonics → OKC
    "CHH": "CHA",  # Charlotte Hornets → Charlotte (new Hornets)
    "VAN": "MEM",  # Vancouver Grizzlies → Memphis
}


def _implied_probability(american_odds):
    """Convert American odds to implied probability (0-100)."""
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


async def _prep_lookups(db) -> dict:
    """Build team_id → abbreviation map."""
    r = await db.execute(sql_text("SELECT id, abbreviation FROM nba.teams"))
    return {row.abbreviation.upper(): row.id for row in r.fetchall()}


async def _match_game(db, team_map: dict, home_team: str, away_team: str, game_date_str: str) -> int | None:
    """Find game in our DB by teams and date."""
    date_part = game_date_str.split(" ")[0] if " " in game_date_str else game_date_str
    try:
        parsed_date = date.fromisoformat(date_part)
    except ValueError:
        return None
    home_abbr = TEAM_ABBREV_MAP.get(home_team.upper())
    away_abbr = TEAM_ABBREV_MAP.get(away_team.upper())
    if not home_abbr or not away_abbr:
        return None

    home_id = team_map.get(home_abbr)
    away_id = team_map.get(away_abbr)
    if not home_id or not away_id:
        return None

    r = await db.execute(
        sql_text(
            "SELECT id FROM nba.games "
            "WHERE home_team_id = :ht AND away_team_id = :at "
            "AND DATE(date) = :gdate LIMIT 1"
        ),
        {"ht": home_id, "at": away_id, "gdate": parsed_date},
    )
    row = r.fetchone()
    return row[0] if row else None


async def backfill(start_year: int, end_year: int, dry_run: bool = False):
    engine = create_async_engine(DATABASE_URL, echo=False, pool_size=5)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        r = await db.execute(sql_text("SELECT id, year FROM nba.seasons"))
        season_map = {row.year: row.id for row in r.fetchall()}
        team_map = await _prep_lookups(db)

        # Check existing in old table
        r = await db.execute(sql_text("SELECT game_id, is_opening FROM nba.betting_lines_old"))
        existing = {(row.game_id, row.is_opening) for row in r.fetchall()}

    logger.info(f"NBA {start_year}-{end_year}: {len(season_map)} seasons, {len(team_map)} teams, {len(existing)} existing rows")

    # ── Load Kaggle SQLite ──
    if not os.path.exists(KAGGLE_CACHE):
        logger.error(f"Kaggle dataset not found at {KAGGLE_CACHE}")
        logger.error("Run: kagglehub.dataset_download('visualize25/basketball-betting-dataset')")
        sys.exit(1)

    conn = sqlite3.connect(KAGGLE_CACHE)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT GAME_ID, Date, HomeTeam, AwayTeam,
               HomeSpread_AtOpen, HomeSpread_AtClose,
               Over_AtOpen, Over_AtClose,
               HomeML, AwayML
        FROM BettingOdds_History
        WHERE CAST(STRFTIME('%Y', Date) AS INTEGER) BETWEEN ? AND ?
        ORDER BY Date
    """, (start_year, end_year))

    rows = cursor.fetchall()
    conn.close()
    logger.info(f"Loaded {len(rows)} Kaggle rows for {start_year}-{end_year}")

    counts = {"loaded": 0, "no_match": 0, "duplicate": 0, "no_odds": 0, "error": 0}
    batch = []

    for row in rows:
        try:
            game_id_ext, game_date_str, home_str, away_str, \
                spr_open, spr_close, ou_open, ou_close, home_ml_str, away_ml_str = row

            # Parse moneyline
            home_ml = int(home_ml_str) if home_ml_str and str(home_ml_str).strip() not in ("", "None", "null") else None
            away_ml = int(away_ml_str) if away_ml_str and str(away_ml_str).strip() not in ("", "None", "null") else None

            # Skip if no odds at all
            if spr_open is None and spr_close is None and ou_open is None and ou_close is None and home_ml is None and away_ml is None:
                counts["no_odds"] += 1
                continue

            async with Session() as db:
                gid = await _match_game(db, team_map, home_str, away_str, game_date_str)

            if not gid:
                counts["no_match"] += 1
                continue

            home_prob = _implied_probability(home_ml)
            away_prob = _implied_probability(away_ml)

            # Two records per game: opening and closing
            for is_opening, spr, ou in [("t", spr_open, ou_open), ("f", spr_close, ou_close)]:
                if spr is None and ou is None and home_ml is None and away_ml is None:
                    continue

                key = (gid, is_opening)
                if key in existing:
                    counts["duplicate"] += 1
                    continue

                batch.append({
                    "game_id": gid,
                    "sportsbook": None,
                    "spread": spr,  # already home team spread
                    "spread_home_odds": None,
                    "spread_away_odds": None,
                    "over_under": ou,
                    "over_odds": None,
                    "under_odds": None,
                    "home_moneyline": home_ml,
                    "away_moneyline": away_ml,
                    "home_implied_probability": home_prob,
                    "away_implied_probability": away_prob,
                    "is_opening": is_opening,
                    "recorded_at": datetime.now(timezone.utc),
                })
                counts["loaded"] += 1
                existing.add(key)

            if len(batch) >= 200:
                if not dry_run:
                    async with Session() as db:
                        await db.execute(
                            sql_text(
                                "INSERT INTO nba.betting_lines_old "
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
            try:
                game_debug = f"game_id={row[0]}" if row else "unknown"
            except:
                game_debug = "unknown"
            logger.warning(f"Row error {game_debug}: {e}")
            counts["error"] += 1

    # Flush final batch
    if batch and not dry_run:
        async with Session() as db:
            await db.execute(
                sql_text(
                    "INSERT INTO nba.betting_lines_old "
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
        description="Backfill historical NBA lines into nba.betting_lines_old from Kaggle"
    )
    parser.add_argument("--start", type=int, default=2007, help="Start season")
    parser.add_argument("--end", type=int, default=2020, help="End season (default: 2020)")
    parser.add_argument("--dry-run", action="store_true", help="Don't insert")
    args = parser.parse_args()

    asyncio.run(backfill(args.start, args.end, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
