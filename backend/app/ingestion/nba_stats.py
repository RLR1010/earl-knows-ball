"""
NBA data ingestion pipeline - Basketball-Reference source.
Loads 2006-2026 player rosters, season stats, and game data.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.nba import (
    NBATeam, NBASeason, NBAGame, NBAGameStatus, NBAPlayer, NBAPlayerSeasonStats,
)

logger = logging.getLogger("earl.nba_stats")

NBA_TEAMS = [
    (1610612737, "ATL", "Atlanta Hawks", "East", "Southeast"),
    (1610612738, "BOS", "Boston Celtics", "East", "Atlantic"),
    (1610612739, "CLE", "Cleveland Cavaliers", "East", "Central"),
    (1610612740, "NOP", "New Orleans Pelicans", "West", "Southwest"),
    (1610612741, "CHI", "Chicago Bulls", "East", "Central"),
    (1610612742, "DAL", "Dallas Mavericks", "West", "Southwest"),
    (1610612743, "DEN", "Denver Nuggets", "West", "Northwest"),
    (1610612744, "GSW", "Golden State Warriors", "West", "Pacific"),
    (1610612745, "HOU", "Houston Rockets", "West", "Southwest"),
    (1610612746, "LAC", "LA Clippers", "West", "Pacific"),
    (1610612747, "LAL", "Los Angeles Lakers", "West", "Pacific"),
    (1610612748, "MIA", "Miami Heat", "East", "Southeast"),
    (1610612749, "MIL", "Milwaukee Bucks", "East", "Central"),
    (1610612750, "MIN", "Minnesota Timberwolves", "West", "Northwest"),
    (1610612751, "BKN", "Brooklyn Nets", "East", "Atlantic"),
    (1610612752, "NYK", "New York Knicks", "East", "Atlantic"),
    (1610612753, "ORL", "Orlando Magic", "East", "Southeast"),
    (1610612754, "IND", "Indiana Pacers", "East", "Central"),
    (1610612755, "PHI", "Philadelphia 76ers", "East", "Atlantic"),
    (1610612756, "PHX", "Phoenix Suns", "West", "Pacific"),
    (1610612757, "POR", "Portland Trail Blazers", "West", "Northwest"),
    (1610612758, "SAC", "Sacramento Kings", "West", "Pacific"),
    (1610612759, "SAS", "San Antonio Spurs", "West", "Southwest"),
    (1610612760, "OKC", "Oklahoma City Thunder", "West", "Northwest"),
    (1610612761, "TOR", "Toronto Raptors", "East", "Atlantic"),
    (1610612762, "UTA", "Utah Jazz", "West", "Northwest"),
    (1610612763, "MEM", "Memphis Grizzlies", "West", "Southwest"),
    (1610612764, "WAS", "Washington Wizards", "East", "Southeast"),
    (1610612765, "DET", "Detroit Pistons", "East", "Central"),
    (1610612766, "CHA", "Charlotte Hornets", "East", "Southeast"),
]

YEARS = list(range(2006, 2027))
BBREF_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def season_str(y: int) -> str:
    return f"{y}-{str(y+1)[-2:]}"


def _si(v) -> int | None:
    if v is None or v == "": return None
    try: return int(v.replace(",", ""))
    except: return None


def _sf(v) -> float | None:
    if v is None or v == "": return None
    try: return float(v)
    except: return None


async def sync_teams(db: AsyncSession) -> dict[int, int]:
    result = {}
    for aid, abbr, name, conf, div in NBA_TEAMS:
        r = await db.execute(select(NBATeam).where(NBATeam.abbreviation == abbr))
        t = r.scalar_one_or_none()
        if not t:
            t = NBATeam(abbreviation=abbr, name=name, conference=conf, division=div)
            db.add(t)
            await db.flush()
        result[aid] = t.id
    return result


async def sync_seasons(db: AsyncSession) -> dict[int, int]:
    result = {}
    for y in YEARS:
        r = await db.execute(select(NBASeason).where(NBASeason.year == y))
        s = r.scalar_one_or_none()
        if not s:
            s = NBASeason(year=y)
            db.add(s)
            await db.flush()
        result[y] = s.id
    return result



async def fetch_bbref_json(url: str) -> str | None:
    max_retries = 5
    for attempt in range(max_retries):
        await asyncio.sleep(15)
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as c:
            try:
                r = await c.get(url, headers=BBREF_HEADERS)
                if r.status_code == 429:
                    wait = 20 * (attempt + 1)
                    logger.warning(f"Rate limited. Waiting {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return r.text
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"BBRef error (attempt {attempt+1}): {e}")
                    await asyncio.sleep(10)
                else:
                    logger.warning(f"BBRef error (final): {e}")
    return None


async def load_players_and_stats(db: AsyncSession, team_map: dict[int, int], season_map: dict[int, int]) -> dict:
    """Load all players and season stats from BBRef totals pages."""
    seen_nba_ids = set()
    player_count = 0
    stat_count = 0

    from bs4 import BeautifulSoup

    for year in YEARS:
        ss = season_str(year)
        url = f"https://www.basketball-reference.com/leagues/NBA_{year}_totals.html"
        html = await fetch_bbref_json(url)
        if not html:
            logger.warning(f"  Skipping {ss}: no data")
            continue

        try:
            soup = BeautifulSoup(html, 'html.parser')
        except Exception:
            logger.warning(f"  Skipping {ss}: parse error")
            continue

        table = soup.find('table', id='totals_stats')
        if not table:
            logger.warning(f"  Skipping {ss}: no stats table")
            continue

        season_id = season_map[year]
        year_rows = 0

        all_tr_rows = table.find('tbody').find_all('tr', recursive=False)
        logger.info(f"  {ss}: {len(all_tr_rows)} total rows in tbody")

        for tr in all_tr_rows:
            # Skip partial_table rows (player stats split by team)
            if 'partial_table' in tr.get('class', []):
                continue

            name_el = tr.find('td', {'data-stat': 'name_display'})
            if not name_el: continue
            name = (name_el.find('a') or name_el).text.strip()
            if not name or name == 'Player': continue

            pos = (tr.find('td', {'data-stat': 'pos'}) or name_el).text.strip()[:4] if tr.find('td', {'data-stat': 'pos'}) else "F"

            # BBRef uses team_name_abbr for the 3-letter team code (e.g. "ATL", "BOS")
            team_abbr = ""
            team_el = tr.find('td', {'data-stat': 'team_name_abbr'})
            if team_el:
                a_tag = team_el.find('a')
                team_abbr = a_tag.text.strip().upper() if a_tag else team_el.text.strip().upper()

            # Map to our team IDs
            team_id = None
            for aid, abbr, _, _, _ in NBA_TEAMS:
                if abbr == team_abbr:
                    team_id = team_map[aid]
                    break

            # Extract NBA ID from player page URL
            nba_id = None
            a_tag = name_el.find('a')
            if a_tag and a_tag.get('href'):
                m = re.search(r'/players/[a-z]/([a-z0-9]{6,12})\.html', a_tag['href'])
                if m:
                    nba_id = hash(m.group(1)) % (10**9) + 100000  # synthetic ID

            # Upsert player
            player = None
            if nba_id:
                r = await db.execute(select(NBAPlayer).where(NBAPlayer.nba_id == nba_id))
                player = r.scalar_one_or_none()
            if not player:
                r = await db.execute(select(NBAPlayer).where(NBAPlayer.name == name).limit(1))
                player = r.scalar_one_or_none()
            if not player and nba_id:
                player = NBAPlayer(nba_id=nba_id, name=name, position=pos, team_id=team_id, active=1)
                db.add(player)
                await db.flush()
                player_count += 1
            elif player:
                player.name = name
                player.position = pos

            if not player:
                continue

            # Parse stats
            def g(stat): return (tr.find('td', {'data-stat': stat}) or name_el).text.strip()

            gp = _si(g('games')) or 0
            if gp == 0:
                continue

            # Check for duplicate stat row
            r2 = await db.execute(
                select(NBAPlayerSeasonStats).where(
                    NBAPlayerSeasonStats.player_id == player.id,
                    NBAPlayerSeasonStats.season_id == season_id,
                )
            )
            existing = r2.scalar_one_or_none()
            s = existing or NBAPlayerSeasonStats(player_id=player.id, season_id=season_id)

            s.team_id = team_id
            s.games_played = gp
            s.games_started = _si(g('games_started')) or 0
            s.minutes_played = _sf(g('mp'))
            s.points = _si(g('pts')) or 0
            s.field_goals_made = _si(g('fg')) or 0
            s.field_goals_attempted = _si(g('fga')) or 0
            s.field_goal_pct = _sf(g('fg_pct'))
            s.three_points_made = _si(g('fg3')) or 0
            s.three_points_attempted = _si(g('fg3a')) or 0
            s.three_point_pct = _sf(g('fg3_pct'))
            s.free_throws_made = _si(g('ft')) or 0
            s.free_throws_attempted = _si(g('fta')) or 0
            s.free_throw_pct = _sf(g('ft_pct'))
            s.rebounds = _si(g('trb')) or 0
            s.offensive_rebounds = _si(g('orb')) or 0
            s.defensive_rebounds = _si(g('drb')) or 0
            s.assists = _si(g('ast')) or 0
            s.turnovers = _si(g('tov')) or 0
            s.steals = _si(g('stl')) or 0
            s.blocks = _si(g('blk')) or 0
            s.personal_fouls = _si(g('pf')) or 0

            # Derived
            if s.games_played > 0:
                s.points_per_game = round(s.points / s.games_played, 1)
                s.rebounds_per_game = round(s.rebounds / s.games_played, 1) if s.rebounds else None
                s.assists_per_game = round(s.assists / s.games_played, 1) if s.assists else None
                s.minutes_played = _sf(g('mp'))
            if s.assists and s.turnovers and s.turnovers > 0:
                s.assists_turnover_ratio = round(s.assists / s.turnovers, 2)

            if not existing:
                db.add(s)
            year_rows += 1
            stat_count += 1

        await db.commit()
        logger.info(f"  {ss}: {year_rows} players with stats")

    return {"players": player_count, "stats": stat_count}


async def load_game_logs(db: AsyncSession, team_map: dict[int, int], season_map: dict[int, int]) -> int:
    """Load NBA game schedules from BBRef league schedule pages."""
    from bs4 import BeautifulSoup
    total = 0

    for year in YEARS:
        ss = season_str(year)
        url = f"https://www.basketball-reference.com/leagues/NBA_{year}_games.html"
        html = await fetch_bbref_json(url)
        if not html:
            logger.warning(f"  Skipping {ss} games: no data")
            continue

        try:
            soup = BeautifulSoup(html, 'html.parser')
        except Exception:
            continue

        table = soup.find('table', id='games')
        if not table:
            logger.warning(f"  Skipping {ss} games: no table")
            continue

        season_id = season_map[year]
        count = 0

        for tr in table.find('tbody').find_all('tr', recursive=False):
            date_el = tr.find('td', {'data-stat': 'date_game'})
            if not date_el: continue
            date_str = (date_el.find('a') or date_el).text.strip()

            visitor_el = tr.find('td', {'data-stat': 'visitor_team_name'})
            home_el = tr.find('td', {'data-stat': 'home_team_name'})
            if not visitor_el or not home_el: continue

            visitor_a = visitor_el.find('a')
            home_a = home_el.find('a')
            visitor_abbr = visitor_a.text.strip() if visitor_a else visitor_el.text.strip()
            home_abbr = home_a.text.strip() if home_a else home_el.text.strip()

            visitor_id = None
            home_id = None
            for aid, abbr, _, _, _ in NBA_TEAMS:
                if abbr == visitor_abbr: visitor_id = team_map[aid]
                if abbr == home_abbr: home_id = team_map[aid]
            if not visitor_id or not home_id:
                continue

            visitor_pts = _si((tr.find('td', {'data-stat': 'visitor_points'}) or date_el).text.strip())
            home_pts = _si((tr.find('td', {'data-stat': 'home_points'}) or date_el).text.strip())

            try:
                game_date = datetime.strptime(date_str, "%a, %b %d, %Y").replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            status = NBAGameStatus.FINAL if (visitor_pts is not None or home_pts is not None) else NBAGameStatus.SCHEDULED
            game_id_str = f"{year}_{home_abbr}_{visitor_abbr}_{date_str[:10]}"

            g = NBAGame(
                nba_game_id=game_id_str,
                season_id=season_id,
                game_type="REG",
                home_team_id=home_id,
                away_team_id=visitor_id,
                date=game_date,
                status=status,
                home_score=home_pts,
                away_score=visitor_pts,
            )
            db.add(g)
            count += 1

        await db.commit()
        logger.info(f"  {ss}: {count} games")
        total += count

    return total


async def load_all() -> dict:
    """Full pipeline: load NBA data 2006-2026 from BBRef."""
    results = {}
    async with async_session() as db:
        logger.info("=" * 60)
        logger.info("NBA Stats Ingestion Pipeline (BBRef)")
        logger.info("=" * 60)

        # Step 0: Teams & seasons
        logger.info("\n[Step 0] Syncing teams and seasons...")
        team_map = await sync_teams(db)
        season_map = await sync_seasons(db)
        await db.commit()
        logger.info(f"  Teams: {len(team_map)}, Seasons: {len(season_map)}")

        # Step 1: Players + Stats
        logger.info("\n[Step 1] Loading players and season stats from BBRef...")
        ps = await load_players_and_stats(db, team_map, season_map)
        results["players"] = ps["players"]
        results["season_stats"] = ps["stats"]
        logger.info(f"  New players: {ps['players']}, stat entries: {ps['stats']}")

        # Step 2: Game logs
        logger.info("\n[Step 2] Loading game logs from BBRef...")
        total_games = await load_game_logs(db, team_map, season_map)
        results["games"] = total_games

        logger.info("\n✅ NBA ingestion complete!")
        logger.info(f"  Players: {ps['players']}")
        logger.info(f"  Season stats: {ps['stats']}")
        logger.info(f"  Games: {total_games}")

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(load_all())
