#!/usr/bin/env python3
"""
Backfill historical NFL betting lines into nfl.betting_lines_old.

Source: nflverse games.csv (same as betting_lines.py::ingest_historical_lines)

Usage:
    python -m backend.app.ingestion.backfill_lines_old
    python -m backend.app.ingestion.backfill_lines_old --start 2020 --end 2020
    python -m backend.app.ingestion.backfill_lines_old --dry-run
"""

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import datetime, timezone
from io import StringIO

import httpx
from dotenv import load_dotenv
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

load_dotenv()

logger = logging.getLogger("backfill_lines_old")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)

NFLAYER_GAMES_URL = "https://github.com/nflverse/nfldata/raw/master/data/games.csv"

TABLE = "nfl.betting_lines_old"
SOURCE_NAME = "nflverse"

TEAM_ABBREV_MAP = {
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BUF": "BUF",
    "CAR": "CAR", "CHI": "CHI", "CIN": "CIN", "CLE": "CLE",
    "DAL": "DAL", "DEN": "DEN", "DET": "DET", "GB": "GB",
    "HOU": "HOU", "IND": "IND", "JAX": "JAX", "JAC": "JAX",
    "KC": "KC", "LAC": "LAC", "LAR": "LAR", "LA": "LAR",
    "LV": "LV", "OAK": "LV", "MIA": "MIA", "MIN": "MIN",
    "NE": "NE", "NO": "NO", "NYG": "NYG", "NYJ": "NYJ",
    "PHI": "PHI", "PIT": "PIT", "SEA": "SEA", "SF": "SF",
    "TB": "TB", "TEN": "TEN", "WAS": "WAS", "WSH": "WAS",
    "STL": "LAR", "SD": "LAC",
}


def _map_abbrev(abbr: str) -> str | None:
    return TEAM_ABBREV_MAP.get(abbr.upper().strip())


def _safe_float(val) -> float | None:
    if val is None or str(val).strip() == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    if val is None or str(val).strip() == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _implied_probability(american_odds: int) -> float | None:
    if american_odds is None:
        return None
    if american_odds > 0:
        return round(100 / (american_odds / 100 + 1) * 100, 2)
    else:
        return round(abs(american_odds) / (abs(american_odds) + 100) * 100, 2)


async def _download_games_csv() -> list[dict]:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(NFLAYER_GAMES_URL)
        resp.raise_for_status()
        reader = csv.DictReader(StringIO(resp.text))
        return list(reader)


INSERT_SQL = """
    INSERT INTO nfl.betting_lines_old
        (game_id, source, sportsbook, spread, spread_home_odds, spread_away_odds,
         over_under, over_odds, under_odds, home_moneyline, away_moneyline,
         home_implied_probability, away_implied_probability, recorded_at)
    VALUES
        (:game_id, :source, :sportsbook, :spread, :spread_home_odds, :spread_away_odds,
         :over_under, :over_odds, :under_odds, :home_moneyline, :away_moneyline,
         :home_implied_probability, :away_implied_probability, :recorded_at)
    ON CONFLICT (game_id, source) DO NOTHING
"""


async def _match_game(db, season_id: int, week: int, home_team_id: int, away_team_id: int) -> int | None:
    r = await db.execute(
        sql_text(
            "SELECT id FROM nfl.games "
            "WHERE season_id = :sid AND week = :wk "
            "AND home_team_id = :ht AND away_team_id = :at "
            "LIMIT 1"
        ),
        {"sid": season_id, "wk": week, "ht": home_team_id, "at": away_team_id},
    )
    row = r.fetchone()
    return row[0] if row else None


async def _flush_batch(db_maker, batch: list[dict], dry_run: bool, existing_ids: set[int]) -> None:
    if not batch or dry_run:
        return
    async with db_maker() as db:
        await db.execute(sql_text(INSERT_SQL), batch)
        await db.commit()
    for b in batch:
        existing_ids.add(b["game_id"])


async def backfill(start_year: int, end_year: int, dry_run: bool = False):
    engine = create_async_engine(DATABASE_URL, echo=False, pool_size=5)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        r = await db.execute(sql_text("SELECT id, year FROM nfl.seasons"))
        season_map = {row.year: row.id for row in r.fetchall()}

        r = await db.execute(sql_text("SELECT id, abbreviation FROM nfl.teams"))
        team_map = {row.abbreviation.upper(): row.id for row in r.fetchall()}

        r = await db.execute(
            sql_text(f"SELECT game_id FROM {TABLE} WHERE source = :src"),
            {"src": SOURCE_NAME},
        )
        existing_game_ids = {row.game_id for row in r.fetchall()}

    logger.info(f"Range: {start_year}-{end_year} | {len(season_map)} seasons, {len(team_map)} teams")
    logger.info(f"Existing in {TABLE}: {len(existing_game_ids)} games from nflverse")

    # Download nflverse data
    logger.info("Downloading nflverse games.csv...")
    rows = await _download_games_csv()
    logger.info(f"Downloaded {len(rows)} game records")

    # Filter to requested years
    filtered = [r for r in rows if r.get("season") and start_year <= int(r["season"]) <= end_year]
    logger.info(f"Filtered to {len(filtered)} games in {start_year}-{end_year}")

    counts = {"loaded": 0, "no_match": 0, "no_lines": 0, "duplicate": 0, "error": 0}
    batch = []

    for row in filtered:
        try:
            season_year = int(row["season"])
            week = int(row["week"])
            nfl_away = row.get("away_team", "").strip()
            nfl_home = row.get("home_team", "").strip()

            # Check line data exists
            spread = _safe_float(row.get("spread_line"))
            total = _safe_float(row.get("total_line"))
            home_ml = _safe_int(row.get("home_moneyline"))
            away_ml = _safe_int(row.get("away_moneyline"))
            home_spread_odds = _safe_int(row.get("home_spread_odds"))
            away_spread_odds = _safe_int(row.get("away_spread_odds"))
            over_odds = _safe_int(row.get("over_odds"))
            under_odds = _safe_int(row.get("under_odds"))

            if spread is None and total is None and home_ml is None and away_ml is None:
                counts["no_lines"] += 1
                continue

            away_abbr = _map_abbrev(nfl_away)
            home_abbr = _map_abbrev(nfl_home)
            if not away_abbr or not home_abbr:
                counts["no_match"] += 1
                continue

            season_id = season_map.get(season_year)
            home_team_id = team_map.get(home_abbr)
            away_team_id = team_map.get(away_abbr)
            if not season_id or not home_team_id or not away_team_id:
                counts["no_match"] += 1
                continue

            # Match game
            async with Session() as db:
                game_id = await _match_game(db, season_id, week, home_team_id, away_team_id)

            if not game_id:
                counts["no_match"] += 1
                continue

            # Dedup
            if game_id in existing_game_ids:
                counts["duplicate"] += 1
                continue

            home_prob = _implied_probability(home_ml) if home_ml else None
            away_prob = _implied_probability(away_ml) if away_ml else None

            batch.append({
                "game_id": game_id,
                "source": SOURCE_NAME,
                "sportsbook": None,
                "spread": spread,
                "spread_home_odds": home_spread_odds,
                "spread_away_odds": away_spread_odds,
                "over_under": total,
                "over_odds": over_odds,
                "under_odds": under_odds,
                "home_moneyline": home_ml,
                "away_moneyline": away_ml,
                "home_implied_probability": home_prob,
                "away_implied_probability": away_prob,
                "recorded_at": datetime.now(timezone.utc),
            })
            counts["loaded"] += 1

            if len(batch) >= 200:
                await _flush_batch(Session, batch, dry_run, existing_game_ids)
                logger.info(f"  {counts['loaded']} loaded...")
                batch = []

        except Exception as e:
            logger.warning(f"Row error ({row.get('season', '?')} wk{row.get('week', '?')}): {e}")
            counts["error"] += 1

    # Flush final batch
    await _flush_batch(Session, batch, dry_run, existing_game_ids)

    logger.info(
        f"Done: {counts['loaded']} loaded, {counts['no_match']} no match, "
        f"{counts['no_lines']} no lines, {counts['duplicate']} duplicate, {counts['error']} errors"
    )

    await engine.dispose()


def main():
    parser = argparse.ArgumentParser(
        description="Backfill historical NFL lines into nfl.betting_lines_old from nflverse"
    )
    parser.add_argument("--start", type=int, default=2005, help="Start season (default: 2005)")
    parser.add_argument("--end", type=int, default=2020, help="End season (default: 2020)")
    parser.add_argument("--dry-run", action="store_true", help="Don't insert")
    args = parser.parse_args()

    asyncio.run(backfill(args.start, args.end, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
