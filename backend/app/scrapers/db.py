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


def _resolve_player_team_id(conn, sport: str, player_name: str) -> Optional[int]:
    """Resolve a player's team_id from {sport}.players by name.

    Used for award/season props where we only have the player name (BetMGM
    award results carry no team). Matching is case- and accent-insensitive, strips
    punctuation, and tolerates Jr./Sr./II/III/IV/V suffixes on either side so
    'Will Anderson' matches 'Will Anderson Jr.' and vice-versa.
    """
    if not player_name:
        return None
    key = _player_name_key(player_name)
    if not key:
        return None
    key_clause = (
        "regexp_replace(regexp_replace("
        "lower(translate(replace(name,' ',''),"
        "'áéíóúàèìòùäëïöüñÁÉÍÓÚÀÈÌÒÙÄËÏÖÜÑ','aeiouaeiouaeiounaeiouaeiouaeioun')),"
        "'[^a-z0-9]','','g'),"
        "'(jr|sr|iii|iv|ii)$','')"
        " = :k"
    )
    # Prefer a row that already carries a team_id (roster loader populates these;
    # legacy rows may be stale duplicates with NULL team).
    row = conn.execute(
        text(f"SELECT team_id FROM {sport}.players WHERE {key_clause} "
             "AND team_id IS NOT NULL ORDER BY team_id LIMIT 1"),
        {"k": key},
    ).fetchone()
    if row and row[0]:
        return row[0]
    return None


def _player_name_key(name: str) -> str:
    """Normalize a player name for team_id matching: lowercase, accent-fold,
    strip punctuation, and drop Jr./Sr./II/III/IV/V suffix tokens."""
    import re as _re
    import unicodedata as _ud
    n = _ud.normalize("NFD", str(name))
    n = "".join(c for c in n if not _ud.combining(c)).lower()
    n = _re.sub(r"[^a-z0-9]", "", n)
    for suf in ("iii", "iv", "ii", "jr", "sr"):
        if n.endswith(suf):
            return n[:-len(suf)]
    return n


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
            # Award rows often only have the player name (BetMGM); resolve the
            # team from the players table so team_id is populated.
            if team_id is None and getattr(prop, "player_name", None):
                team_id = _resolve_player_team_id(conn, prop.sport, prop.player_name)
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
