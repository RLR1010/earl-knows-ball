"""Shared odds-ingest logic used by all three sports.

The Odds API (the-odds-api.com) returns an identical response shape for every
sport; only the sport key, team-name map, and target tables differ. This module
holds the sport-agnostic pieces so NFL, NBA, and MLB ingest lines in exactly the
same way (see ``snapshot_opening_lines`` and the per-sport wrappers).

Reference sport module: ``mlb_betting_lines.snapshot_mlb_opening_lines``.
"""
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import certifi
import httpx
import logging

from ..core.config import settings as app_settings

logger = logging.getLogger(__name__)

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Odds API sportsbook keys -> normalized names we store.
ODDS_API_SPORTSBOOK_MAP = {
    "fanduel": "FanDuel",
    "draftkings": "DraftKings",
}

# How far into the future (hours) to consider when pulling games / lines.
LINE_WINDOW_HOURS = 48


@dataclass
class SportConfig:
    """Per-sport configuration for ``snapshot_opening_lines``."""

    name: str                 # display name, e.g. "NFL"
    odds_key: str             # Odds API sport key, e.g. "americanfootball_nfl"
    schema: str               # DB schema only (e.g. "nfl"); used for log labels
    bets_table: str           # e.g. "nfl.betting_lines"
    games: str                # e.g. "nfl.games"
    seasons: str              # e.g. "nfl.seasons"
    teams: str                # e.g. "nfl.teams"
    team_name_map: dict       # Odds API full name -> our abbreviation
    # SQL for the current season (used to scope the upcoming-game query). The
    # generic query joins games -> season by year; override with season_stmt if
    # the sport stores season differently.
    year: int


def _implied_prob(american_odds: float) -> float:
    """Convert American odds to implied probability (0..1)."""
    if american_odds is None:
        return 0.0
    if american_odds > 0:
        return 100.0 / (american_odds + 100.0)
    return abs(american_odds) / (abs(american_odds) + 100.0)


def _extract_odds_from_markets(markets, home_name: str, away_name: str) -> dict:
    """Pull h2h / spreads / totals from an Odds API ``markets`` list.

    The Odds API returns separate ``outcomes`` lists per market; the market is
    identified by ``key``. This returns a dict with the current (closing) values
    for the game's two teams.
    """
    result = {
        "spread": None,
        "spread_home_odds": None,
        "spread_away_odds": None,
        "over_under": None,
        "over_odds": None,
        "under_odds": None,
        "home_moneyline": None,
        "away_moneyline": None,
    }

    for market in markets or []:
        mkey = (market or {}).get("key", "")
        outcomes = market.get("outcomes", [])
        if mkey == "h2h":
            for o in outcomes:
                if o.get("name") == home_name:
                    result["home_moneyline"] = o.get("price")
                elif o.get("name") == away_name:
                    result["away_moneyline"] = o.get("price")
        elif mkey == "spreads":
            for o in outcomes:
                if o.get("name") == home_name:
                    result["spread"] = o.get("point")
                    result["spread_home_odds"] = o.get("price")
                elif o.get("name") == away_name:
                    result["spread_away_odds"] = o.get("price")
        elif mkey == "totals":
            for o in outcomes:
                if o.get("name") == "Over":
                    result["over_under"] = o.get("point")
                    result["over_odds"] = o.get("price")
                elif o.get("name") == "Under":
                    result["under_odds"] = o.get("price")

    # Default the missing spread side / under odds to the same value where safe.
    if result["spread"] is not None:
        if result["spread_home_odds"] is None:
            result["spread_home_odds"] = -110
        if result["spread_away_odds"] is None:
            result["spread_away_odds"] = -110
    return result


_SSL_CTX = None


def _get_ssl_context():
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    return _SSL_CTX


async def snapshot_opening_lines(cfg: SportConfig, db, api_key: str, days: int = 3):
    """Fetch current season's lines from the Odds API and upsert per-book rows.

    Mirrors ``mlb_betting_lines.snapshot_mlb_opening_lines`` but parameterized
    by ``SportConfig`` so NFL / NBA / MLB ingest identical data the same way.

    Returns dict: {loaded, updated_game_ids, skipped}.
    """
    from sqlalchemy import text

    sport = cfg.name
    schema = cfg.schema

    # 1. Find the current season id + upcoming games window.
    season_row = (
        await db.execute(
            text(f"SELECT id FROM {cfg.seasons} WHERE year = :y"), {"y": cfg.year}
        )
    ).first()

    if not season_row:
        logger.info(f"  [{sport}] No season row found for {cfg.year}")
        return {"loaded": 0, "updated_game_ids": [], "skipped": []}
    season_id = season_row[0]

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=2)
    window_end = now + timedelta(hours=LINE_WINDOW_HOURS)

    games = (
        await db.execute(
            text(
                f"""
                SELECT g.id, g.home_team_id, g.away_team_id, g.date,
                       th.abbreviation AS home_abbr, ta.abbreviation AS away_abbr
                FROM {cfg.games} g
                JOIN {cfg.teams} th ON th.id = g.home_team_id
                JOIN {cfg.teams} ta ON ta.id = g.away_team_id
                WHERE g.date BETWEEN :ws AND :we
                ORDER BY g.date
                """
            ),
            {"ws": window_start, "we": window_end},
        )
    ).fetchall()

    if not games:
        logger.info(f"  [{sport}] No upcoming games in window")
        return {"loaded": 0, "updated_game_ids": [], "skipped": []}

    # Map abbreviation -> game row for quick lookup by Odds team names.
    games_by_abbr = {}
    for g in games:
        games_by_abbr.setdefault(g.home_abbr, []).append(g)
        games_by_abbr.setdefault(g.away_abbr, []).append(g)

    # 2. Call the Odds API.
    url = f"{ODDS_API_BASE}/sports/{cfg.odds_key}/odds/"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    async with httpx.AsyncClient(verify=_get_ssl_context()) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

    loaded = 0
    updated_game_ids = set()
    skipped = []

    for event in data:
        try:
            home_name = event.get("home_team", "")
            away_name = event.get("away_team", "")
            home_abbr = cfg.team_name_map.get(home_name)
            away_abbr = cfg.team_name_map.get(away_name)

            if not home_abbr or not away_abbr:
                skipped.append(f"Unknown team names: {home_name} / {away_name}")
                continue

            # Match to an upcoming game by both abbreviations.
            candidates_home = games_by_abbr.get(home_abbr, [])
            candidates_away = games_by_abbr.get(away_abbr, [])
            game = None
            for gh in candidates_home:
                if any(gh.id == ga.id for ga in candidates_away):
                    game = gh
                    break
            if game is None:
                skipped.append(f"No match for {home_abbr} @ {away_abbr}")
                continue

            gid = game.id
            game_started = game.date <= now

            bookmakers = event.get("bookmakers", [])
            bookmakers = [
                b
                for b in bookmakers
                if (b.get("key", "") or "").lower() in {"fanduel", "draftkings"}
            ]
            if not bookmakers:
                skipped.append(f"No FanDuel/DraftKings bookmakers for {gid}")
                continue

            existing_openings = set()
            open_rows = await db.execute(
                text(
                    f"SELECT sportsbook FROM {cfg.bets_table} "
                    "WHERE game_id = :gid AND is_opening = :opening"
                ),
                {"gid": gid, "opening": True},
            )
            for (sb,) in open_rows:
                existing_openings.add(sb)

            any_saved = False
            for bookmaker in bookmakers:
                sb_key = (bookmaker.get("key", "") or "").lower().strip()
                sb_name = ODDS_API_SPORTSBOOK_MAP.get(sb_key, sb_key)
                markets = bookmaker.get("markets", [])

                closing = _extract_odds_from_markets(markets, home_name, away_name)
                if not closing.get("home_moneyline") or not closing.get("away_moneyline"):
                    continue

                required = [
                    "spread", "over_under", "home_moneyline", "away_moneyline",
                    "spread_home_odds", "spread_away_odds", "over_odds", "under_odds",
                ]
                if not all(closing.get(f) is not None for f in required):
                    continue

                home_ml = closing["home_moneyline"]
                away_ml = closing["away_moneyline"]
                home_p = _implied_prob(home_ml)
                away_p = _implied_prob(away_ml)

                if sb_name in existing_openings:
                    # Book already has an opening row — refresh the closing row.
                    # (Matches snapshot_mlb_opening_lines: delete old closing row,
                    #  then INSERT a fresh closing row with current lines.)
                    if not game_started:
                        await db.execute(
                            text(
                                f"DELETE FROM {cfg.bets_table} "
                                "WHERE game_id = :gid AND sportsbook = :sb AND is_opening = :op"
                            ),
                            {"gid": gid, "sb": sb_name, "op": False},
                        )
                        await db.execute(
                            text(
                                f"""
                                INSERT INTO {cfg.bets_table}
                                (game_id, sportsbook, is_opening, spread, over_under,
                                 home_moneyline, away_moneyline, spread_home_odds,
                                 spread_away_odds, over_odds, under_odds,
                                 home_implied_probability, away_implied_probability,
                                 recorded_at, api_last_update)
                                VALUES (:gid, :sb, :op, :spr, :ou, :hml, :aml,
                                        :sprh, :spra, :oov, :unv, :hp, :ap, :t, :t)
                                """
                            ),
                            {
                                "gid": gid, "sb": sb_name, "op": False,
                                "spr": closing["spread"], "sprh": closing["spread_home_odds"],
                                "spra": closing["spread_away_odds"], "ou": closing["over_under"],
                                "oov": closing["over_odds"], "unv": closing["under_odds"],
                                "hml": home_ml, "aml": away_ml, "hp": home_p, "ap": away_p,
                                "t": datetime.now(timezone.utc),
                            },
                        )
                        loaded += 1
                else:
                    # First time this book appears — write the immutable opening row.
                    await db.execute(
                        text(
                            f"""
                            INSERT INTO {cfg.bets_table}
                            (game_id, sportsbook, is_opening, spread, over_under,
                             home_moneyline, away_moneyline, spread_home_odds,
                             spread_away_odds, over_odds, under_odds,
                             home_implied_probability, away_implied_probability,
                             recorded_at, api_last_update)
                            VALUES (:gid, :sb, :op, :spr, :ou, :hml, :aml,
                                    :sprh, :spra, :oov, :unv, :hp, :ap, :t, :t)
                            ON CONFLICT (game_id, sportsbook, is_opening) DO NOTHING
                            """
                        ),
                        {
                            "gid": gid, "sb": sb_name, "op": True,
                            "spr": closing["spread"], "sprh": closing["spread_home_odds"],
                            "spra": closing["spread_away_odds"], "ou": closing["over_under"],
                            "oov": closing["over_odds"], "unv": closing["under_odds"],
                            "hml": home_ml, "aml": away_ml, "hp": home_p, "ap": away_p,
                            "t": datetime.now(timezone.utc),
                        },
                    )
                    existing_openings.add(sb_name)
                    loaded += 1

                any_saved = True

            if any_saved:
                updated_game_ids.add(gid)
            else:
                skipped.append(f"No valid sportsbook data for {gid}")

        except Exception as e:
            event_id = event.get("id", "?")
            logger.warning(f"[{sport}] Error on event {event_id}: {e}")
            skipped.append(str(e))

    await db.commit()
    logger.info(
        f"[{sport}] per-book lines: {loaded} rows for {len(data)} games, "
        f"{len(updated_game_ids)} updated"
    )
    return {"loaded": loaded, "updated_game_ids": list(updated_game_ids), "skipped": skipped}
