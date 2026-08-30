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
# Extended through the NBA Finals (Lakers beat Heat Oct 11, 2020) so the bubble
# PLAYOFF games (Aug 18 - Oct 11) get ingested too (classified POST by _game_type).
BUBBLE_END = date(2020, 10, 12)    # through the Finals + 1 slack day


def _game_status(comp):
    st = comp.get("status", {}).get("type", {})
    return st.get("state") or ""


def _game_type(comp, season_year, game_dt=None):
    """Classify NFL/NBA style: POST for bubble playoffs, PLAYIN for the play-in.

    ESPN does NOT put "playoff"/"play-in" in the status detail for these games
    (it's just "Final"/"status_final"). The round is in competitions[0].notes[0]
    .headline, e.g. "WEST 1ST ROUND - GAME 1", "WEST PLAY-IN - GAME 1",
    "EAST CONFERENCE FINALS", "NBA FINALS". We rely on that headline, with the
    bubble playoff start date (Aug 17, 2020) as a robust fallback boundary.
    """
    st = comp.get("status", {}).get("type", {})
    blurb = ""
    notes = comp.get("notes") or []
    if notes:
        blurb = (notes[0].get("headline") or "").lower()
    detail = (st.get("detail") or "").lower()
    name = (st.get("name") or "").lower()
    hay = " ".join([blurb, detail, name])
    if "play-in" in hay:
        return "PLAYIN"
    if ("playoff" in hay or "1st round" in blurb or "conference" in blurb
            or "finals" in blurb or "semifinals" in blurb or "quarterfinal" in blurb):
        return "POST"
    # Robust fallback: any 2019-20 bubble game on/after Aug 17 2020 is a playoff game
    # (postseason tipped Aug 17; Finals ended Oct 11, 2020).
    if season_year == 2019 and game_dt is not None:
        d = game_dt.date() if hasattr(game_dt, "date") else game_dt
        if d >= date(2020, 8, 17) and d <= date(2020, 10, 15):
            return "POST"
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
                    gt = _game_type(comp, SEASON_YEAR, game_dt)

                    # Guard: skip never-played/postponed placeholder rows. ESPN leaves
                    # 0-0 "games" in the schedule for games that were postponed and later
                    # rescheduled (e.g. the 2020 bubble boycott games Aug 26-29). Those are
                    # NOT real games — the rescheduled version is served under a different
                    # nba_game_id with real scores, and we must not store the 0-0 ghost.
                    # A played game is FINAL with actual score values; treat any game that
                    # isn't final, or that has both scores 0, as a placeholder and skip it.
                    if (game_status != "FINAL"
                            or home_score is None or away_score is None
                            or (home_score == 0 and away_score == 0)):
                        skipped += 1
                        continue

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
