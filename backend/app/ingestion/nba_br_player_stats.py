"""
NBA player game stats ingestion from Basketball Reference.

For each FINAL game in our DB that doesn't have player stats yet,
scrape the boxscore from www.basketball-reference.com/boxscores/.

BR URL format: /boxscores/YYYYMMDD{TEAM}.html
where TEAM is the home team abbreviation.

This is a last-resort fallback when the ESPN core API doesn't have data.
"""

import asyncio
import logging
import re
import time
from typing import Optional

import httpx
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("nba-br-stats")

DB_URL = "postgresql://earl:earl2025@localhost:5432/earl_knows_football"

BR_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Team abbreviation mapping: BR uses very standard abbreviations
# Just need to check if our DB abbreviations match BR's
TEAM_MAP = {
    "GSW": "GSW",
    "NYK": "NYK",
    "BKN": "BRK",  # BR uses BRK
    "CHO": "CHO",
    "NOP": "NOP",
    "PHX": "PHO",  # BR uses PHO
    "UTA": "UTA",  # BR uses UTH  
    "SAS": "SAS",
}

# Reverse map for ESPN teams that BR might use
BR_TEAM = {
    "ATL": "ATL", "GSW": "GSW", "NYK": "NYK", "BKN": "BRK", "CHA": "CHO",
    "NOP": "NOP", "PHX": "PHO", "UTA": "UTA", "SAS": "SAS",
    "BOS": "BOS", "CLE": "CLE", "CHI": "CHI", "DAL": "DAL",
    "DEN": "DEN", "DET": "DET", "HOU": "HOU", "IND": "IND",
    "LAC": "LAC", "LAL": "LAL", "MEM": "MEM", "MIA": "MIA",
    "MIL": "MIL", "MIN": "MIN", "OKC": "OKC", "ORL": "ORL",
    "PHI": "PHI", "POR": "POR", "SAC": "SAC", "TOR": "TOR",
    "WAS": "WAS",
}


def _extract_table(html: str, table_id: str) -> list[dict]:
    """Extract player rows from a Basketball Reference basic stats table."""
    m = re.search(r'<table[^>]*id="' + re.escape(table_id) + r'"[^>]*>(.*?)</table>', html, re.DOTALL)
    if not m:
        return []
    
    table_html = m.group(1)
    tbody_m = re.search(r'<tbody>(.*?)</tbody>', table_html, re.DOTALL)
    if not tbody_m:
        return []
    
    tbody = tbody_m.group(1)
    rows = []
    
    for tr_m in re.finditer(r'<tr[^>]*>(.*?)</tr>', tbody, re.DOTALL):
        tr_html = tr_m.group(1)
        
        if re.search(r'class="[^"]*section_header', tr_html):
            continue
        if 'class="thead' in tr_html:
            continue
        
        cells = {}
        for el_m in re.finditer(r'<(?:th|td)[^>]*data-stat="([^"]*)"[^>]*>(.*?)</(?:th|td)>', tr_html, re.DOTALL):
            stat_name = el_m.group(1)
            val = re.sub(r'<[^>]+>', '', el_m.group(2)).strip()
            val = val.replace('&nbsp;', '').replace('&amp;', '&').replace('&#x27;', "'")
            csk_m = re.search(r'csk="([^"]*)"', el_m.group(0))
            if csk_m and stat_name == 'player':
                val = csk_m.group(1)
            cells[stat_name] = val
        
        if cells.get('player') and cells['player'] not in ('Reserves', 'Team Totals'):
            rows.append(cells)
    
    return rows


def _name_clean(name: str) -> str:
    """Clean BR player name format 'Brown,Jaylen' -> 'Jaylen Brown'."""
    parts = name.split(",")
    if len(parts) == 2:
        return f"{parts[1].strip()} {parts[0].strip()}"
    return name


def _sv(val, expected_type=str):
    if val is None or val == '':
        return None
    try:
        if expected_type == float:
            return float(val)
        elif expected_type == int:
            v = val.replace(',', '')
            return int(float(v))
        return val
    except (ValueError, TypeError):
        return None


def _pct(val):
    if val is None or val == '':
        return None
    if val.startswith('.'):
        return float('0' + val)
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def process_game_page(
    html: str,
    db_conn,
    db_game_id: int,
    nba_game_id: str,
    db_home_team_id: int,
    db_away_team_id: int,
    player_cache: dict,
) -> int:
    """Parse BR boxscore page and insert player stats.
    
    Returns number of rows inserted.
    """
    # Find the two basic stats table IDs
    table_ids = re.findall(r'id="(box-[A-Z]+-game-basic)"', html)
    if len(table_ids) < 2:
        logger.warning(f"  Could not find both basic stats tables")
        return 0
    
    inserted = 0
    for t_id in table_ids:
        rows = _extract_table(html, t_id)
        if not rows:
            continue
        
        # Determine team from table ID (box-BOS-game-basic -> BOS)
        team_abbr = t_id.split("-")[1]
        
        # Map to DB team ID
        db_abbr = BR_TEAM.get(team_abbr)
        if not db_abbr:
            logger.debug(f"  Unknown BR team abbreviation: {team_abbr}")
            continue
        
        db_team_id = None
        if db_abbr == db_conn.execute(text("SELECT abbreviation FROM nba.teams WHERE id = :tid"), {"tid": db_home_team_id}).scalar():
            db_team_id = db_home_team_id
        elif db_abbr == db_conn.execute(text("SELECT abbreviation FROM nba.teams WHERE id = :tid"), {"tid": db_away_team_id}).scalar():
            db_team_id = db_away_team_id
        else:
            # Look up by abbreviation
            t_row = db_conn.execute(
                text("SELECT id FROM nba.teams WHERE abbreviation = :abbr"),
                {"abbr": db_abbr},
            ).fetchone()
            if t_row:
                db_team_id = t_row[0]
        
        if not db_team_id:
            continue
        
        for row in rows:
            player_name = _name_clean(row.get("player", ""))
            if not player_name:
                continue
            
            # Find player in DB by name
            p_row = db_conn.execute(
                text("SELECT id FROM nba.players WHERE LOWER(name) = LOWER(:name) LIMIT 1"),
                {"name": player_name},
            ).fetchone()
            
            if not p_row:
                # Try by last name
                parts = player_name.split()
                if len(parts) >= 2:
                    last = parts[-1]
                    candidates = db_conn.execute(
                        text("SELECT id, name FROM nba.players WHERE LOWER(name) LIKE LOWER(:patt) LIMIT 3"),
                        {"patt": f"%{last}%"},
                    ).fetchall()
                    if len(candidates) == 1:
                        p_row = candidates[0]
                    elif len(candidates) > 0:
                        # Pick best match
                        for cid, cname in candidates:
                            if cname.lower().startswith(parts[0].lower()):
                                p_row = (cid, cname)
                                break
                        if not p_row:
                            p_row = candidates[0]
            
            if not p_row:
                continue
            
            db_player_id = p_row[0]
            
            try:
                db_conn.execute(text("""
                    INSERT INTO nba.player_game_stats
                        (game_id, player_id, team_id, nba_game_id, nba_player_id,
                         minutes, field_goals_made, field_goals_attempted, field_goal_pct,
                         three_pointers_made, three_pointers_attempted, three_pointer_pct,
                         free_throws_made, free_throws_attempted, free_throw_pct,
                         rebounds_offensive, rebounds_defensive, rebounds_total,
                         assists, steals, blocks, turnovers, fouls_personal,
                         points, plus_minus)
                    VALUES
                        (:gid, :pid, :tid, :ngid, null,
                         :minutes, :fgm, :fga, :fg_pct,
                         :fg3m, :fg3a, :fg3_pct,
                         :ftm, :fta, :ft_pct,
                         :orb, :drb, :trb,
                         :ast, :stl, :blk, :tov, :pf,
                         :pts, :pm)
                    ON CONFLICT (game_id, player_id) DO NOTHING
                """), {
                    "gid": db_game_id,
                    "pid": db_player_id,
                    "tid": db_team_id,
                    "ngid": nba_game_id,
                    "minutes": _sv(row.get("mp")),
                    "fgm": _sv(row.get("fg"), int),
                    "fga": _sv(row.get("fga"), int),
                    "fg_pct": _pct(row.get("fg_pct")),
                    "fg3m": _sv(row.get("fg3"), int),
                    "fg3a": _sv(row.get("fg3a"), int),
                    "fg3_pct": _pct(row.get("fg3_pct")),
                    "ftm": _sv(row.get("ft"), int),
                    "fta": _sv(row.get("fta"), int),
                    "ft_pct": _pct(row.get("ft_pct")),
                    "orb": _sv(row.get("orb"), int),
                    "drb": _sv(row.get("drb"), int),
                    "trb": _sv(row.get("trb"), int),
                    "ast": _sv(row.get("ast"), int),
                    "stl": _sv(row.get("stl"), int),
                    "blk": _sv(row.get("blk"), int),
                    "tov": _sv(row.get("tov"), int),
                    "pf": _sv(row.get("pf"), int),
                    "pts": _sv(row.get("pts"), int),
                    "pm": _sv(row.get("plus_minus"), int),
                })
                inserted += 1
            except Exception as e:
                logger.warning(f"  DB error for {player_name}: {e}")
    
    return inserted


async def scrape_missing_games(season_year: int = 2025):
    """Scrape player stats from BR for games missing them."""
    engine = create_engine(DB_URL)
    
    with engine.connect() as db_conn:
        # Get games missing player stats
        games = db_conn.execute(text("""
            SELECT g.id, g.nba_game_id, g.date::date,
                   h.abbreviation as home, a.abbreviation as away,
                   h.id as home_id, a.id as away_id
            FROM nba.games g
            JOIN nba.seasons s ON s.id = g.season_id
            JOIN nba.teams h ON h.id = g.home_team_id
            JOIN nba.teams a ON a.id = g.away_team_id
            WHERE s.year = :year AND g.game_type = 'REG' AND g.status::text = 'FINAL'
              AND NOT EXISTS (
                  SELECT 1 FROM nba.player_game_stats pgs WHERE pgs.game_id = g.id
              )
            ORDER BY g.date
        """), {"year": season_year}).fetchall()
        
        if not games:
            logger.info(f"[{season_year}] All games have player stats! 🎉")
            engine.dispose()
            return
        
        logger.info(f"[{season_year}] {len(games)} games missing player stats")
        
        total = 0
        success = 0
        skip_404 = 0
        
        for idx, (db_gid, nba_gid, date, home, away, home_id, away_id) in enumerate(games, 1):
            if not nba_gid:
                continue
            
            # Build BR URL: /boxscores/YYYYMMDD{HOME}.html
            date_str = date.strftime("%Y%m%d")
            br_abbr = BR_TEAM.get(home, home)
            url = f"https://www.basketball-reference.com/boxscores/{date_str}0{br_abbr}.html"
            
            # Respect BR
            if idx > 1:
                await asyncio.sleep(3.5)
            
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url, headers=BR_HEADERS)
                    
                    if resp.status_code == 404:
                        logger.warning(f"  [{idx}/{len(games)}] {date} {away}@{home}: 404")
                        skip_404 += 1
                        continue
                    
                    if resp.status_code != 200:
                        logger.warning(f"  [{idx}/{len(games)}] {date} {away}@{home}: {resp.status_code}")
                        continue
                    
                    html_text = resp.text
                    
                    # Verify the page has basic stats tables
                    if 'basic' not in html_text or not re.search(r'id="box-[A-Z]+-game-basic"', html_text):
                        logger.warning(f"  [{idx}/{len(games)}] {date} {away}@{home}: No basic stats tables found")
                        continue
                    
                    rows_inserted = process_game_page(
                        html_text, db_conn, db_gid, nba_gid,
                        home_id, away_id, {}
                    )
                    
                    if rows_inserted > 0:
                        total += rows_inserted
                        success += 1
                        db_conn.commit()
                        logger.info(f"  [{idx}/{len(games)}] {date} {away}@{home}: {rows_inserted} rows ({success} games, {total} total)")
                    else:
                        logger.warning(f"  [{idx}/{len(games)}] {date} {away}@{home}: 0 rows matched")
            
            except httpx.TimeoutException:
                logger.warning(f"  [{idx}/{len(games)}] {date} {away}@{home}: Timeout")
            except Exception as e:
                logger.warning(f"  [{idx}/{len(games)}] {date} {away}@{home}: {e}")
        
        db_conn.commit()
        logger.info(f"\nDone: {total} rows from {success} games, {skip_404} 404s, {len(games) - success - skip_404} other failures")
    
    engine.dispose()


if __name__ == "__main__":
    logger.info("Starting BR scrape for missing 2025-26 REG games...")
    asyncio.run(scrape_missing_games(2025))
