"""Backfill the missing 2019-20 NBA bubble seeding games (Jul 30 - Aug 14, 2020).

These games belong to season_id=29 (real 2019-20 season, shortened by COVID).
ESPN's scoreboard serves them normally; the main ingester's full-season date walk
(Oct 2019-Jun 2020) created 971 of the 1051 REG games but the summer-2020 seeding
round was skipped. We fetch just the bubble window and upsert by nba_game_id.

After this, run backfill_nba_games_espn_team_stats.py to fill the new games'
boxscore columns (fouls, total_turnovers, estimated_possessions), then rebuild
cumulative/rolling/splits.
"""
import asyncio
from datetime import date, timedelta

import httpx
from sqlalchemy import select

from app.database import async_session
from app.models.nba import NBASeason, NBAGame
from app.ingestion.nba_games_espn import fetch_espn_games, ensure_team

Season = NBASeason
Game = NBAGame

SEASON_YEAR = 2019   # real 2019-20 -> season_id via ensure_season
BUBBLE_START = date(2020, 7, 25)   # a few days before seeding tips, to catch any early games
BUBBLE_END = date(2020, 8, 15)     # through the final seeding date


def _game_status(comp):
    st = comp.get("status", {}).get("type", {})
    return st.get("state") or ""


def _game_type(comp, season_year):
    st = comp.get("status", {}).get("type", {})
    detail = (st.get("detail") or "").lower()
    name = (st.get("name") or "").lower()
    if "playoff" in detail or "playoff" in name:
        return "POST"
    if "play-in" in detail or "play-in" in name:
        return "PLAYIN"
    return "REG"


async def backfill_bubble() -> dict:
    loaded = updated = skipped = 0
    async with httpx.AsyncClient(timeout=30) as client:
        async with async_session() as session:
            season_id = await _ensure_season(session)
            current = BUBBLE_START
            while current <= BUBBLE_END:
                date_str = current.strftime("%Y%m%d")
                events = await fetch_espn_games(date_str, client)
                for event in events:
                    game_id = int(event["id"])
                    comps = event.get("competitions", [])
                    if not comps:
                        skipped += 1
                        continue
                    comp = comps[0]
                    competitors = comp.get("competitors", [])
                    if len(competitors) < 2:
                        skipped += 1
                        continue
                    away_team = next((c for c in competitors if c.get("homeAway") == "away"), competitors[0])
                    home_team = next((c for c in competitors if c.get("homeAway") == "home"), competitors[1])
                    away_abbr = (away_team["team"].get("abbreviation") or away_team["team"].get("shortDisplayName") or away_team["team"]["id"])
                    home_abbr = (home_team["team"].get("abbreviation") or home_team["team"].get("shortDisplayName") or home_team["team"]["id"])
                    away_id = await ensure_team(away_abbr, session, client)
                    home_id = await ensure_team(home_abbr, session, client)
                    try:
                        away_score = int(away_team.get("score")) if away_team.get("score") else None
                        home_score = int(home_team.get("score")) if home_team.get("score") else None
                    except (ValueError, TypeError):
                        away_score = home_score = None
                    import datetime as _dt
                    game_dt = None
                    try:
                        game_dt = _dt.datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
                    except Exception:
                        pass
                    venue = ""
                    try:
                        venue = comp.get("venue", {}).get("fullName", "")
                    except Exception:
                        pass
                    game_status = "FINAL" if _game_status(comp) == "post" else (
                        "LIVE" if _game_status(comp) == "in"
                        else ("FINAL" if home_score is not None and away_score is not None else "SCHEDULED"))
                    gt = _game_type(comp, SEASON_YEAR)

                    existing = (await session.execute(
                        select(Game).where(Game.nba_game_id == str(game_id))
                    )).scalar_one_or_none()

                    if existing:
                        existing.season_id = season_id
                        existing.home_score = home_score
                        existing.away_score = away_score
                        existing.status = game_status
                        existing.game_type = gt
                        if game_dt:
                            existing.date = game_dt
                        updated += 1
                    else:
                        session.add(Game(
                            nba_game_id=str(game_id),
                            season_id=season_id,
                            home_team_id=home_id,
                            away_team_id=away_id,
                            home_score=home_score,
                            away_score=away_score,
                            date=game_dt,
                            venue=venue,
                            status=game_status,
                            game_type=gt,
                        ))
                        loaded += 1
                current += timedelta(days=1)
            await session.commit()
    return {"loaded": loaded, "updated": updated, "skipped": skipped}


async def _ensure_season(session) -> int:
    row = (await session.execute(select(Season).where(Season.year == SEASON_YEAR))).scalar_one_or_none()
    if row:
        return row.id
    s = Season(year=SEASON_YEAR, name="2019-2020 NBA Season", is_regular_season=True)
    session.add(s)
    await session.flush()
    return s.id


if __name__ == "__main__":
    print(asyncio.run(backfill_bubble()))
