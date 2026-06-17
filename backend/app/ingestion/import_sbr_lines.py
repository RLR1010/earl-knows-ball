"""Import SBR opening/closing line data into the DB betting_lines table."""
import asyncio
import json
import logging
import os
import re
from datetime import datetime

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("import_sbr")

# ── SBR team name → DB abbreviation ────────────────────────────────
SBR_TO_ABBR = {
    'Cardinals': 'ARI', 'Falcons': 'ATL', 'Ravens': 'BAL', 'Bills': 'BUF',
    'Panthers': 'CAR', 'Bears': 'CHI', 'Bengals': 'CIN', 'Browns': 'CLE',
    'Cowboys': 'DAL', 'Broncos': 'DEN', 'Lions': 'DET', 'Packers': 'GB',
    'Texans': 'HOU', 'Colts': 'IND', 'Jaguars': 'JAX', 'Chiefs': 'KC',
    'Chargers': 'LAC', 'Rams': 'LAR', 'Raiders': 'LV', 'Dolphins': 'MIA',
    'Vikings': 'MIN', 'Patriots': 'NE', 'Saints': 'NO', 'Giants': 'NYG',
    'Jets': 'NYJ', 'Eagles': 'PHI', 'Steelers': 'PIT', 'Seahawks': 'SEA',
    '49ers': 'SF', 'Fortyniners': 'SF', 'Buccaneers': 'TB', 'Titans': 'TEN',
    'Commanders': 'WAS', 'Redskins': 'WAS',
    # Legacy/mapped names
    'Kansas': 'KC', 'LosAngeles': 'LAR', 'NewYork': 'NYG',
    'Oakland': 'LV', 'SanDiego': 'LAC', 'St.Louis': 'LAR',
    'Tampa': 'TB', 'Washingtom': 'WAS', 'BuffaloBills': 'BUF',
    'KCChiefs': 'KC', 'LVRaiders': 'LV',
}

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football"
)
SBR_PATH = os.environ.get(
    "SBR_DATA_PATH",
    "/tmp/nfl_archive_10Y.json"
)


async def load_sbr_data():
    with open(SBR_PATH) as f:
        data = json.load(f)
    logger.info(f"Loaded {len(data)} SBR games")
    return data


def parse_date(d):
    """Convert SBR date float (YYYYMMDD.0) to (year, month, day)."""
    s = str(int(d))
    return int(s[:4]), int(s[4:6]), int(s[6:8])


def abbr(name):
    return SBR_TO_ABBR.get(str(name).strip())


async def import_lines():
    engine = create_async_engine(DB_URL)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    data = await load_sbr_data()

    async with Session() as db:
        stats = {"processed": 0, "mapped": 0, "inserted": 0, "skipped": 0,
                 "no_match": 0, "bad_data": 0, "errors": 0}

        # Preload games by (year, home_abbr, away_abbr, date)
        from sqlalchemy import text
        rows = await db.execute(text("""
            SELECT g.id, s.year, g.date::date as game_date,
                   ht.abbreviation as home_abbr, at.abbreviation as away_abbr
            FROM games g
            JOIN seasons s ON s.id = g.season_id
            JOIN teams ht ON ht.id = g.home_team_id
            JOIN teams at ON at.id = g.away_team_id
            WHERE g.game_type = 'REG'
        """))
        game_map = {}
        for r in rows:
            key = (r.year, r.home_abbr, r.away_abbr, str(r.game_date))
            game_map[key] = r.id

        for g in data:
            stats["processed"] += 1
            try:
                yr, mo, day = parse_date(g["date"])
                home = abbr(g["home_team"])
                away = abbr(g["away_team"])
                if not home or not away:
                    stats["no_match"] += 1
                    continue

                # Build date from SBR float
                date_str = f"{yr:04d}-{mo:02d}-{day:02d}"

                # Try exact match first, then try +-1 day
                game_id = game_map.get((yr, home, away, date_str))
                if not game_id:
                    # Try day before
                    from datetime import date, timedelta
                    dt = date(yr, mo, day)
                    for delta in [timedelta(days=-1), timedelta(days=1)]:
                        alt = (dt + delta).isoformat()
                        game_id = game_map.get((yr, home, away, alt))
                        if game_id:
                            break

                if not game_id:
                    stats["no_match"] += 1
                    if stats["no_match"] <= 5:
                        logger.warning(f"No DB match: {yr} {away} @ {home} on {date_str}")
                    continue

                stats["mapped"] += 1

                # Get opening spread
                os_str = str(g.get("home_open_spread", ""))
                cs_str = str(g.get("home_close_spread", ""))
                if not os_str.strip() or not cs_str.strip():
                    stats["bad_data"] += 1
                    continue

                os_val = float(os_str)
                cs_val = float(cs_str)

                # Filter out data errors (>10pt movement)
                if abs(cs_val - os_val) > 10:
                    stats["bad_data"] += 1
                    continue

                # Get O/U
                ou_str = str(g.get("open_over_under", ""))
                ou = float(ou_str) if ou_str.strip() else None

                # Moneyline
                home_ml = g.get("home_close_ml")
                away_ml = g.get("away_close_ml")

                # Check if already inserted for this game
                existing = await db.execute(
                    select(text("1 from betting_lines"))
                    .where(text(f"game_id={game_id} AND source='sbr_historical'"))
                    .limit(1)
                )
                if existing.scalar():
                    stats["skipped"] += 1
                    continue

                # Insert opening line
                from app.models import BettingLine
                opening = BettingLine(
                    game_id=game_id,
                    source="sbr_historical",
                    spread=os_val,
                    over_under=ou,
                )
                db.add(opening)

                # Insert closing line
                closing = BettingLine(
                    game_id=game_id,
                    source="sbr_historical",
                    spread=cs_val,
                    over_under=ou,
                    home_moneyline=home_ml,
                    away_moneyline=away_ml,
                )
                db.add(closing)
                stats["inserted"] += 2

                if stats["inserted"] % 1000 == 0:
                    await db.commit()
                    logger.info(f"  {stats['inserted']} lines inserted so far...")

            except Exception as e:
                logger.error(f"Error processing game {g}: {e}")
                stats["errors"] += 1

        await db.commit()
        logger.info(f"\nDone! Stats: {stats}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(import_lines())

# Monkey-patch the import to use separate sources for open/close
# Replace the insertion section
