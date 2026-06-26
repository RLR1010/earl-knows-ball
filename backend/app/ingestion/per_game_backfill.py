"""
Per-game backfill: one API call per game for opening + closing lines.

Opening: game_time - 12 hours (optimal timing)
Closing: game_time - 15 minutes (as close to game as practical)

Usage:
    python -m app.ingestion.per_game_backfill --sport mlb --year 2021
    python -m app.ingestion.per_game_backfill --sport mlb --start 2021 --end 2025
"""
import asyncio, logging, os, sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger('earl.per_game_backfill')

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# ── Team maps ────────────────────────────────────────────────────────

MLB_TEAM_MAP = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Cleveland Indians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET",
    "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Oakland Athletics": "OAK",
    "Athletics": "OAK",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}

NFL_TEAM_MAP = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL", "Denver Broncos": "DEN",
    "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN", "New England Patriots": "NE",
    "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
    "Washington Football Team": "WAS", "Washington Redskins": "WAS",
    "Oakland Raiders": "LV", "San Diego Chargers": "LAC",
    "St. Louis Rams": "LAR",
}

NBA_TEAM_MAP = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN", "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET", "Golden State Warriors": "GSW",
    "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Clippers": "LAC",
    "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR", "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}

# Opening offset: how early to capture opening lines (hours before game)
# Closing offset: how close to game (minutes before)
SPORT_CONFIG = {
    "mlb": {"api": "baseball_mlb", "map": MLB_TEAM_MAP, "open_hrs": 18, "close_min": 10},
    "nfl": {"api": "americanfootball_nfl", "map": NFL_TEAM_MAP, "open_hrs": 72, "close_min": 10},
    "nba": {"api": "basketball_nba", "map": NBA_TEAM_MAP, "open_hrs": 12, "close_min": 10},
}

import os as _os
DB_URL = _os.environ.get("SYNC_DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")


def implied_prob(american_odds: int) -> Optional[float]:
    if american_odds is None or american_odds == 0: return None
    return round(100 / (american_odds + 100), 4) if american_odds > 0 else round(abs(american_odds) / (abs(american_odds) + 100), 4)


async def run():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--sport", choices=["nfl", "mlb", "nba"], required=True)
    parser.add_argument("--year", type=int, action="append")
    parser.add_argument("--start", type=int, default=2021)
    parser.add_argument("--end", type=int, default=2025)
    parser.add_argument("--opening-only", action="store_true")
    parser.add_argument("--closing-only", action="store_true")
    args = parser.parse_args()

    if not ODDS_API_KEY:
        logger.error("ODDS_API_KEY not set"); return

    cfg = SPORT_CONFIG[args.sport]
    api_sport = cfg["api"]
    team_map = cfg["map"]
    open_hrs = cfg["open_hrs"]
    close_min = cfg["close_min"]

    years = sorted(set(args.year or list(range(args.start, args.end + 1))))

    engine = create_async_engine(DB_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        # Get team IDs
        r = await db.execute(sql_text(f"SELECT abbreviation, id FROM {args.sport}.teams"))
        team_id_map = {row.abbreviation: row.id for row in r.fetchall()}

        # Get existing game IDs already stored (to skip)
        existing = {"opening": set(), "closing": set()}
        for src_type, src_name in [("opening", "the_odds_api_opening"), ("closing", "the_odds_api_closing")]:
            r = await db.execute(
                sql_text(f"SELECT game_id FROM {args.sport}.betting_lines WHERE source = :src"),
                {"src": src_name},
            )
            existing[src_type] = {row.game_id for row in r.fetchall()}

        # Get all games for target seasons
        year_condition = ", ".join(str(y) for y in years)
        sql = sql_text(f"""
            SELECT g.id, g.date, ht.abbreviation AS ht, at.abbreviation AS at
            FROM {args.sport}.games g
            JOIN {args.sport}.seasons s ON s.id = g.season_id
            JOIN {args.sport}.teams ht ON ht.id = g.home_team_id
            JOIN {args.sport}.teams at ON at.id = g.away_team_id
            WHERE s.year IN ({year_condition})
              AND g.date < NOW()
            ORDER BY g.date
        """)
        r = await db.execute(sql)
        games = r.fetchall()
        logger.info(f"{args.sport.upper()} {years}: {len(games)} games total")

        # Pre-filter for what needs doing
        opening_games = [g for g in games if g.id not in existing["opening"]] if not args.closing_only else []
        closing_games = [g for g in games if g.id not in existing["closing"]] if not args.opening_only else []
        logger.info(f"  Opening: {len(opening_games)} remaining")
        logger.info(f"  Closing: {len(closing_games)} remaining")

    # ── Process ────────────────────────────────────────────────────────
    engine = create_async_engine(DB_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    insert_cols = [
        "game_id", "source", "sportsbook",
        "spread", "spread_home_odds", "spread_away_odds",
        "over_under", "over_odds", "under_odds",
        "home_moneyline", "away_moneyline",
        "home_implied_probability", "away_implied_probability",
        "is_opening", "api_last_update", "recorded_at",
    ]
    insert_sql_t = f"""
        INSERT INTO {args.sport}.betting_lines
        ({', '.join(insert_cols)})
        VALUES ({', '.join(f':{c}' for c in insert_cols)})
        ON CONFLICT (game_id, source, sportsbook, is_opening) DO NOTHING
    """

    async with httpx.AsyncClient(timeout=30.0) as client:
        stats = {"opening_calls": 0, "closing_calls": 0, "opening_inserted": 0, "closing_inserted": 0, "opening_nodata": 0, "closing_nodata": 0}

        # ── Process ────────────────────────────────────────────────────
        for snapshot_type, game_list, offset_type in [
            ("opening", opening_games, "hours"),
            ("closing", closing_games, "minutes"),
        ]:
            if not game_list:
                continue

            source_name = f"the_odds_api_{snapshot_type}"
            offset_value = open_hrs if snapshot_type == "opening" else close_min
            label = f"{snapshot_type} ({offset_value}{'h' if offset_type == 'hours' else 'm'} before)"
            is_open = True if snapshot_type == "opening" else False
            # MLB/NBA use varchar is_opening, NFL uses boolean
            if args.sport in ('mlb', 'nba'):
                is_open = 't' if snapshot_type == 'opening' else 'f'

            logger.info(f"\nProcessing {label}: {len(game_list)} games")

            for idx, g in enumerate(game_list):
                if idx > 0 and idx % 200 == 0:
                    logger.info(f"  {snapshot_type}: {idx}/{len(game_list)} ({stats[f'{snapshot_type}_inserted']} lines)")

                if snapshot_type == "opening":
                    # ── Forward-stepping opener ──
                    # Start at open_hrs before game, step forward 1h until
                    # FanDuel has all 3 markets (h2h, spreads, totals).
                    # Each step saves ALL books found for that snapshot.
                    max_steps = open_hrs - 2  # stop 2h before game
                    fanduel_done = False

                    for step in range(max_steps + 1):
                        qt = g.date - timedelta(hours=offset_value - step)
                        date_param = qt.strftime("%Y-%m-%dT%H:%M:%SZ")

                        resp = await client.get(
                            f"{ODDS_API_BASE}/historical/sports/{api_sport}/odds",
                            params={"apiKey": ODDS_API_KEY, "regions": "us",
                                    "markets": "h2h,spreads,totals", "oddsFormat": "american",
                                    "date": date_param},
                        )
                        stats["opening_calls"] += 1

                        if resp.status_code != 200:
                            continue

                        events = resp.json().get("data", [])
                        matched_ev = None
                        for ev in events:
                            ha = team_map.get(ev.get("home_team", ""))
                            aa = team_map.get(ev.get("away_team", ""))
                            if ha == g.ht and aa == g.at:
                                matched_ev = ev
                                break

                        if not matched_ev:
                            continue

                        # Extract per-sportsbook lines
                        batch = []
                        fanduel_row = None
                        for bk in matched_ev.get("bookmakers", []):
                            row = {
                                "game_id": g.id, "source": source_name, "sportsbook": bk["key"],
                                "spread": None, "spread_home_odds": None, "spread_away_odds": None,
                                "over_under": None, "over_odds": None, "under_odds": None,
                                "home_moneyline": None, "away_moneyline": None,
                                "home_implied_probability": None, "away_implied_probability": None,
                                "is_opening": is_open,
                                "api_last_update": qt,
                                "recorded_at": datetime.now(timezone.utc),
                            }
                            home_name = matched_ev.get("home_team", "")
                            away_name = matched_ev.get("away_team", "")

                            for market in bk.get("markets", []):
                                key = market["key"]
                                outcomes = market.get("outcomes", [])

                                if key == "spreads":
                                    for o in outcomes:
                                        name = o.get("name", "")
                                        if name == home_name:
                                            row["spread"] = o.get("point") if o.get("point") is not None else None
                                            row["spread_home_odds"] = o.get("price")
                                        elif name == away_name:
                                            row["spread_away_odds"] = o.get("price")
                                elif key == "totals":
                                    for o in outcomes:
                                        if o.get("name") == "Over":
                                            row["over_under"] = o.get("point")
                                            row["over_odds"] = o.get("price")
                                        elif o.get("name") == "Under":
                                            row["under_odds"] = o.get("price")
                                elif key == "h2h":
                                    for o in outcomes:
                                        price = o.get("price")
                                        if o.get("name") == home_name:
                                            row["home_moneyline"] = price
                                            row["home_implied_probability"] = implied_prob(price)
                                        elif o.get("name") == away_name:
                                            row["away_moneyline"] = price
                                            row["away_implied_probability"] = implied_prob(price)

                            # Only save if we got at least one market
                            if any([
                                row["spread"] is not None, row["over_under"] is not None,
                                row["home_moneyline"] is not None,
                            ]):
                                batch.append(row)

                            # Track FanDuel completeness at this step
                            if bk["key"] == "fanduel":
                                fanduel_row = row

                        # Save this step's snapshot
                        if batch:
                            async with Session() as db:
                                await db.execute(sql_text(insert_sql_t), batch)
                                await db.commit()
                            stats["opening_inserted"] += len(batch)
                        else:
                            # Game found but no usable lines from any book; keep stepping
                            continue

                        # Check if FanDuel has all 3 markets
                        if fanduel_row is not None:
                            fd_has_h2h = fanduel_row["home_moneyline"] is not None or fanduel_row["away_moneyline"] is not None
                            fd_has_spread = fanduel_row["spread"] is not None
                            fd_has_total = fanduel_row["over_under"] is not None
                            if fd_has_h2h and fd_has_spread and fd_has_total:
                                fanduel_done = True
                                logger.debug(f"  Opening complete for game {g.id} at T-{offset_value - step}h (step {step}): FanDuel full")
                                break
                            else:
                                logger.debug(f"  Opening partial for game {g.id} at T-{offset_value - step}h (step {step}): "
                                            f"FanDuel h2h={fd_has_h2h} spread={fd_has_spread} total={fd_has_total}")
                        else:
                            logger.debug(f"  Opening partial for game {g.id} at T-{offset_value - step}h (step {step}): FanDuel not present")

                    if not fanduel_done:
                        stats["opening_nodata"] += 1

                else:
                    # ── Closing snapshot: single call ──
                    qt = g.date - timedelta(minutes=offset_value)
                    date_param = qt.strftime("%Y-%m-%dT%H:%M:%SZ")

                    resp = await client.get(
                        f"{ODDS_API_BASE}/historical/sports/{api_sport}/odds",
                        params={"apiKey": ODDS_API_KEY, "regions": "us",
                                "markets": "h2h,spreads,totals", "oddsFormat": "american",
                                "date": date_param},
                    )
                    stats["closing_calls"] += 1

                    if resp.status_code != 200:
                        stats["closing_nodata"] += 1
                        continue

                    events = resp.json().get("data", [])
                    matched_ev = None
                    for ev in events:
                        ha = team_map.get(ev.get("home_team", ""))
                        aa = team_map.get(ev.get("away_team", ""))
                        if ha == g.ht and aa == g.at:
                            matched_ev = ev
                            break

                    if not matched_ev:
                        stats["closing_nodata"] += 1
                        continue

                    batch = []
                    for bk in matched_ev.get("bookmakers", []):
                        row = {
                            "game_id": g.id, "source": source_name, "sportsbook": bk["key"],
                            "spread": None, "spread_home_odds": None, "spread_away_odds": None,
                            "over_under": None, "over_odds": None, "under_odds": None,
                            "home_moneyline": None, "away_moneyline": None,
                            "home_implied_probability": None, "away_implied_probability": None,
                            "is_opening": is_open,
                            "api_last_update": qt,
                            "recorded_at": datetime.now(timezone.utc),
                        }
                        home_name = matched_ev.get("home_team", "")
                        away_name = matched_ev.get("away_team", "")

                        for market in bk.get("markets", []):
                            key = market["key"]
                            outcomes = market.get("outcomes", [])

                            if key == "spreads":
                                for o in outcomes:
                                    name = o.get("name", "")
                                    if name == home_name:
                                        row["spread"] = o.get("point") if o.get("point") is not None else None
                                        row["spread_home_odds"] = o.get("price")
                                    elif name == away_name:
                                        row["spread_away_odds"] = o.get("price")
                            elif key == "totals":
                                for o in outcomes:
                                    if o.get("name") == "Over":
                                        row["over_under"] = o.get("point")
                                        row["over_odds"] = o.get("price")
                                    elif o.get("name") == "Under":
                                        row["under_odds"] = o.get("price")
                            elif key == "h2h":
                                for o in outcomes:
                                    price = o.get("price")
                                    if o.get("name") == home_name:
                                        row["home_moneyline"] = price
                                        row["home_implied_probability"] = implied_prob(price)
                                    elif o.get("name") == away_name:
                                        row["away_moneyline"] = price
                                        row["away_implied_probability"] = implied_prob(price)

                        if any([
                            row["spread"] is not None, row["over_under"] is not None,
                            row["home_moneyline"] is not None,
                        ]):
                            batch.append(row)

                    if batch:
                        async with Session() as db:
                            await db.execute(sql_text(insert_sql_t), batch)
                            await db.commit()
                        stats["closing_inserted"] += len(batch)

            logger.info(f"  {snapshot_type} done: {stats[f'{snapshot_type}_inserted']} lines, "
                        f"{stats[f'{snapshot_type}_nodata']} no data, "
                        f"{stats[f'{snapshot_type}_calls']} API calls")

    # ── Summary ──
    logger.info(f"\n{'='*50}")
    logger.info(f"{args.sport.upper()} {years} COMPLETE")
    logger.info(f"  Opening: {stats['opening_inserted']} lines, {stats['opening_nodata']} no data, {stats['opening_calls']} calls")
    logger.info(f"  Closing: {stats['closing_inserted']} lines, {stats['closing_nodata']} no data, {stats['closing_calls']} calls")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
