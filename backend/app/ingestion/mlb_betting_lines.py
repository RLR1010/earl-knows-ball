import os
import ssl
import httpx
import certifi
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.models.mlb import MLBBettingLine, MLBGames, MLBSeason, GameStatus
from app.models.mlb.team import MLBTeam

logger = logging.getLogger(__name__)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Map The Odds API team names to our abbreviations
MLB_TEAM_NAME_MAP = {
    "Arizona Diamondbacks": "ARI",
    "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",
    "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN",
    "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",
    "Detroit Tigers": "DET",
    "Houston Astros": "HOU",
    "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA",
    "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",
    "New York Mets": "NYM",
    "New York Yankees": "NYY",
    "Oakland Athletics": "OAK",
    "Athletics": "OAK",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD",
    "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA",
    "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}

# Map Odds API sportsbook keys to our normalized names
# These match the keys returned by the Odds API (not full names)
ODDS_API_SPORTSBOOK_MAP = {
    "fanduel": "fanduel",
    "draftkings": "draftkings",
    "betmgm": "betmgm",
    "betrivers": "betrivers",
    "williamhill_us": "williamhill_us",
    "bovada": "bovada",
    "lowvig": "lowvig",
    "pointsbetus": "pointsbetus",
    "barstool": "barstool",
    "fanatics": "fanatics",
    "foxbet": "foxbet",
    "wynnbet": "wynnbet",
    "sugarhouse": "sugarhouse",
    "twinspires": "twinspires",
    "unibet": "unibet",
    "caesars": "caesars",
    "betonlineag": "betonlineag",
    "betus": "betus",
    "superbook": "superbook",
    "mybookieag": "mybookieag",
    "intertops": "intertops",
    "circasports": "circasports",
    "betfair": "betfair",
    "gtbets": "gtbets",
}


def _extract_odds_from_markets(markets: list, home_team_name: str, away_team_name: str) -> dict:
    """Extract spread, OU, and moneylines from a list of market dicts."""
    result = {}
    for m in markets:
        m_key = m.get("key", "")
        outcomes = m.get("outcomes", [])
        if m_key == "h2h":
            for ent in outcomes:
                name = ent.get("name", "")
                price = ent.get("price")
                if name == home_team_name:
                    result["home_moneyline"] = price
                elif name == away_team_name:
                    result["away_moneyline"] = price
        elif m_key == "spreads":
            for ent in outcomes:
                name = ent.get("name", "")
                point = ent.get("point")
                price = ent.get("price")
                if name == home_team_name:
                    # The Odds API returns point from the team's perspective:
                    #   point < 0 = team is favored by |point|
                    #   point > 0 = team is underdog by point
                    # Our convention: negative = home favored, positive = home underdog.
                    # The home team's point value is our spread directly.
                    result["spread"] = point
                    result["spread_home_odds"] = price
                elif name == away_team_name:
                    result["spread_away_odds"] = price
        elif m_key == "totals":
            for ent in outcomes:
                point = ent.get("point")
                price = ent.get("price")
                name_lower = (ent.get("name", "") or "").lower()
                if name_lower == "over":
                    result["over_under"] = point
                    result["over_odds"] = price
                elif name_lower == "under":
                    result["under_odds"] = price
    return result


def _implied_prob(american_odds: int | float | None) -> float | None:
    """Convert American odds to implied probability (0-1)."""
    if american_odds is None:
        return None
    try:
        odds = float(american_odds)
        if odds > 0:
            return round(100 / (odds + 100), 4)
        else:
            return round(abs(odds) / (abs(odds) + 100), 4)
    except (ValueError, TypeError):
        return None


async def snapshot_mlb_opening_lines(
    db: AsyncSession,
    api_key: str = "",
    days_from_now: int = 3,
) -> dict:
    """Fetch current MLB odds from The Odds API and store per-sportsbook lines.

    Stores ALL sportsbooks returned by the API (not just the first),
    so the consolidation script can pick the best one. Deletes old
    ``the_odds_api_current`` rows for the same games before inserting fresh data.

    Returns:
        {"loaded": int, "updated_game_ids": list[int], "skipped": list[str]}
    """
    api_key = (api_key or app_settings.odds_api_key).strip()
    logger.info(f"snapshot_mlb: api_key={'<set>' if api_key else '<empty>'}, len={len(api_key)}, first_char='{api_key[:5] if api_key else 'n/a'}'")
    if not api_key:
        return {"error": "No API key", "loaded": 0, "updated_game_ids": [], "skipped": []}

    # Get current MLB season
    season_result = await db.execute(
        select(MLBSeason).where(MLBSeason.year == 2026)
    )
    season = season_result.scalar_one_or_none()
    if not season:
        return {"error": "2026 MLB season not found", "loaded": 0, "updated_game_ids": [], "skipped": []}

    odds_url = (
        f"{ODDS_API_BASE}/sports/baseball_mlb/odds"
        f"?apiKey={api_key}&regions=us"
        f"&markets=h2h,spreads,totals&oddsFormat=american"
    )

    ssl_context = ssl.create_default_context(cafile=certifi.where())

    async with httpx.AsyncClient(timeout=30.0, verify=ssl_context) as client:
        try:
            resp = await client.get(odds_url)
            if resp.status_code != 200:
                return {"error": f"API {resp.status_code}", "loaded": 0, "updated_game_ids": [], "skipped": []}
            data = resp.json()
        except Exception as e:
            return {"error": str(e), "loaded": 0, "updated_game_ids": [], "skipped": []}

    if not isinstance(data, list):
        return {"error": "Unexpected API response", "loaded": 0, "updated_game_ids": [], "skipped": []}

    now = datetime.now(timezone.utc)
    loaded = 0
    skipped = []
    updated_game_ids = set()

    for event in data:
        try:
            home_name = event.get("home_team", "")
            away_name = event.get("away_team", "")
            home_abbr = MLB_TEAM_NAME_MAP.get(home_name)
            away_abbr = MLB_TEAM_NAME_MAP.get(away_name)
            if not home_abbr or not away_abbr:
                skipped.append(f"Unknown teams: {home_name} @ {away_name}")
                continue

            game_time_str = event.get("commence_time") or event.get("commence", "")
            if not game_time_str:
                skipped.append(f"No time for {away_abbr} @ {home_abbr}")
                continue

            game_dt = datetime.fromisoformat(game_time_str.replace("Z", "+00:00"))

            # Skip games that have already started
            if game_dt <= now:
                skipped.append(f"{away_abbr} @ {home_abbr} already started")
                continue

            # Skip games more than 48 hours out — odds are unreliable placeholders
            if game_dt > now + timedelta(hours=48):
                skipped.append(f"{away_abbr} @ {home_abbr} more than 48h away ({game_dt})")
                continue

            # Look up teams
            home_result = await db.execute(
                select(MLBTeam).where(MLBTeam.abbreviation == home_abbr)
            )
            home_team = home_result.scalar_one_or_none()
            away_result = await db.execute(
                select(MLBTeam).where(MLBTeam.abbreviation == away_abbr)
            )
            away_team = away_result.scalar_one_or_none()
            if not home_team or not away_team:
                skipped.append(f"DB lookup failed: {away_abbr} @ {home_abbr}")
                continue

            # Find game matching home, away, and time (within 90-min window)
            time_lower = game_dt - timedelta(minutes=90)
            time_upper = game_dt + timedelta(minutes=90)
            game_result = await db.execute(
                select(MLBGames).where(
                    MLBGames.season_id == season.id,
                    MLBGames.home_team_id == home_team.id,
                    MLBGames.away_team_id == away_team.id,
                    MLBGames.date >= time_lower,
                    MLBGames.date <= time_upper,
                )
            )
            game = game_result.scalar_one_or_none()
            if not game:
                skipped.append(f"Game not found: {away_abbr} @ {home_abbr} ({game_dt})")
                continue

            gid = game.id

            # ── Only record betting lines for SCHEDULED games ──
            if game.status != GameStatus.SCHEDULED:
                skipped.append(f"Game {away_abbr} @ {home_abbr} status={game.status}, skipping")
                continue

            bookmakers = event.get("bookmakers", [])
            if not bookmakers:
                skipped.append(f"No bookmakers for {gid}")
                continue

            # ── Only update closing lines for future games ──
            game_started = game.date <= datetime.now(timezone.utc)

            # Track which opening rows already exist for this game
            existing_openings = set()
            open_rows = await db.execute(
                text(
                    "SELECT sportsbook FROM mlb.betting_lines "
                    "WHERE game_id = :gid AND is_opening = :opening"
                ),
                {"gid": gid, "opening": True},
            )
            for (sb,) in open_rows:
                existing_openings.add(sb)

            # ── Insert one row per sportsbook ──
            # Two rows per book: opening (is_opening=True) and closing (is_opening=False)
            # The API sometimes provides an "openings" sub-dict per market.
            any_saved = False
            # Only record FanDuel and DraftKings — reliable run line data, ~100% coverage.
            bookmakers = [b for b in bookmakers if b.get("key", "").lower() in {'fanduel', 'draftkings'}]
            for bookmaker in bookmakers:
                sb_key = bookmaker.get("key", "").lower().strip()
                sb_name = ODDS_API_SPORTSBOOK_MAP.get(sb_key, sb_key)

                markets = bookmaker.get("markets", [])

                # Current (closing) line — use current data
                closing = _extract_odds_from_markets(markets, home_name, away_name)

                if not closing.get("home_moneyline") or not closing.get("away_moneyline"):
                    continue  # skip if this book doesn't have h2h

                # ALL 8 fields must be present to record anything
                required_fields = ['spread', 'over_under', 'home_moneyline', 'away_moneyline',
                                   'spread_home_odds', 'spread_away_odds', 'over_odds', 'under_odds']
                if not all(closing.get(f) is not None for f in required_fields):
                    continue  # skip this book if any required field is missing

                if sb_name in existing_openings:
                    # ── Phase 2+: save/update closing row (subsequent cycles) ──
                    # Only update closing lines for future games
                    if not game_started:
                        # Delete old closing row first so the INSERT below replaces it
                        await db.execute(
                            text("DELETE FROM mlb.betting_lines "
                                 "WHERE game_id = :gid AND sportsbook = :sb AND is_opening = :opening"),
                            {"gid": gid, "sb": sb_name, "opening": False},
                        )
                        await db.execute(
                            text(
                                "INSERT INTO mlb.betting_lines "
                                "(game_id, sportsbook, is_opening, spread, over_under, "
                                "home_moneyline, away_moneyline, spread_home_odds, spread_away_odds, "
                                "over_odds, under_odds, home_implied_probability, away_implied_probability, "
                                "recorded_at, api_last_update) "
                                "VALUES (:game_id, :sportsbook, :is_opening, :spread, :over_under, "
                                ":home_moneyline, :away_moneyline, :spread_home_odds, :spread_away_odds, "
                                ":over_odds, :under_odds, :home_implied_probability, :away_implied_probability, "
                                ":recorded_at, :api_last_update)"
                            ),
                            {
                                "game_id": gid,
                                "sportsbook": sb_name,
                                "is_opening": False,
                                "spread": closing.get("spread"),
                                "over_under": closing.get("over_under"),
                                "home_moneyline": closing.get("home_moneyline"),
                                "away_moneyline": closing.get("away_moneyline"),
                                "spread_home_odds": closing.get("spread_home_odds"),
                                "spread_away_odds": closing.get("spread_away_odds"),
                                "over_odds": closing.get("over_odds"),
                                "under_odds": closing.get("under_odds"),
                                "home_implied_probability": _implied_prob(closing.get("home_moneyline")),
                                "away_implied_probability": _implied_prob(closing.get("away_moneyline")),
                                "recorded_at": datetime.now(timezone.utc),
                                "api_last_update": datetime.now(timezone.utc),
                            },
                        )
                        loaded += 1
                else:
                    # ── Phase 1: save opening row (first time for this book) ──
                    await db.execute(
                        text(
                            "INSERT INTO mlb.betting_lines "
                            "(game_id, sportsbook, is_opening, spread, over_under, "
                            "home_moneyline, away_moneyline, spread_home_odds, spread_away_odds, "
                            "over_odds, under_odds, home_implied_probability, away_implied_probability, "
                            "recorded_at, api_last_update) "
                            "VALUES (:game_id, :sportsbook, :is_opening, :spread, :over_under, "
                            ":home_moneyline, :away_moneyline, :spread_home_odds, :spread_away_odds, "
                            ":over_odds, :under_odds, :home_implied_probability, :away_implied_probability, "
                            ":recorded_at, :api_last_update)"
                            "ON CONFLICT (game_id, sportsbook, is_opening) DO NOTHING"
                        ),
                        {
                            "game_id": gid,
                            "sportsbook": sb_name,
                            "is_opening": True,
                            "spread": closing.get("spread"),
                            "over_under": closing.get("over_under"),
                            "home_moneyline": closing.get("home_moneyline"),
                            "away_moneyline": closing.get("away_moneyline"),
                            "spread_home_odds": closing.get("spread_home_odds"),
                            "spread_away_odds": closing.get("spread_away_odds"),
                            "over_odds": closing.get("over_odds"),
                            "under_odds": closing.get("under_odds"),
                            "home_implied_probability": _implied_prob(closing.get("home_moneyline")),
                            "away_implied_probability": _implied_prob(closing.get("away_moneyline")),
                            "recorded_at": datetime.now(timezone.utc),
                            "api_last_update": datetime.now(timezone.utc),
                        },
                    )
                    existing_openings.add(sb_name)  # prevent duplicate insert
                    loaded += 1

                any_saved = True

            if any_saved:
                updated_game_ids.add(gid)
            else:
                skipped.append(f"No valid sportsbook data for {gid}")

        except Exception as e:
            event_id = event.get("id", "?")
            logger.warning(f"Error on event {event_id}: {e}")
            skipped.append(str(e))
    logger.info(
        f"MLB per-book lines: {loaded} rows for {len(data)} games, "
        f"{len(updated_game_ids)} updated"
    )

    await db.commit()
    return {
        "loaded": loaded,
        "updated_game_ids": list(updated_game_ids),
        "skipped": skipped,
    }
