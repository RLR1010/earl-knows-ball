"""
MLB starting lineups scraper.

Fetches probable pitchers and batting lineups from the MLB Stats API
for today's or a specific date's games.
"""
import asyncio
import logging
from datetime import date, datetime, timezone, timedelta
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("earl.mlb_lineups")

STATS_API = "https://statsapi.mlb.com"
SPORT_ID = 1  # MLB


async def fetch_schedule(game_date: date) -> list[dict]:
    """Fetch MLB schedule for a given date, return game list."""
    date_str = game_date.strftime("%m/%d/%Y")
    url = f"{STATS_API}/api/v1/schedule?date={date_str}&sportId={SPORT_ID}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            # Map MLB team IDs to our abbreviations
            away_id = game["teams"]["away"]["team"]["id"]
            home_id = game["teams"]["home"]["team"]["id"]
            game_pk = game["gamePk"]
            game_date_str = game.get("gameDate", "")
            status = game.get("status", {}).get("detailedState", "")
            away_name = game["teams"]["away"]["team"]["name"]
            home_name = game["teams"]["home"]["team"]["name"]

            # Probable pitchers (may be None early in the day)
            away_sp = game["teams"]["away"].get("probablePitcher", {})
            home_sp = game["teams"]["home"].get("probablePitcher", {})

            games.append({
                "game_pk": game_pk,
                "away_team_id": away_id,
                "home_team_id": home_id,
                "away_team_name": away_name,
                "home_team_name": home_name,
                "game_date": game_date_str,
                "status": status,
                "away_sp_id": away_sp.get("id"),
                "away_sp_name": away_sp.get("fullName"),
                "home_sp_id": home_sp.get("id"),
                "home_sp_name": home_sp.get("fullName"),
            })
    return games


async def fetch_lineups(game_pk: int) -> dict:
    """Fetch starting lineups for a game from the live feed."""
    url = f"{STATS_API}/api/v1.1/game/{game_pk}/feed/live"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        data = resp.json()

    gd = data.get("gameData", {})
    ld = data.get("liveData", {})

    # Probable pitchers
    pp = gd.get("probablePitchers", {})
    away_sp = pp.get("away", {})
    home_sp = pp.get("home", {})

    result = {
        "game_pk": game_pk,
        "away_sp": {"id": away_sp.get("id"), "name": away_sp.get("fullName")},
        "home_sp": {"id": home_sp.get("id"), "name": home_sp.get("fullName")},
        "away_lineup": [],
        "home_lineup": [],
    }

    # Batting orders from boxscore
    box = ld.get("boxscore", {})
    for side_key, side_label in [("away", "away"), ("home", "home")]:
        team_box = box.get("teams", {}).get(side_key, {})
        batters = team_box.get("batters", [])
        players = team_box.get("players", {})

        # Determine starter status: pitcher or position player
        for pid in batters:
            player_key = f"ID{pid}"
            pdata = players.get(player_key, {})
            name = pdata.get("person", {}).get("fullName", "?")
            pos = pdata.get("position", {}).get("abbreviation", "?")
            raw_order = pdata.get("battingOrder", "")
            # MLB Stats API returns batting_order as (position * 100) e.g. 100, 200...900
            # or sometimes as 1-9 directly. Normalize to 1-9.
            bo = int(raw_order) if raw_order else 0
            if bo > 9:
                bo = bo // 100
            # Check if starting pitcher
            is_pitcher = pdata.get("position", {}).get("abbreviation") == "1"

            result[f"{side_label}_lineup"].append({
                "player_id": pid,
                "name": name,
                "position": pos,
                "batting_order": bo,
                "is_starting_pitcher": is_pitcher,
            })

        # Sort by batting order
        result[f"{side_label}_lineup"].sort(key=lambda x: x["batting_order"])

    return result


async def save_lineups(db: AsyncSession, game_id: int, away_lineup: list[dict], home_lineup: list[dict]):
    """Save lineups to the mlb.lineups table."""
    from sqlalchemy import select, delete as sa_delete
    from app.models.mlb import MLBLineup

    # Delete existing lineups for this game
    await db.execute(sa_delete(MLBLineup).where(MLBLineup.game_id == game_id))

    now = datetime.now(timezone.utc)

    def _row(side: str, order: int, entry: dict) -> MLBLineup:
        return MLBLineup(
            game_id=game_id,
            team_side=side,
            batting_order=order,
            player_id=None,  # MLB API IDs don't match our DB player IDs
            player_name=entry.get("name", "?"),
            position=entry.get("position"),
            created_at=now,
            updated_at=now,
        )

    seen: set[tuple[str, int]] = set()

    def _add(side: str, bo: int, entry: dict):
        key = (side, bo)
        if key in seen:
            return
        seen.add(key)
        db.add(_row(side, bo, entry))

    for entry in away_lineup:
        bo = entry["batting_order"]
        if bo < 1 or bo > 9:
            if entry.get("is_starting_pitcher"):
                continue  # SPs handled separately by caller at bo=0
            else:
                continue
        _add("away", bo, entry)
    for entry in home_lineup:
        bo = entry["batting_order"]
        if bo < 1 or bo > 9:
            if entry.get("is_starting_pitcher"):
                continue  # SPs handled separately by caller at bo=0
            else:
                continue
        _add("home", bo, entry)


async def update_lineups_for_date(db: AsyncSession, game_date: date) -> dict:
    """
    Fetch and save lineups for all scheduled games on a given date.
    Also updates probable pitchers in the games table.
    Returns stats dict.
    """
    from sqlalchemy import select
    from app.models.mlb import MLBGames

    stats = {"games_checked": 0, "lineups_saved": 0, "pitchers_updated": 0, "errors": 0}

    # Fetch schedule from MLB API
    games = await fetch_schedule(game_date)
    if not games:
        return stats

    logger.info(f"Fetching lineups for {len(games)} games on {game_date}")
    stats["games_checked"] = len(games)

    for game_info in games:
        try:
            game_pk = game_info["game_pk"]
            if not game_pk:
                continue

            # Find matching DB game by mlb_game_id
            r = await db.execute(
                select(MLBGames).where(MLBGames.mlb_game_id == game_pk)
            )
            db_game = r.scalar_one_or_none()
            if not db_game:
                continue

            # Update probable pitchers
            changed = False
            if game_info.get("home_sp_name") and db_game.home_pitcher_name != game_info["home_sp_name"]:
                db_game.home_pitcher_name = game_info["home_sp_name"]
                changed = True
            if game_info.get("away_sp_name") and db_game.away_pitcher_name != game_info["away_sp_name"]:
                db_game.away_pitcher_name = game_info["away_sp_name"]
                changed = True
            if changed:
                stats["pitchers_updated"] += 1

            # Fetch full lineups (batting order)
            lineup_data = await fetch_lineups(game_pk)
            if "error" in lineup_data:
                continue

            away_lu = lineup_data.get("away_lineup", [])
            home_lu = lineup_data.get("home_lineup", [])

            # Save / update starting pitchers first (so old SP rows are gone before save_lineups)
            from app.models.mlb import MLBLineup
            from sqlalchemy import delete as sa_delete
            await db.execute(sa_delete(MLBLineup).where(
                MLBLineup.game_id == db_game.id, MLBLineup.batting_order == 0
            ))
            now = datetime.now(timezone.utc)
            if db_game.home_pitcher_name:
                db.add(MLBLineup(
                    game_id=db_game.id, team_side="home", batting_order=0,
                    player_id=None, player_name=db_game.home_pitcher_name,
                    position="SP", created_at=now, updated_at=now,
                ))
            if db_game.away_pitcher_name:
                db.add(MLBLineup(
                    game_id=db_game.id, team_side="away", batting_order=0,
                    player_id=None, player_name=db_game.away_pitcher_name,
                    position="SP", created_at=now, updated_at=now,
                ))
            if away_lu or home_lu:
                await save_lineups(db, db_game.id, away_lu, home_lu)
                stats["lineups_saved"] += 1

        except Exception as e:
            logger.error(f"Error processing game {game_info.get('game_pk')}: {e}")
            stats["errors"] += 1

    await db.commit()
    logger.info(f"Lineups: {stats['lineups_saved']} saved, {stats['pitchers_updated']} pitchers updated")
    return stats


if __name__ == "__main__":
    async def test():
        logging.basicConfig(level=logging.INFO)
        today = date.today()
        games = await fetch_schedule(today)
        print(f"Games today ({today}): {len(games)}")
        for g in games[:3]:
            print(f"  {g['away_team_name']} @ {g['home_team_name']}: "
                  f"SP={g['away_sp_name']} vs {g['home_sp_name']}")
            if g['game_pk']:
                lineups = await fetch_lineups(g['game_pk'])
                print(f"    Away lineup: {len(lineups['away_lineup'])} players")
                print(f"    Home lineup: {len(lineups['home_lineup'])} players")
                break

    asyncio.run(test())
    async def test():
        logging.basicConfig(level=logging.INFO)
        today = date.today()
        games = await fetch_schedule(today)
        print(f"Games today ({today}): {len(games)}")
        for g in games[:3]:
            print(f"  {g['away_team_name']} @ {g['home_team_name']}: "
                  f"SP={g['away_sp_name']} vs {g['home_sp_name']}")
            if g['game_pk']:
                lineups = await fetch_lineups(g['game_pk'])
                print(f"    Away lineup: {len(lineups['away_lineup'])} players")
                print(f"    Home lineup: {len(lineups['home_lineup'])} players")
                break

    asyncio.run(test())
