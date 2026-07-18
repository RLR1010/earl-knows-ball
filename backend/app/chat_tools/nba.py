"""NBA-specific tool definitions and executors for the tool-calling chat engine.

All raw SQL queries use actual nba schema column names (verified against the DB).
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone as dt_timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import (
    NBATeam,
    NBAPlayer,
    NBAPlayerSeasonStats,
    NBAPlayerGameStats,
    NBADfsSalary,
)

logger = logging.getLogger("earl.chat_tools.nba")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _today_chicago() -> date:
    return datetime.now(dt_timezone(timedelta(hours=-5))).date()  # CDT


async def _resolve_season_year(db: AsyncSession) -> int:
    """Return the year of the most recent NBA season."""
    r = await db.execute(text("SELECT MAX(year) FROM nba.seasons"))
    val = r.scalar_one_or_none()
    if val is None:
        raise ValueError("No NBA seasons found")
    return val


async def _resolve_season_id(db: AsyncSession, year: int | None = None) -> int:
    if year is None:
        year = await _resolve_season_year(db)
    r = await db.execute(text("SELECT id FROM nba.seasons WHERE year = :y"), {"y": year})
    val = r.scalar_one_or_none()
    if val is None:
        raise ValueError(f"No NBA season found for year {year}")
    return val


async def _resolve_team_id(db: AsyncSession, name_or_abbr: str) -> int | None:
    clean = name_or_abbr.strip().lower()
    for col in ("abbreviation", "name"):
        r = await db.execute(
            text(f"SELECT id FROM nba.teams WHERE LOWER({col}) = :q"),
            {"q": clean},
        )
        tid = r.scalar_one_or_none()
        if tid:
            return tid
    r = await db.execute(
        text("SELECT id FROM nba.teams WHERE LOWER(name) LIKE :q OR LOWER(abbreviation) LIKE :q"),
        {"q": f"%{clean}%"},
    )
    return r.scalar_one_or_none()


# ─── Tool Definitions ────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_team_info",
            "description": "Get basic info about an NBA team: name, abbreviation, location, conference, division.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {
                        "type": "string",
                        "description": "Team name or abbreviation (e.g., 'Boston Celtics', 'BOS', 'Lakers')",
                    },
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_stats",
            "description": "Get season stats for an NBA team: PPG, OPPG, W/L record, recent form from completed games.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {
                        "type": "string",
                        "description": "Team name or abbreviation",
                    },
                    "season_year": {
                        "type": "integer",
                        "description": "Season year (defaults to current)",
                    },
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_standings",
            "description": "Get NBA standings with W/L records, win pct, grouped by conference and division.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_todays_games",
            "description": "Get all NBA games scheduled on a given date, including status and score. CRITICAL: Only pass game_date if the user SPECIFICALLY asks about a different date. For 'today' or 'tonight' queries, OMIT game_date so it uses the correct America/Chicago date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_date": {
                        "type": "string",
                        "description": "Date in YYYY-MM-DD format. Defaults to today if omitted.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_game_info",
            "description": "Get detailed info about a specific NBA game: score, betting lines (spread, OU, ML), venue, status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_id": {
                        "type": "integer",
                        "description": "Game ID from get_todays_games or other query.",
                    },
                },
                "required": ["game_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_head_to_head",
            "description": "Get head-to-head results between two NBA teams: recent meetings and scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team1": {"type": "string", "description": "Team name or abbreviation"},
                    "team2": {"type": "string", "description": "Team name or abbreviation"},
                    "limit": {"type": "integer", "description": "Meetings to return (default 5, max 10)"},
                },
                "required": ["team1", "team2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_stats",
            "description": "Get season stats for an NBA player by name: PTS, REB, AST, STL, BLK, FG%, 3P%, FT%, usage rate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player full name (e.g., 'Giannis Antetokounmpo')"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_game_logs",
            "description": "Get game-by-game stats for an NBA player over a season: points, rebounds, assists, fantasy. Great for seeing streaks and trends.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player full name"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                    "limit": {"type": "integer", "description": "Recent games (default 10, max 20)"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dfs_salaries",
            "description": "Get DK/FD DFS salaries for NBA players on a given team for a given date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation"},
                    "game_date": {"type": "string", "description": "Date YYYY-MM-DD (defaults to today)"},
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_game_prediction",
            "description": "Get Earl's model prediction for an NBA game: ATS pick, O/U pick, moneyline with confidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_id": {"type": "integer", "description": "Game ID from get_todays_games."},
                },
                "required": ["game_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_articles",
            "description": "Search NBA news articles by keyword. Returns titles, summaries, source, dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (team name, player name, topic)"},
                    "limit": {"type": "integer", "description": "Max articles (default 5, max 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_schedule",
            "description": "Get the full schedule for an NBA team: upcoming and past games with results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                    "limit": {"type": "integer", "description": "Games to return (default 10, max 20)"},
                },
                "required": ["team_name"],
            },
        },
    },
]


# ─── Tool Implementations ─────────────────────────────────────────────────────

async def _get_team_info(db: AsyncSession, args: dict) -> dict:
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    stmt = select(NBATeam).where(NBATeam.id == tid)
    r = await db.execute(stmt)
    team = r.scalar_one_or_none()
    if not team:
        return {"error": "Team not found"}
    return {
        "id": team.id,
        "name": team.name,
        "abbreviation": team.abbreviation,
        "conference": team.conference,
        "division": team.division,
    }


async def _get_team_stats(db: AsyncSession, args: dict) -> dict:
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    year = args.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    sql = text("""
        SELECT
            COUNT(*) AS total_games,
            SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score > g.away_score)
                      OR (g.away_team_id = :tid AND g.away_score > g.home_score)
                 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score < g.away_score)
                      OR (g.away_team_id = :tid AND g.away_score < g.home_score)
                 THEN 1 ELSE 0 END) AS losses,
            AVG(CASE WHEN g.home_team_id = :tid THEN g.home_score ELSE g.away_score END) AS ppg,
            AVG(CASE WHEN g.home_team_id = :tid THEN g.away_score ELSE g.home_score END) AS oppg
        FROM nba.games g
        WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
          AND g.season_id = :sid AND g.game_type = 'REG' AND g.status = 'FINAL'
    """)
    r = await db.execute(sql, {"tid": tid, "sid": sid})
    row = r.mappings().first()
    if not row or row.total_games == 0:
        return {"error": "No game data found"}

    # Recent 10
    form_sql = text("""
        SELECT CASE
            WHEN (g.home_team_id = :tid AND g.home_score > g.away_score)
              OR (g.away_team_id = :tid AND g.away_score > g.home_score)
            THEN 'W' ELSE 'L' END AS result
        FROM nba.games g
        WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
          AND g.season_id = :sid AND g.status = 'FINAL'
        ORDER BY g.date DESC LIMIT 10
    """)
    fr = await db.execute(form_sql, {"tid": tid, "sid": sid})
    form = "".join(r.result for r in fr.mappings())

    return {
        "record": f"{row.wins}-{row.losses}",
        "ppg": round(float(row.ppg or 0), 1),
        "oppg": round(float(row.oppg or 0), 1),
        "point_diff": round(float((row.ppg or 0) - (row.oppg or 0)), 1),
        "total_games": row.total_games,
        "recent_form_10": form,
    }


async def _get_standings(db: AsyncSession, args: dict) -> dict:
    year = await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    sql = text("""
        SELECT t.name, t.conference, t.division,
               COUNT(g.id) AS total_games,
               SUM(CASE WHEN (g.home_team_id = t.id AND g.home_score > g.away_score)
                          OR (g.away_team_id = t.id AND g.away_score > g.home_score)
                     THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN (g.home_team_id = t.id AND g.home_score < g.away_score)
                          OR (g.away_team_id = t.id AND g.away_score < g.home_score)
                     THEN 1 ELSE 0 END) AS losses
        FROM nba.teams t
        LEFT JOIN nba.games g ON (g.home_team_id = t.id OR g.away_team_id = t.id)
            AND g.season_id = :sid AND g.game_type = 'REG' AND g.status = 'FINAL'
        GROUP BY t.id, t.name, t.conference, t.division
        ORDER BY t.conference, t.division, wins DESC
    """)
    r = await db.execute(sql, {"sid": sid})
    standings = []
    for row in r.mappings():
        standings.append({
            "team": row.name,
            "conference": row.conference,
            "division": row.division,
            "record": f"{row.wins}-{row.losses}",
            "win_pct": round(row.wins / row.total_games, 3) if row.total_games else 0,
        })
    return {"season_year": year, "standings": standings}


async def _get_todays_games(db: AsyncSession, args: dict) -> dict:
    game_date = args.get("game_date")
    parsed = date.fromisoformat(game_date) if game_date else _today_chicago()

    sql = text("""
        SELECT g.*, ht.name AS home_name, at2.name AS away_name
        FROM nba.games g
        JOIN nba.teams ht ON ht.id = g.home_team_id
        JOIN nba.teams at2 ON at2.id = g.away_team_id
        WHERE g.date::date = :d
        ORDER BY g.date ASC
    """)
    r = await db.execute(sql, {"d": parsed})
    games = []
    for row in r.mappings():
        games.append({
            "game_id": row.id,
            "home_team": row.home_name,
            "away_team": row.away_name,
            "home_score": row.home_score,
            "away_score": row.away_score,
            "status": row.status,
            "time": str(row.date) if row.date else None,
        })
    return {"date": str(parsed), "games": games}


async def _get_game_info(db: AsyncSession, args: dict) -> dict:
    gid = args["game_id"]

    sql = text("""
        SELECT g.*, ht.name AS home_name, at2.name AS away_name,
               AVG(bl.home_spread) AS avg_spread,
               AVG(bl.over_under) AS avg_ou,
               AVG(bl.home_ml) AS avg_home_ml,
               AVG(bl.away_ml) AS avg_away_ml
        FROM nba.games g
        JOIN nba.teams ht ON ht.id = g.home_team_id
        JOIN nba.teams at2 ON at2.id = g.away_team_id
        LEFT JOIN nba.betting_lines bl ON bl.game_id = g.id
        WHERE g.id = :gid
        GROUP BY g.id, ht.name, at2.name
    """)
    r = await db.execute(sql, {"gid": gid})
    row = r.mappings().first()
    if not row:
        return {"error": f"Game not found: {gid}"}

    info = {
        "game_id": row.id,
        "date": str(row.date) if row.date else None,
        "home_team": row.home_name,
        "away_team": row.away_name,
        "home_score": row.home_score,
        "away_score": row.away_score,
        "status": row.status,
        "venue": row.venue,
    }
    if row.avg_spread is not None:
        info["betting_lines"] = {
            "avg_spread": round(float(row.avg_spread), 1),
            "avg_ou": round(float(row.avg_ou), 1),
            "avg_home_ml": round(float(row.avg_home_ml), 1) if row.avg_home_ml else None,
            "avg_away_ml": round(float(row.avg_away_ml), 1) if row.avg_away_ml else None,
        }
    return info


async def _get_head_to_head(db: AsyncSession, args: dict) -> dict:
    t1 = await _resolve_team_id(db, args.get("team1", ""))
    t2 = await _resolve_team_id(db, args.get("team2", ""))
    if not t1 or not t2:
        return {"error": "One or both teams not found"}
    lim = min(args.get("limit", 5), 10)

    sql = text("""
        SELECT g.*, ht.name AS home_name, at2.name AS away_name
        FROM nba.games g
        JOIN nba.teams ht ON ht.id = g.home_team_id
        JOIN nba.teams at2 ON at2.id = g.away_team_id
        WHERE ((g.home_team_id = :t1 AND g.away_team_id = :t2)
            OR (g.home_team_id = :t2 AND g.away_team_id = :t1))
          AND g.status = 'FINAL'
        ORDER BY g.date DESC LIMIT :lim
    """)
    r = await db.execute(sql, {"t1": t1, "t2": t2, "lim": lim})
    meetings = []
    for row in r.mappings():
        winner = None
        if row.home_score is not None and row.away_score is not None:
            winner = row.home_name if row.home_score > row.away_score else row.away_name
        meetings.append({
            "date": str(row.date) if row.date else None,
            "home": row.home_name,
            "away": row.away_name,
            "score": f"{row.home_score}-{row.away_score}",
            "winner": winner,
        })
    return {"meetings": meetings}


async def _get_player_stats(db: AsyncSession, args: dict) -> dict:
    player_name = args.get("player_name", "")
    year = args.get("season_year") or await _resolve_season_year(db)

    clean = player_name.strip()
    parts = clean.lower().split(" ", 1)
    if len(parts) == 2:
        stmt = select(NBAPlayer).where(
            NBAPlayer.name.ilike(f"{parts[0]}% {parts[1]}%"),
        )
    else:
        stmt = select(NBAPlayer).where(NBAPlayer.name.ilike(f"%{parts[0]}%"))
    r = await db.execute(stmt)
    player = r.scalar_one_or_none()
    if not player:
        return {"error": f"Player not found: {player_name}"}

    stmt = select(NBAPlayerSeasonStats).where(
        NBAPlayerSeasonStats.player_id == player.id,
        NBAPlayerSeasonStats.season_year == year,
    )
    r = await db.execute(stmt)
    stats = r.scalar_one_or_none()
    if not stats:
        return {"error": f"No season stats for {player.name} in {year}"}

    return {
        "player": player.name,
        "position": player.position,
        "season_year": year,
        "games_played": stats.games_played,
        "minutes_per_game": stats.minutes_per_game,
        "points_per_game": stats.points_per_game,
        "rebounds_per_game": stats.rebounds_per_game,
        "assists_per_game": stats.assists_per_game,
        "steals_per_game": stats.steals_per_game,
        "blocks_per_game": stats.blocks_per_game,
        "turnovers_per_game": stats.turnovers_per_game,
        "fg_pct": stats.fg_pct,
        "three_pct": stats.three_pct,
        "ft_pct": stats.ft_pct,
        "usage_rate": stats.usage_rate,
        "per": stats.per,
    }


async def _get_player_game_logs(db: AsyncSession, args: dict) -> dict:
    player_name = args.get("player_name", "")
    year = args.get("season_year") or await _resolve_season_year(db)
    lim = min(args.get("limit", 10), 20)

    clean = player_name.strip()
    parts = clean.lower().split(" ", 1)
    if len(parts) == 2:
        stmt = select(NBAPlayer).where(
            NBAPlayer.name.ilike(f"{parts[0]}% {parts[1]}%"),
        )
    else:
        stmt = select(NBAPlayer).where(NBAPlayer.name.ilike(f"%{parts[0]}%"))
    r = await db.execute(stmt)
    player = r.scalar_one_or_none()
    if not player:
        return {"error": f"Player not found: {player_name}"}

    sql = text("""
        SELECT pgs.*, g.date,
               ht.name AS opponent_name,
               CASE WHEN g.home_team_id = pgs.team_id THEN 'home' ELSE 'away' END AS venue
        FROM nba.player_game_stats pgs
        JOIN nba.games g ON g.id = pgs.game_id
        LEFT JOIN nba.teams ht ON ht.id = CASE
            WHEN g.home_team_id = pgs.team_id THEN g.away_team_id
            ELSE g.home_team_id END
        WHERE pgs.player_id = :pid AND g.season_id = (
            SELECT id FROM nba.seasons WHERE year = :year
        )
        ORDER BY g.date DESC
        LIMIT :lim
    """)
    r = await db.execute(sql, {"pid": player.id, "year": year, "lim": lim})
    games = []
    for row in r.mappings():
        games.append({
            "date": str(row.date) if row.date else None,
            "opponent": row.opponent_name,
            "venue": row.venue,
            "minutes": row.minutes,
            "points": row.points,
            "rebounds": row.rebounds,
            "assists": row.assists,
            "steals": row.steals,
            "blocks": row.blocks,
            "turnovers": row.turnovers,
            "fg_made": row.fg_made,
            "fg_att": row.fg_att,
            "three_made": row.three_made,
            "three_att": row.three_att,
            "ft_made": row.ft_made,
            "ft_att": row.ft_att,
            "fantasy_points": row.fantasy_points,
        })
    return {"player": player.name, "season_year": year, "game_logs": games}


async def _get_dfs_salaries(db: AsyncSession, args: dict) -> dict:
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    game_date_str = args.get("game_date")
    game_date = date.fromisoformat(game_date_str) if game_date_str else _today_chicago()

    stmt = select(NBADfsSalary).where(
        NBADfsSalary.team_id == tid,
        NBADfsSalary.game_date == game_date,
    ).order_by(NBADfsSalary.salary.desc())
    r = await db.execute(stmt)
    salaries = []
    for s in r.scalars():
        salaries.append({
            "player_id": s.player_id,
            "salary": s.salary,
            "site": s.site,
            "position": s.position,
        })
    return {"game_date": str(game_date), "salaries": salaries}


async def _get_game_prediction(db: AsyncSession, args: dict) -> dict:
    gid = args["game_id"]

    sql = text("""
        SELECT gp.*, ht.name AS home_name, at2.name AS away_name
        FROM nba.game_predictions gp
        JOIN nba.teams ht ON LOWER(ht.abbreviation) = LOWER(SPLIT_PART(gp.home_team, ' ', -1))
        JOIN nba.teams at2 ON LOWER(at2.abbreviation) = LOWER(SPLIT_PART(gp.away_team, ' ', -1))
        WHERE gp.game_id = :gid LIMIT 1
    """)
    r = await db.execute(sql, {"gid": gid})
    row = r.mappings().first()
    if not row:
        sql2 = text("SELECT * FROM nba.game_predictions WHERE game_id = :gid")
        r2 = await db.execute(sql2, {"gid": gid})
        row = r2.mappings().first()
        if not row:
            return {"error": f"No prediction found for game {gid}"}
        home_name = row.home_team
        away_name = row.away_team
    else:
        home_name = row.home_name
        away_name = row.away_name

    return {
        "game_id": gid,
        "home_team": home_name,
        "away_team": away_name,
        "prediction": {
            "spread_pick": row.spread_pick,
            "margin_conf": round(float(row.margin_conf or 0) * 100, 1) if row.margin_conf else None,
            "ou_pick": row.ou_pick,
            "ou_conf": round(float(row.ou_conf or 0) * 100, 1) if row.ou_conf else None,
            "ml_pick": row.ml_pick,
            "ml_conf": round(float(row.ml_conf or 0) * 100, 1) if row.ml_conf else None,
            "predicted_home_score": row.predicted_home_score,
            "predicted_away_score": row.predicted_away_score,
            "predicted_spread": row.predicted_spread,
        },
    }


async def _search_articles(db: AsyncSession, args: dict) -> dict:
    query = args.get("query", "")
    lim = min(args.get("limit", 5), 10)

    sql = text("""
        SELECT title, excerpt, source_name, published_at
        FROM nba.articles
        WHERE body ILIKE :q OR title ILIKE :q
        ORDER BY published_at DESC LIMIT :lim
    """)
    r = await db.execute(sql, {"q": f"%{query}%", "lim": lim})
    articles = []
    for row in r.mappings():
        articles.append({
            "title": row.title,
            "excerpt": row.excerpt,
            "source": row.source_name,
            "published": str(row.published_at) if row.published_at else None,
        })
    return {"articles": articles}


async def _get_team_schedule(db: AsyncSession, args: dict) -> dict:
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    year = args.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)
    lim = min(args.get("limit", 10), 20)

    sql = text("""
        SELECT g.*, ht.name AS home_name, at2.name AS away_name
        FROM nba.games g
        JOIN nba.teams ht ON ht.id = g.home_team_id
        JOIN nba.teams at2 ON at2.id = g.away_team_id
        WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
          AND g.season_id = :sid
        ORDER BY g.date ASC LIMIT :lim
    """)
    r = await db.execute(sql, {"tid": tid, "sid": sid, "lim": lim})
    games = []
    for row in r.mappings():
        opponent = row.away_name if row.home_team_id == tid else row.home_name
        venue = "home" if row.home_team_id == tid else "away"
        result_str = None
        if row.status == 'FINAL' and row.home_score is not None:
            if row.home_team_id == tid:
                result_str = "W" if row.home_score > row.away_score else "L"
            else:
                result_str = "W" if row.away_score > row.home_score else "L"
        games.append({
            "game_id": row.id,
            "opponent": opponent,
            "venue": venue,
            "date": str(row.date) if row.date else None,
            "result": result_str,
            "score": f"{row.home_score}-{row.away_score}" if row.home_score is not None else None,
            "status": row.status,
        })
    return {"season_year": year, "games": games}


# ─── Handler Map ─────────────────────────────────────────────────────────────

_TOOL_HANDLERS = {
    "get_team_info": _get_team_info,
    "get_team_stats": _get_team_stats,
    "get_standings": _get_standings,
    "get_todays_games": _get_todays_games,
    "get_game_info": _get_game_info,
    "get_head_to_head": _get_head_to_head,
    "get_player_stats": _get_player_stats,
    "get_player_game_logs": _get_player_game_logs,
    "get_dfs_salaries": _get_dfs_salaries,
    "get_game_prediction": _get_game_prediction,
    "search_articles": _search_articles,
    "get_team_schedule": _get_team_schedule,
}


# ─── Dispatcher ──────────────────────────────────────────────────────────────

async def execute_nba_tool(db: AsyncSession, tool_call) -> str:
    """Execute an NBA tool call and return a JSON result string.

    Args:
        db: Database session.
        tool_call: OpenAI-style tool call with .function.name and .function.arguments.

    Returns:
        JSON string with the result.
    """
    func_name = tool_call.function.name
    try:
        args = json.loads(tool_call.function.arguments)
    except (json.JSONDecodeError, TypeError):
        args = {}

    handler = _TOOL_HANDLERS.get(func_name)
    if not handler:
        logger.warning("Unknown NBA tool: %s", func_name)
        return json.dumps({"error": f"Unknown tool: {func_name}"})

    logger.info("NBA tool: %s args=%s", func_name, args)
    try:
        # Use a savepoint so failures don't abort the outer transaction
        async with db.begin_nested():
            result = await handler(db, args)
        return json.dumps(result, default=str, ensure_ascii=False)
    except Exception as e:
        logger.exception("Error in NBA tool %s: %s", func_name, e)
        try:
            await db.rollback()
        except Exception:
            pass
        return json.dumps({"error": f"Error executing {func_name}: {str(e)}"})
