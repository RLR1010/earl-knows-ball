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
    """Fetch starting lineups for a game from the authoritative boxscore endpoint.

    Uses /api/v1/game/{id}/boxscore teams.{away,home}.battingOrder, which lists
    EXACTLY the 9 starting position players in batting order (works for historical
    completed games). Also resolves the starting pitcher per side (pitchers[0]).
    Each returned lineup entry carries the real MLB Stats API player_id.
    """
    url = f"{STATS_API}/api/v1/game/{game_pk}/boxscore"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}"}
        data = resp.json()

    teams_box = data.get("teams", {})

    result = {
        "game_pk": game_pk,
        "away_sp": {"id": None, "name": None},
        "home_sp": {"id": None, "name": None},
        "away_lineup": [],
        "home_lineup": [],
    }

    for side_key in ["away", "home"]:
        team_box = teams_box.get(side_key, {})
        batting_order = team_box.get("battingOrder", [])  # exactly the 9 starters
        players = team_box.get("players", {})
        lineup = []

        # Starting pitcher = first in pitchers[] for side
        pitchers = team_box.get("pitchers", [])
        sp_pid = pitchers[0] if pitchers else None
        if sp_pid is not None:
            pdata = players.get(f"ID{sp_pid}", {})
            result[f"{side_key}_sp"] = {
                "id": sp_pid,
                "name": pdata.get("person", {}).get("fullName"),
            }

        for idx, pid in enumerate(batting_order, start=1):
            pdata = players.get(f"ID{pid}", {})
            name = pdata.get("person", {}).get("fullName", "?")
            pos = pdata.get("position", {}).get("abbreviation", "?")
            lineup.append({
                "player_id": pid,
                "name": name,
                "position": pos,
                "batting_order": idx,
                "is_starting_pitcher": bool(sp_pid == pid),
            })

        result[f"{side_key}_lineup"] = lineup

    return result


async def save_lineups(db: AsyncSession, game_id: int, away_lineup: list[dict], home_lineup: list[dict]):
    """Save lineups to the mlb.lineups table."""
    from sqlalchemy import select, delete as sa_delete
    from app.models.mlb import MLBLineup
    from app.models.mlb.player import MLBPlayer

    # Resolve MLB Stats API player IDs -> our players.id via players.mlb_id.
    # Cache by (api_id) -> players.id for the whole run.
    mlb_to_db_id = {}
    api_ids = [
        e.get("player_id") for e in away_lineup + home_lineup
        if e.get("player_id")
    ]
    if api_ids:
        rows = (await db.execute(
            select(MLBPlayer.id, MLBPlayer.mlb_id).where(MLBPlayer.mlb_id.in_(api_ids))
        )).all()
        for db_id, mlb_id in rows:
            mlb_to_db_id[mlb_id] = db_id

    # Delete existing lineups for this game (we re-insert everything below)
    await db.execute(sa_delete(MLBLineup).where(MLBLineup.game_id == game_id))

    now = datetime.now(timezone.utc)

    def _row(side: str, order: int, entry: dict) -> MLBLineup:
        return MLBLineup(
            game_id=game_id,
            team_side=side,
            batting_order=order,
            player_id=mlb_to_db_id.get(entry.get("player_id")),  # resolve to our player id
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
        if entry.get("is_starting_pitcher"):
            _add("away", 0, entry)  # Starting pitcher
        elif 1 <= bo <= 9:
            _add("away", bo, entry)
    for entry in home_lineup:
        bo = entry["batting_order"]
        if entry.get("is_starting_pitcher"):
            _add("home", 0, entry)  # Starting pitcher
        elif 1 <= bo <= 9:
            _add("home", bo, entry)


async def update_lineups_for_date(db: AsyncSession, game_date: date) -> dict:
    """
    Fetch and save lineups for all scheduled games on a given date.
    Also updates probable pitchers in the games table.
    Returns stats dict.
    """
    from sqlalchemy import select
    from app.models.mlb import MLBGames

    stats = {"games_checked": 0, "lineups_saved": 0, "pitchers_updated": 0, "updated_game_ids": [], "errors": 0}

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
                if db_game.id not in stats["updated_game_ids"]:
                    stats["updated_game_ids"].append(db_game.id)

            # Fetch full lineups (batting order)
            lineup_data = await fetch_lineups(game_pk)
            if "error" in lineup_data:
                continue

            away_lu = lineup_data.get("away_lineup", [])
            home_lu = lineup_data.get("home_lineup", [])

            if away_lu or home_lu:
                await save_lineups(db, db_game.id, away_lu, home_lu)
                stats["lineups_saved"] += 1

            # Fallback: if the lineup API didn't include SP (AL games), insert from game record
            from sqlalchemy import select, delete as sa_delete
            from app.models.mlb import MLBLineup
            logger.info(f"  Fallback check for game {db_game.id} (mlb_id={game_pk}): HP={db_game.home_pitcher_name!r} AP={db_game.away_pitcher_name!r}")
            r = await db.execute(
                select(MLBLineup).where(
                    MLBLineup.game_id == db_game.id,
                    MLBLineup.batting_order == 0
                )
            )
            existing_pitchers = r.scalars().all()
            existing_sides = {p.team_side for p in existing_pitchers}
            logger.info(f"  Existing pitcher rows: {existing_sides}")
            now = datetime.now(timezone.utc)

            # Resolve probable-pitcher NAME -> players.id (accent-insensitive), so
            # fallback SP rows get a real player_id (previously always NULL).
            import unicodedata as _ud
            def _norm(s: str) -> str:
                return _ud.normalize("NFD", s).encode("ascii", "ignore").decode().strip().lower()
            from sqlalchemy import or_
            from app.models.mlb.player import MLBPlayer as _P
            async def _resolve(name: str):
                if not name:
                    return None
                rows = (await db.execute(
                    select(_P.id).where(
                        or_(_P.name == name, _P.name.ilike(f"{name}%"))
                    ).limit(5)
                )).scalars().all()
                # prefer exact, accent-insensitive match
                for pid in rows:
                    pname = (await db.execute(select(_P.name).where(_P.id == pid))).scalar()
                    if pname and _norm(pname) == _norm(name):
                        return pid
                return rows[0] if rows else None

            if db_game.home_pitcher_name and "home" not in existing_sides:
                pid = await _resolve(db_game.home_pitcher_name)
                logger.info(f"  Inserting home SP: {db_game.home_pitcher_name} (player_id={pid})")
                db.add(MLBLineup(
                    game_id=db_game.id, team_side="home", batting_order=0,
                    player_id=pid, player_name=db_game.home_pitcher_name,
                    position="SP", created_at=now, updated_at=now,
                ))
            if db_game.away_pitcher_name and "away" not in existing_sides:
                pid = await _resolve(db_game.away_pitcher_name)
                logger.info(f"  Inserting away SP: {db_game.away_pitcher_name} (player_id={pid})")
                db.add(MLBLineup(
                    game_id=db_game.id, team_side="away", batting_order=0,
                    player_id=pid, player_name=db_game.away_pitcher_name,
                    position="SP", created_at=now, updated_at=now,
                ))

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
