"""
Write scraped data to the database.

Handles name-to-ID resolution, upserts, and bulk inserts.
Uses the sync engine (psycopg2) — the scraper runs standalone, not async.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, text

from app.scrapers.models import TeamProp, PlayerSeasonProp, PlayerDailyProp

logger = logging.getLogger("earl.scrapers.db")

# Schema → table name mapping per sport
TEAM_PROPS_TABLE = "team_props"
SEASON_PROPS_TABLE = "player_season_props"
DAILY_PROPS_TABLE = "player_daily_props"


def _resolve_team_id(
    conn, sport: str, team_name: str
) -> Optional[int]:
    """Look up a team's internal ID by name."""
    if not team_name:
        return None
    result = conn.execute(
        text(
            f"SELECT id FROM {sport}.teams "
            "WHERE name ILIKE :name OR abbreviation ILIKE :name"
        ),
        {"name": team_name},
    ).fetchone()
    if result:
        return result[0]
    logger.warning(f"Could not resolve team '{team_name}' in {sport}.teams")
    return None


def _current_season(conn, sport: str) -> int:
    """Get the current season year for a sport."""
    result = conn.execute(
        text(
            f"SELECT year FROM {sport}.seasons "
            "ORDER BY year DESC LIMIT 1"
        )
    ).fetchone()
    if result:
        return result[0]
    # Fallback: current year if no seasons table or no data
    logger.warning(f"No seasons found for {sport}, using current year")
    return datetime.utcnow().year


def save_team_props(engine, props: list[TeamProp]) -> int:
    """Upsert team props. Returns count of rows written."""
    count = 0
    with engine.begin() as conn:
        season_year = _current_season(conn, props[0].sport) if props else 0

        for prop in props:
            team_id = _resolve_team_id(conn, prop.sport, prop.team_name)
            table = f"{prop.sport}.{TEAM_PROPS_TABLE}"

            conn.execute(
                text(
                    f"""
                INSERT INTO {table}
                    (season_year, team_id, bookmaker,
                     championship_odds, make_playoffs_odds, miss_playoffs_odds,
                     win_total, win_total_over_odds, win_total_under_odds,
                     scraped_at)
                VALUES
                    (:season_year, :team_id, :bookmaker,
                     :championship_odds, :make_playoffs_odds, :miss_playoffs_odds,
                     :win_total, :win_total_over_odds, :win_total_under_odds,
                     :scraped_at)
                ON CONFLICT (season_year, team_id, bookmaker)
                DO UPDATE SET
                    championship_odds = EXCLUDED.championship_odds,
                    make_playoffs_odds = EXCLUDED.make_playoffs_odds,
                    miss_playoffs_odds = EXCLUDED.miss_playoffs_odds,
                    win_total = EXCLUDED.win_total,
                    win_total_over_odds = EXCLUDED.win_total_over_odds,
                    win_total_under_odds = EXCLUDED.win_total_under_odds,
                    scraped_at = EXCLUDED.scraped_at
                """
                ),
                {
                    "season_year": prop.season_year or season_year,
                    "team_id": team_id,
                    "bookmaker": prop.bookmaker,
                    "championship_odds": prop.championship_odds,
                    "make_playoffs_odds": prop.make_playoffs_odds,
                    "miss_playoffs_odds": prop.miss_playoffs_odds,
                    "win_total": (
                        float(prop.win_total) if prop.win_total else None
                    ),
                    "win_total_over_odds": prop.win_total_over_odds,
                    "win_total_under_odds": prop.win_total_under_odds,
                    "scraped_at": prop.scraped_at,
                },
            )
            count += 1

    logger.info(f"Saved {count} team props")
    return count


def save_player_season_props(engine, props: list[PlayerSeasonProp]) -> int:
    """Upsert player season props (award odds)."""
    count = 0
    with engine.begin() as conn:
        season_year = _current_season(conn, props[0].sport) if props else 0

        for prop in props:
            team_id = (
                _resolve_team_id(conn, prop.sport, prop.team_name)
                if prop.team_name
                else None
            )
            table = f"{prop.sport}.{SEASON_PROPS_TABLE}"

            conn.execute(
                text(
                    f"""
                INSERT INTO {table}
                    (season_year, player_name, team_id, prop_type, bookmaker,
                     odds, implied_probability, scraped_at)
                VALUES
                    (:season_year, :player_name, :team_id, :prop_type, :bookmaker,
                     :odds, :implied_probability, :scraped_at)
                ON CONFLICT (season_year, player_name, prop_type, bookmaker)
                DO UPDATE SET
                    odds = EXCLUDED.odds,
                    team_id = COALESCE(EXCLUDED.team_id, {table}.team_id),
                    implied_probability = EXCLUDED.implied_probability,
                    scraped_at = EXCLUDED.scraped_at
                """
                ),
                {
                    "season_year": prop.season_year or season_year,
                    "player_name": prop.player_name,
                    "team_id": team_id,
                    "prop_type": prop.prop_type,
                    "bookmaker": prop.bookmaker,
                    "odds": prop.odds,
                    "implied_probability": _american_to_implied(prop.odds),
                    "scraped_at": prop.scraped_at,
                },
            )
            count += 1

    logger.info(f"Saved {count} player season props")
    return count


def save_player_daily_props(engine, props: list[PlayerDailyProp]) -> int:
    """Upsert player daily props (game props)."""
    count = 0
    with engine.begin() as conn:
        for prop in props:
            team_id = (
                _resolve_team_id(conn, prop.sport, prop.team_name)
                if prop.team_name
                else None
            )
            table = f"{prop.sport}.{DAILY_PROPS_TABLE}"

            conn.execute(
                text(
                    f"""
                INSERT INTO {table}
                    (game_id, player_name, team_id, prop_type, bookmaker,
                     line, odds, direction, scraped_at)
                VALUES
                    (:game_id, :player_name, :team_id, :prop_type, :bookmaker,
                     :line, :odds, :direction, :scraped_at)
                ON CONFLICT (game_id, player_name, prop_type, direction, line, bookmaker)
                DO UPDATE SET
                    odds = EXCLUDED.odds,
                    team_id = COALESCE(EXCLUDED.team_id, {table}.team_id),
                    scraped_at = EXCLUDED.scraped_at
                """
                ),
                {
                    "game_id": prop.game_id,
                    "player_name": prop.player_name,
                    "team_id": team_id,
                    "prop_type": prop.prop_type,
                    "bookmaker": prop.bookmaker,
                    "line": float(prop.line),
                    "odds": prop.odds,
                    "direction": prop.direction,
                    "scraped_at": prop.scraped_at,
                },
            )
            count += 1

    logger.info(f"Saved {count} player daily props")
    return count


def _american_to_implied(american_odds: int) -> Optional[float]:
    """Convert American odds to implied probability (0-1)."""
    if american_odds is None:
        return None
    if american_odds > 0:
        return round(100 / (american_odds + 100), 4)
    else:
        return round(abs(american_odds) / (abs(american_odds) + 100), 4)
