"""Player props + team futures ingestion from The Odds API.

The Odds API serves *player props* from the **per-event** endpoint:

    GET /v4/sports/{sport}/events/{eventId}/odds?markets=<keys>&regions=us

One request per event returns every requested market for that game. Unlike the
sport-level endpoint (h2h/spreads/totals), player props live behind the event
ID. Market key naming differs per sport:

  * MLB  -> ``batter_*`` / ``pitcher_*`` (e.g. batter_hits, pitcher_strikeouts)
  * NFL  -> ``player_*`` (e.g. player_pass_yds, player_rush_yds)
  * NBA/WNBA/NHL -> ``player_*`` (player_points, player_rebounds, ...)

This module holds the sport-agnostic pieces (mirrors ``odds_common``) plus the
per-sport market-key tables, so all sports ingest props the same way.

Usage (per-sport standalone scripts under ``app/scripts/``):

    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python \
        app/scripts/run_nfl_props_daily.py
"""
import logging
import unicodedata
from datetime import datetime, timezone

import httpx

from .odds_common import (
    ODDS_API_BASE,
    ODDS_API_SPORTSBOOK_MAP,
    SportConfig,
    _get_ssl_context,
)

logger = logging.getLogger(__name__)


def _normalize_name(name):
    """Normalize a player name for accent/case-insensitive matching.

    Strips diacritics and lowercases so that Odds API names ("Jose Ramirez")
    match our roster names ("José Ramírez"). Handles None safely.
    """
    if not name:
        return name
    return (
        "".join(c for c in unicodedata.normalize("NFD", str(name)) if unicodedata.category(c) != "Mn")
        .strip()
        .lower()
    )

# How many upcoming days of events to scan for props.
PROPS_DAYS = 1

# ---------------------------------------------------------------------------
# Per-sport player-prop market keys (from the-odds-api.com betting-markets doc)
# ---------------------------------------------------------------------------
MLB_PROP_MARKETS = [
    "batter_home_runs",
    "batter_hits",
    "batter_total_bases",
    "batter_rbis",
    "batter_runs_scored",
    "batter_strikeouts",
    "batter_walks",
    "pitcher_strikeouts",
    "pitcher_hits_allowed",
    "pitcher_walks",
    "pitcher_earned_runs",
    "pitcher_outs",
]

NFL_PROP_MARKETS = [
    "player_pass_yds",
    "player_pass_tds",
    "player_pass_attempts",
    "player_pass_completions",
    "player_rush_yds",
    "player_rush_attempts",
    "player_rush_tds",
    "player_reception_yds",
    "player_receptions",
    "player_rushing_receiving_yds",
    "player_kick_return_yds",
    "player_field_goals_made",
    "player_anytime_td",
]


def _prop_books() -> dict:
    """Return sportsbook map used for props. Reuse mainstream map, but props
    come back from more books than opening lines, so only store the ones we
    recognize (FanDuel/DraftKings) plus raw key as-is otherwise."""
    return ODDS_API_SPORTSBOOK_MAP


def _score_prop_outcome(market_key: str, outcome: dict) -> dict:
    """Build a ``player_daily_props`` row fragment from one Odds API outcome.

    The Odds API player-prop outcome schema (confirmed live for MLB/NFL):
      {"name": "Over", "description": "Hunter Brown", "point": 5.5, "price": 130}

    * ``name`` is the side/direction (Over / Under / Yes / No / Any)
    * ``description`` is the **player name**

    (This differs from more familiar layouts where ``name`` is the player; the
    Odds API puts the player in ``description`` for props.)
    """
    direction_raw = (outcome.get("name") or outcome.get("description") or "").strip()
    player = (outcome.get("description") or "").strip()

    direction_low = direction_raw.lower()
    if direction_low.startswith("over"):
        direction = "Over"
    elif direction_low.startswith("under"):
        direction = "Under"
    elif direction_low.startswith("yes") or direction_low == "any":
        direction = "Yes"
    elif direction_low.startswith("no"):
        direction = "No"
    else:
        direction = (direction_raw if len(direction_raw) <= 10 else direction_raw[:10]) or "Any"

    return {
        "prop_type": market_key,
        "player_name": player[:255],
        "line": outcome.get("point"),
        "odds": outcome.get("price"),
        "direction": direction,
    }


async def snapshot_player_props(
    cfg: SportConfig,
    db,
    api_key: str,
    days: int = PROPS_DAYS,
    markets: list | None = None,
):
    """Fetch player props for upcoming games and save rows to player_daily_props.

    ``db`` is an async SQLAlchemy session. ``cfg`` is the sport's
    ``SportConfig``. Returns a summary dict.

    Resolution: events are matched to our internal ``games`` by (home, away)
    abbreviation + a short date window, mirroring ``odds_common``.
    """
    import os
    from datetime import date, timedelta as _td

    from sqlalchemy import text

    from ..core.config import settings as app_settings

    if not api_key:
        api_key = os.environ.get("ODDS_API_KEY", "") or app_settings.odds_api_key

    market_keys = markets or _MARKETS_BY_SPORT.get(cfg.odds_key, [])
    schema = cfg.schema
    loaded = 0
    updated_games = 0
    book_counts: dict = {}
    skipped = []

    # ------------------------------------------------------------------
    # 1. Pull upcoming events for the sport.
    # ------------------------------------------------------------------
    events_url = f"{ODDS_API_BASE}/sports/{cfg.odds_key}/events/?apiKey={api_key}"
    async with httpx.AsyncClient(timeout=30, verify=_get_ssl_context()) as client:
        resp = await client.get(events_url)
        if resp.status_code != 200:
            logger.error(f"[{schema}] events request failed: {resp.status_code} {resp.text[:200]}")
            return {"loaded": 0, "updated_games": 0, "book_counts": {}, "skipped": [f"events HTTP {resp.status_code}"]}
        events = resp.json()

    if not isinstance(events, list) or not events:
        logger.info(f"[{schema}] no upcoming events (out of season?).")
        return {"loaded": 0, "updated_games": 0, "book_counts": {}, "skipped": []}

    # ------------------------------------------------------------------
    # 2. Load our upcoming games keyed by (home_abbrev, away_abbrev, date).
    # ------------------------------------------------------------------
    today = date.today()
    window_end = today + _td(days=days + 1)
    rows = (
        await db.execute(
            text(
                f"""
                SELECT gh.abbreviation AS home_abbrev, ga.abbreviation AS away_abbrev,
                       g.id AS game_id, g.date AS game_date,
                       g.home_team_id, g.away_team_id
                FROM {cfg.games} g
                LEFT JOIN {cfg.teams} gh ON gh.id = g.home_team_id
                LEFT JOIN {cfg.teams} ga ON ga.id = g.away_team_id
                WHERE g.date >= :today AND g.date < :window_end
                """
            ),
            {"today": today, "window_end": window_end},
        )
    ).mappings()
    game_index = {}
    for r in rows:
        key = (r["home_abbrev"], r["away_abbrev"])
        game_index.setdefault(key, []).append(
            {"game_id": r["game_id"], "home_team_id": r["home_team_id"], "away_team_id": r["away_team_id"]}
        )

    # ------------------------------------------------------------------
    # 3. For each event, pull props and save.
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    async with httpx.AsyncClient(timeout=30, verify=_get_ssl_context()) as client:
        for event in events:
            event_id = event.get("id")
            home = cfg.team_name_map.get(event.get("home_team", ""))
            away = cfg.team_name_map.get(event.get("away_team", ""))
            if not home or not away:
                skipped.append(f"unmapped teams for event {event_id}: {event.get('home_team')} @ {event.get('away_team')}")
                continue

            # find internal game id(s)
            cands = game_index.get((home, away), [])
            if not cands:
                skipped.append(f"no matching game for {home} @ {away}")
                continue
            game = cands[0]
            game_id = game["game_id"]
            home_team_id = game.get("home_team_id")
            away_team_id = game.get("away_team_id")

            # Build {player_name: team_id} map for this game's two teams.
            # Keys are normalized (accent-stripped, lowercased) so names from
            # The Odds API (e.g. "Jose Ramirez") still match our roster names
            # (e.g. "José Ramírez").
            team_map = {}
            tids = [t for t in (home_team_id, away_team_id) if t is not None]
            if tids:
                try:
                    tro = await db.execute(
                        text(
                            f"""
                            SELECT name, team_id FROM {schema}.players
                            WHERE team_id IN ({','.join(str(t) for t in tids)})
                            """
                        )
                    )
                    for pname, pteam in tro.fetchall():
                        if pname:
                            team_map[_normalize_name(pname)] = pteam
                except Exception as e:
                    logger.warning(f"[{schema}] team map load failed: {e}")


            props_url = (
                f"{ODDS_API_BASE}/sports/{cfg.odds_key}/events/{event_id}/odds/"
                f"?apiKey={api_key}&regions=us&oddsFormat=american&markets={','.join(market_keys)}"
            )
            resp = await client.get(props_url)
            if resp.status_code != 200:
                logger.warning(f"[{schema}] props failed event {event_id}: {resp.status_code} {resp.text[:150]}")
                skipped.append(f"event {event_id}: HTTP {resp.status_code}")
                continue
            data = resp.json()

            if not isinstance(data, dict) or not data.get("bookmakers"):
                continue  # no books offering props for this game

            inserted_for_game = 0
            for bm in data.get("bookmakers", []):
                book_raw = bm.get("key")
                book = ODDS_API_SPORTSBOOK_MAP.get(book_raw, book_raw)
                book_counts[book] = book_counts.get(book, 0)
                for m in bm.get("markets", []):
                    mkey = m.get("key")
                    for outcome in m.get("outcomes", []):
                        row = _score_prop_outcome(mkey, outcome)
                        player = row["player_name"]
                        if not player:
                            continue
                        try:
                            await db.execute(
                                text(
                                    f"""
                                    INSERT INTO {schema}.player_daily_props
                                        (game_id, player_name, team_id, prop_type, line,
                                         odds, direction, bookmaker, scraped_at)
                                    VALUES (:game_id, :player_name, :team_id, :prop_type, :line,
                                            :odds, :direction, :bookmaker, :scraped_at)
                                    ON CONFLICT (game_id, player_name, prop_type, direction, line, bookmaker)
                                    DO UPDATE SET
                                        odds = EXCLUDED.odds,
                                        team_id = COALESCE(EXCLUDED.team_id, {schema}.player_daily_props.team_id),
                                        scraped_at = EXCLUDED.scraped_at
                                    """
                                ),
                                {
                                    "game_id": str(game_id),
                                    "player_name": player,
                                    "team_id": team_map.get(_normalize_name(player)),
                                    "prop_type": row["prop_type"],
                                    "line": row["line"],
                                    "odds": row["odds"],
                                    "direction": row["direction"],
                                    "bookmaker": book,
                                    "scraped_at": now,
                                },
                            )
                            loaded += 1
                            inserted_for_game += 1
                            book_counts[book] += 1
                        except Exception as e:
                            logger.warning(f"[{schema}] db insert prop failed: {e}")

            if inserted_for_game:
                updated_games += 1

    await db.commit()
    logger.info(
        f"[{schema}] player props: {loaded} rows for {updated_games} games, "
        f"books={book_counts}"
    )
    return {
        "loaded": loaded,
        "updated_games": updated_games,
        "book_counts": book_counts,
        "skipped": skipped[:20],
    }


async def snapshot_team_futures(
    cfg: SportConfig,
    db,
    api_key: str,
    futures_sport_key: str,
):
    """Save championship futures (World Series / Super Bowl) to team_props.

    ``futures_sport_key`` is the Odds API outrights sport key, e.g.
    ``baseball_mlb_world_series_winner`` or ``americanfootball_nfl_super_bowl_winner``.
    Returns a summary dict.
    """
    import os

    from sqlalchemy import text

    from ..core.config import settings as app_settings

    if not api_key:
        api_key = os.environ.get("ODDS_API_KEY", "") or app_settings.odds_api_key

    schema = cfg.schema
    loaded = 0
    books: dict = {}
    skipped = []
    year = cfg.year

    url = (
        f"{ODDS_API_BASE}/sports/{futures_sport_key}/odds/"
        f"?apiKey={api_key}&regions=us&oddsFormat=american"
    )
    async with httpx.AsyncClient(timeout=30, verify=_get_ssl_context()) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.error(f"[{schema}] futures request failed: {resp.status_code} {resp.text[:200]}")
            return {"loaded": 0, "books": {}, "skipped": [f"futures HTTP {resp.status_code}"]}
        data = resp.json()

    if not isinstance(data, list) or not data:
        logger.info(f"[{schema}] no futures markets.")
        return {"loaded": 0, "books": {}, "skipped": []}

    # futures endpoint returns a single "event" (the championship), with one
    # market ("outrights") whose outcomes are the teams.
    for mkt in data[0].get("bookmakers", []):
        book_raw = mkt.get("key")
        book = ODDS_API_SPORTSBOOK_MAP.get(book_raw, book_raw)
        for m in mkt.get("markets", []):
            for outcome in m.get("outcomes", []):
                team_name = outcome.get("name")  # e.g. "Los Angeles Dodgers"
                abbrev = cfg.team_name_map.get(team_name)
                if not abbrev:
                    skipped.append(f"futures team unmapped: {team_name}")
                    continue
                try:
                    await db.execute(
                        text(
                            f"""
                            INSERT INTO {schema}.team_props
                                (season_year, team_id, bookmaker, championship_odds, scraped_at)
                            SELECT :year, t.id, :book, :odds, :scraped_at
                            FROM {cfg.teams} t
                            WHERE t.abbreviation = :abbrev
                            ON CONFLICT (season_year, team_id, bookmaker)
                            DO UPDATE SET
                                championship_odds = EXCLUDED.championship_odds,
                                scraped_at = EXCLUDED.scraped_at
                            """
                        ),
                        {
                            "year": year,
                            "book": book,
                            "odds": outcome.get("price"),
                            "abbrev": abbrev,
                            "scraped_at": datetime.now(timezone.utc),
                        },
                    )
                    loaded += 1
                    books[book] = books.get(book, 0) + 1
                except Exception as e:
                    logger.warning(f"[{schema}] futures db insert failed: {e}")

    await db.commit()
    logger.info(f"[{schema}] futures: {loaded} rows, books={books}")
    return {"loaded": loaded, "books": books, "skipped": skipped[:20]}


# Map Odds API sport key -> its default player-prop market keys.
_MARKETS_BY_SPORT = {
    "baseball_mlb": MLB_PROP_MARKETS,
    "americanfootball_nfl": NFL_PROP_MARKETS,
}
