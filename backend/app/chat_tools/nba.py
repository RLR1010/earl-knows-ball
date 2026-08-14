"""NBA-specific tool definitions and executors for the tool-calling chat engine.

All raw SQL queries use actual nba schema column names (verified against the DB).
"""

import json
import logging
import unicodedata
from datetime import date, datetime, timedelta, timezone as dt_timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import (
    NBATeam,
    NBAPlayer,
    NBAPlayerSeasonStats,
    NBAPlayerGameStats,
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


async def _resolve_data_season_id(db: AsyncSession, table: str) -> int:
    """Return the most recent season_id that actually has rows in the given nba table."""
    r = await db.execute(text(f"SELECT MAX(season_id) FROM nba.{table}"))
    sid = r.scalar_one_or_none()
    if sid is None:
        return await _resolve_season_id(db, None)
    return int(sid)


async def _resolve_props_season(db: AsyncSession) -> int:
    """Most recent season for which futures/props exist (upcoming or current)."""
    r = await db.execute(text(
        "SELECT GREATEST(COALESCE(MAX(season_year), 0), 0) FROM nba.team_props"
    ))
    val = r.scalar_one_or_none()
    if not val:
        r2 = await db.execute(text(
            "SELECT GREATEST(COALESCE(MAX(season_year), 0), 0) FROM nba.player_season_props"
        ))
        val = r2.scalar_one_or_none()
    cur = await _resolve_season_year(db)
    return int(val or cur)


def _strip_accents(s) -> str:
    """NFD-decompose + lowercase for accent-insensitive matching."""
    return unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode().lower()


async def _resolve_player_split(db: AsyncSession, player_name: str) -> dict:
    """Resolve an NBA player by (accent-insensitive) name, mirroring MLB's NFD
    normalization so "Jose Calderon" -> "José Calderón".

    Returns {'name': ..., 'id': ...} or raises ValueError.
    """
    def _norm(s: str) -> str:
        return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
    n = _norm(player_name)
    r = await db.execute(text("SELECT id, name FROM nba.players"))
    best = None
    for pid, name in r.all():
        if _norm(str(name)) == n:
            return {"name": name, "id": pid}
        if not best and n in _norm(str(name)):
            best = {"name": name, "id": pid}
    if best:
        return best
    raise ValueError(f"Player not found: {player_name}")


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
            "name": "get_game_writeup",
            "description": "Get Earl's published write-up for a specific game: the matchup/date, the public analysis (public_content), the PREMIUM analysis (premium_content), and the premium Prop Bets article (prop_title + prop_content) if one exists. Use this to reference Earl's own full write-up / premium analysis or prop picks for a game and stay consistent with them.",
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
            "description": "Get game-by-game stats for an NBA player over a season: points, rebounds, assists, steals, blocks. Great for seeing streaks and trends.",
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
            "name": "get_player_split_stats",
            "description": "Get an NBA player's split stats for handicapping: home vs away, vs East vs West, starter vs bench, back-to-back (0 days rest) vs 1+ rest, and monthly averages. Returns career (all-time) and current-season splits with PPG, RPG, APG, SPG, BPG, FG%, 3P%, FT%, TS%, plus-minus.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player full name (accent-insensitive, e.g. 'Jose Calderon' or 'Giannis Antetokounmpo')"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to most recent season)"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_split_stats",
            "description": "Get an NBA team's split stats for handicapping: home vs away, vs East vs West. Returns career (all-time) splits and the requested season's splits with W/L, PPG, Opp PPG, point differential, pace, FG%/3P%/FT%, reb/ast/stl/blk/tov/fouls, plus ATS (wins/losses/pushes, cover %) and O/U (overs/unders, over %) vs the consensus closing line. Reads nba.team_splits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation (e.g. 'Boston Celtics', 'BOS', 'Lakers')"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to most recent season)"},
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
            "description": "Search NBA news articles by semantic similarity. Filters by date range when provided. Returns titles, summaries, source, dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (team name, player name, topic)"},
                    "limit": {"type": "integer", "description": "Max articles (default 5, max 10)"},
                    "date_from": {"type": "string", "description": "Earliest publish date (ISO: YYYY-MM-DD), inclusive from midnight UTC. Example: 2025-09-01"},
                    "date_to": {"type": "string", "description": "Latest publish date (ISO: YYYY-MM-DD), inclusive through end of day UTC. Example: 2025-12-31"},
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
    {
        "type": "function",
        "function": {
            "name": "get_team_trends",
            "description": "Get an NBA team's recent performance trends: net rating, offensive/defensive rating, effective FG%, pace, ATS and over/under performance over the last 5 and 10 games, plus recent-weighted and adjusted metrics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_comparison",
            "description": "Compare two NBA teams side by side: points scored/allowed, field goal/3-point percentages, rebounds, offensive/defensive/net rating, and pace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_a": {"type": "string", "description": "First team name or abbreviation"},
                    "team_b": {"type": "string", "description": "Second team name or abbreviation"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                },
                "required": ["team_a", "team_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_rankings",
            "description": "Rank all NBA teams by a category: net rating, offensive rating, defensive rating, points per game, or pace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["net", "ortg", "drtg", "ppg", "pace"], "description": "Ranking category"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                    "limit": {"type": "integer", "description": "Number of ranked teams to return (default 10)"},
                },
                "required": ["category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_season_futures",
            "description": "Season-long futures odds. OMIT team_name to get ALL teams ranked by championship odds from favorite to biggest underdog (lowest number = best odds = favorite, e.g. -150 or +450 beats +3000). Provide team_name to get a single team's full futures (championship, make/miss playoffs, win total over/under).",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation (optional). Omit to rank all teams by championship odds, favorites first."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_season_props",
            "description": "Get an NBA player's season-long awards props (MVP, Rookie of the Year, etc.) with odds and implied probability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player name"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_game_player_props",
            "description": "Get an NBA player prop betting lines for a specific game (points/rebounds/assists/threes, DraftKings) with line and odds. Use the game_id from get_todays_games. Optionally filter to one player or prop type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_id": {"type": "integer", "description": "Game ID from get_todays_games or get_game_info."},
                    "player_name": {"type": "string", "description": "Optional: filter to one player (accent-insensitive)."},
                    "prop_type": {"type": "string", "description": "Optional: filter by prop type (e.g. Points, Rebounds, Assists)."},
                },
                "required": ["game_id"],
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

    try:
        season_id = await _resolve_season_id(db, year)
    except ValueError as e:
        return {"error": str(e)}

    stmt = select(NBAPlayerSeasonStats).where(
        NBAPlayerSeasonStats.player_id == player.id,
        NBAPlayerSeasonStats.season_id == season_id,
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
        "minutes_per_game": round(stats.minutes_played / stats.games_played, 1) if stats.games_played else 0.0,
        "points_per_game": stats.points_per_game,
        "rebounds_per_game": stats.rebounds_per_game,
        "assists_per_game": stats.assists_per_game,
        "steals_per_game": round(stats.steals / stats.games_played, 1) if stats.games_played else 0.0,
        "blocks_per_game": round(stats.blocks / stats.games_played, 1) if stats.games_played else 0.0,
        "turnovers_per_game": round(stats.turnovers / stats.games_played, 1) if stats.games_played else 0.0,
        "fg_pct": stats.field_goal_pct,
        "three_pct": stats.three_point_pct,
        "ft_pct": stats.free_throw_pct,
        "usage_rate": stats.usage_pct,
        "per": stats.efficiency,
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
            "rebounds": row.rebounds_total,
            "assists": row.assists,
            "steals": row.steals,
            "blocks": row.blocks,
            "turnovers": row.turnovers,
            "fg_made": row.field_goals_made,
            "fg_att": row.field_goals_attempted,
            "three_made": row.three_pointers_made,
            "three_att": row.three_pointers_attempted,
            "ft_made": row.free_throws_made,
            "ft_att": row.free_throws_attempted,
        })
    return {"player": player.name, "season_year": year, "game_logs": games}



async def _get_player_split_stats(db: AsyncSession, args: dict) -> dict:
    """Return an NBA player's per-split and career stats for handicapping.

    args:
        player_name: str (required; accent-insensitive)
        season_year: int (optional; default = most recent). None/latest only.

    Returns career (season_id NULL) + current-season split rows grouped by
    split_type for home/away, vs East/West, starter/bench, rest, month.
    """
    pname = args.get("player_name")
    if not pname:
        return {"error": "player_name is required"}
    try:
        player = await _resolve_player_split(db, pname)
    except ValueError as e:
        return {"error": str(e)}

    season_year = args.get("season_year")
    season_id = None
    if season_year is not None:
        try:
            season_id = await _resolve_season_id(db, int(season_year))
        except ValueError:
            return {"error": f"No NBA season found for year {season_year}"}

    # Pull career rows + (optionally) the requested season's rows.
    where = "p.player_id = :pid AND p.season_id IS NULL"
    params = {"pid": player["id"]}
    if season_id is not None:
        where += " OR (p.player_id = :pid AND p.season_id = :sid)"
        params["sid"] = season_id

    sql = text(f"""
        SELECT p.split_type, p.split_label, p.season_id,
               p.games, p.games_started, p.minutes_per_game,
               p.points_per_game, p.field_goals_pct, p.three_point_pct,
               p.free_throw_pct, p.rebounds_per_game,
               p.offensive_rebounds_per_game, p.defensive_rebounds_per_game,
               p.assists_per_game, p.steals_per_game, p.blocks_per_game,
               p.turnovers_per_game, p.fouls_per_game, p.plus_minus_per_game,
               p.true_shooting_pct
        FROM nba.player_splits p
        WHERE ({where})
        ORDER BY p.season_id NULLS FIRST, p.split_type
    """)
    r = await db.execute(sql, params)

    career = {}
    season = {}
    any_rows = False
    for row in r.mappings():
        any_rows = True
        d = {
            "label": row.split_label,
            "games": row.games,
            "games_started": row.games_started,
            "minutes_per_game": float(row.minutes_per_game) if row.minutes_per_game is not None else None,
            "points_per_game": float(row.points_per_game) if row.points_per_game is not None else None,
            "field_goals_pct": float(row.field_goals_pct) if row.field_goals_pct is not None else None,
            "three_point_pct": float(row.three_point_pct) if row.three_point_pct is not None else None,
            "free_throw_pct": float(row.free_throw_pct) if row.free_throw_pct is not None else None,
            "rebounds_per_game": float(row.rebounds_per_game) if row.rebounds_per_game is not None else None,
            "offensive_rebounds_per_game": float(row.offensive_rebounds_per_game) if row.offensive_rebounds_per_game is not None else None,
            "defensive_rebounds_per_game": float(row.defensive_rebounds_per_game) if row.defensive_rebounds_per_game is not None else None,
            "assists_per_game": float(row.assists_per_game) if row.assists_per_game is not None else None,
            "steals_per_game": float(row.steals_per_game) if row.steals_per_game is not None else None,
            "blocks_per_game": float(row.blocks_per_game) if row.blocks_per_game is not None else None,
            "turnovers_per_game": float(row.turnovers_per_game) if row.turnovers_per_game is not None else None,
            "fouls_per_game": float(row.fouls_per_game) if row.fouls_per_game is not None else None,
            "plus_minus_per_game": float(row.plus_minus_per_game) if row.plus_minus_per_game is not None else None,
            "true_shooting_pct": float(row.true_shooting_pct) if row.true_shooting_pct is not None else None,
        }
        if row.season_id is None:
            career[row.split_type] = d
        else:
            season[row.split_type] = d

    if not any_rows:
        return {"player": player["name"], "message": "No split data found.", "hint": "Run the NBA splits refresh first."}

    result = {"player": player["name"]}
    if career:
        result["career"] = {
            "home": career.get("home"), "away": career.get("away"),
            "vs_east": career.get("vs_east"), "vs_west": career.get("vs_west"),
            "starter": career.get("starter"), "bench": career.get("bench"),
            "rest0": career.get("rest0"), "rest_ge1": career.get("rest_ge1"),
        }
    if season_id is not None and season:
        result["season"] = {
            "home": season.get("home"), "away": season.get("away"),
            "vs_east": season.get("vs_east"), "vs_west": season.get("vs_west"),
            "starter": season.get("starter"), "bench": season.get("bench"),
            "rest0": season.get("rest0"), "rest_ge1": season.get("rest_ge1"),
            "months": {k: v for k, v in season.items() if k.startswith("month_")},
        }
    return result


async def _get_team_split_stats(db: AsyncSession, args: dict) -> dict:
    """Return NBA team split stats (home/away, vs conference) + career for handicapping.

    Reads nba.team_splits (freshly rebuilt by the nba-splits-refresh task). Returns
    both the most recent completed season's splits AND the career (lifetime) splits.

    args:
        team_name: str (required; name or abbreviation, case-insensitive)
        season_year: int (optional; default = most recent season that has data)
    """
    tname = args.get("team_name")
    if not tname:
        return {"error": "team_name is required"}

    tid = await _resolve_team_id(db, tname)
    if not tid:
        return {"error": f"No NBA team found for '{tname}'"}

    # Resolve team display name
    tn = await db.execute(
        text("SELECT name, abbreviation, conference, division FROM nba.teams WHERE id = :tid"),
        {"tid": tid},
    )
    tmeta = tn.mappings().first()
    if not tmeta:
        return {"error": f"No NBA team found for '{tname}'"}

    season_year = args.get("season_year")
    season_id = None
    if season_year is not None:
        try:
            season_id = await _resolve_season_id(db, int(season_year))
        except ValueError:
            return {"error": f"No NBA season found for year {season_year}"}

    # Career rows + (optionally) the requested season's rows.
    where = "ts.team_id = :tid AND ts.season_id IS NULL"
    params = {"tid": tid}
    if season_id is not None:
        where += " OR (ts.team_id = :tid AND ts.season_id = :sid)"
        params["sid"] = season_id

    sql = text(f"""
        SELECT ts.split_type, ts.split_label, ts.season_id,
               ts.games, ts.wins, ts.losses, ts.win_pct,
               ts.points_for, ts.points_against, ts.point_differential, ts.pace,
               ts.field_goal_pct, ts.three_point_pct, ts.free_throw_pct,
               ts.rebounds_per_game, ts.assists_per_game, ts.steals_per_game,
               ts.blocks_per_game, ts.turnovers_per_game, ts.fouls_per_game,
               ts.ats_wins, ts.ats_losses, ts.ats_pushes, ts.ats_pct,
               ts.ou_overs, ts.ou_unders, ts.ou_pushes, ts.ou_overs_pct
        FROM nba.team_splits ts
        WHERE ({where})
        ORDER BY ts.season_id NULLS FIRST, ts.split_type
    """)
    r = await db.execute(sql, params)

    def _num(v):
        return float(v) if v is not None else None

    career = {}
    season = {}
    any_rows = False
    for row in r.mappings():
        any_rows = True
        d = {
            "label": row.split_label,
            "games": row.games,
            "wins": row.wins,
            "losses": row.losses,
            "win_pct": _num(row.win_pct),
            "points_for": _num(row.points_for),
            "points_against": _num(row.points_against),
            "point_differential": _num(row.point_differential),
            "pace": _num(row.pace),
            "field_goal_pct": _num(row.field_goal_pct),
            "three_point_pct": _num(row.three_point_pct),
            "free_throw_pct": _num(row.free_throw_pct),
            "rebounds_per_game": _num(row.rebounds_per_game),
            "assists_per_game": _num(row.assists_per_game),
            "steals_per_game": _num(row.steals_per_game),
            "blocks_per_game": _num(row.blocks_per_game),
            "turnovers_per_game": _num(row.turnovers_per_game),
            "fouls_per_game": _num(row.fouls_per_game),
            "ats_wins": row.ats_wins,
            "ats_losses": row.ats_losses,
            "ats_pushes": row.ats_pushes,
            "ats_pct": _num(row.ats_pct),
            "ou_overs": row.ou_overs,
            "ou_unders": row.ou_unders,
            "ou_pushes": row.ou_pushes,
            "ou_overs_pct": _num(row.ou_overs_pct),
        }
        if row.season_id is None:
            career[row.split_type] = d
        else:
            season[row.split_type] = d

    if not any_rows:
        return {
            "team": tmeta.name,
            "message": "No split data found.",
            "hint": "Run the NBA splits refresh first.",
        }

    result = {
        "team": tmeta.name,
        "abbreviation": tmeta.abbreviation,
        "conference": tmeta.conference,
        "division": tmeta.division,
    }
    if career:
        result["career"] = {
            "home": career.get("home"), "away": career.get("away"),
            "vs_east": career.get("vs_east"), "vs_west": career.get("vs_west"),
        }
    if season_id is not None and season:
        result["season"] = {
            "home": season.get("home"), "away": season.get("away"),
            "vs_east": season.get("vs_east"), "vs_west": season.get("vs_west"),
        }
    elif season_id is None:
        # fall back: latest season present in the table
        latest = await db.execute(
            text("SELECT MAX(ts.season_id) AS max_sid FROM nba.team_splits ts WHERE ts.team_id = :tid AND ts.season_id IS NOT NULL"),
            {"tid": tid},
        )
        max_sid = latest.scalar_one_or_none()
        if max_sid and season:
            result["latest_season"] = {
                "season_id": max_sid,
                "splits": {
                    "home": season.get("home"), "away": season.get("away"),
                    "vs_east": season.get("vs_east"), "vs_west": season.get("vs_west"),
                },
            }
    return result


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
    """Search NBA articles via pgvector semantic search with optional date filter."""
    from app.ingestion.pgvector_search import search_articles

    query = args.get("query", "")
    limit = min(args.get("limit", 5), 10)

    # Convert string dates to UTC-aware datetimes for inclusive range
    raw_from = args.get("date_from")
    raw_to = args.get("date_to")
    date_from = None
    date_to = None
    if raw_from:
        try:
            date_from = datetime.fromisoformat(raw_from).replace(
                hour=0, minute=0, second=0, tzinfo=dt_timezone.utc
            )
        except (ValueError, TypeError):
            pass
    if raw_to:
        try:
            # End of day UTC so the full final day is included
            date_to = datetime.fromisoformat(raw_to).replace(
                hour=23, minute=59, second=59, tzinfo=dt_timezone.utc
            )
        except (ValueError, TypeError):
            pass

    articles = await search_articles(
        db, query, sport="nba", top_k=limit,
        date_from=date_from, date_to=date_to,
    )
    results = []
    for a in articles:
        results.append({
            "title": a.get("title", ""),
            "excerpt": (a.get("text", "") or "")[:500],
            "source": a.get("source_name", "Unknown"),
            "published": a.get("published_at", ""),
        })
    return {"articles": results}


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

async def _get_team_trends(db: AsyncSession, args: dict) -> dict:
    """Recent performance trends from nba.team_rolling_stats."""
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    if args.get("season_year"):
        season_id = await _resolve_season_id(db, args.get("season_year"))
    else:
        season_id = await _resolve_data_season_id(db, "team_rolling_stats")

    # Fetch latest available rolling row per team (order by game_date desc)
    sql = text(
        """SELECT * FROM nba.team_rolling_stats
        WHERE team_id = :tid AND season_id = :sid
        ORDER BY game_date DESC LIMIT 1"""
    )
    r = await db.execute(sql, {"tid": tid, "sid": season_id})
    row = r.mappings().first()
    if not row:
        return {"error": f"No rolling stats found for team id {tid} in season {args.get('season_year')}"}

    def f(v):
        return round(float(v), 1) if v is not None else None

    return {
        "latest_game": str(row.game_date)[:10],
        "last_5": {"wins": row.wins_5, "net_rating": f(row.net_rtg_r5), "ortg": f(row.ortg_r5), "drtg": f(row.drtg_r5), "efg_pct": f(row.efg_r5), "pace": f(row.pace_r5), "ast_ratio": f(row.ast_ratio_r5), "ats_margin": f(row.ats_margin_5), "ats_wins": row.ats_wins_5, "ou_over_wins": row.ou_wins_5, "ou_margin": f(row.ou_margin_5)},
        "last_10": {"wins": row.wins_10, "net_rating": f(row.net_rtg_r10), "ortg": f(row.ortg_r10), "drtg": f(row.drtg_r10), "efg_pct": f(row.efg_r10), "pace": f(row.pace_r10), "ats_margin": f(row.ats_margin_10), "ats_wins": row.ats_wins_10, "ou_over_wins": row.ou_wins_10},
        "recent_weighted_3": {"ppg": f(row.rw3_ppg), "net_rating": f(row.rw3_net_rtg), "drtg": f(row.rw3_drtg), "efg_pct": f(row.rw3_efg_pct)},
        "recent_weighted_5": {"ppg": f(row.rw5_ppg), "net_rating": f(row.rw5_net_rtg), "drtg": f(row.rw5_drtg), "efg_pct": f(row.rw5_efg_pct)},
        "year_adjusted": {"off_rating": f(row.adj_off_10), "def_rating": f(row.adj_def_10)},
        "consistency": {"net_rating_cv": f(row.cv10_net_rtg), "ppg_cv10": f(row.cv10_ppg), "ppg_cv20": f(row.cv20_ppg), "recency_net_rating": f(row.recency_net_rtg), "recency_ppg": f(row.recency_ppg)},
        "star_ppg_5": f(row.star_ppg_5),
    }


async def _get_team_comparison(db: AsyncSession, args: dict) -> dict:
    """Side-by-side team comparison from nba.cumulative_game_stats."""
    ta = await _resolve_team_id(db, args.get("team_a", ""))
    tb = await _resolve_team_id(db, args.get("team_b", ""))
    if not ta or not tb:
        missing = [args.get("team_a"), args.get("team_b")] if not ta else [args.get("team_b")]
        return {"error": f"Team(s) not found: {', '.join(str(m) for m in missing)}"}
    if args.get("season_year"):
        season_id = await _resolve_season_id(db, args.get("season_year"))
    else:
        season_id = await _resolve_data_season_id(db, "cumulative_game_stats")

    sql = text(
        """SELECT team_id, game_id, game_date,
              cum_ppg, cum_oppg, cum_margin_pg, cum_fg_pct, cum_fg3_pct, cum_ft_pct,
              cum_reb_pg, cum_ast_pg, cum_stl_pg, cum_blk_pg, cum_tov_pg,
              cum_ortg, cum_drtg, cum_net_ortg, cum_pace, cum_win_pct
        FROM nba.cumulative_game_stats
        WHERE team_id IN (:a, :b) AND season_id = :sid
        ORDER BY game_date DESC LIMIT 4"""
    )
    r = await db.execute(sql, {"a": ta, "b": tb, "sid": season_id})
    rows = r.mappings().all()
    if not rows:
        return {"error": f"Cumulative stats not found for comparison in season {args.get('season_year')}"}

    def f(v):
        return round(float(v), 3) if v is not None else None

    # Latest per team
    la = (await db.execute(text("SELECT * FROM nba.cumulative_game_stats WHERE team_id = :t AND season_id = :s ORDER BY game_date DESC LIMIT 1"), {"t": ta, "s": season_id})).mappings().first()
    lb = (await db.execute(text("SELECT * FROM nba.cumulative_game_stats WHERE team_id = :t AND season_id = :s ORDER BY game_date DESC LIMIT 1"), {"t": tb, "s": season_id})).mappings().first()
    if not la or not lb:
        return {"error": "Cumulative stats not found"}

    name_a = (await _team_abbr(db, ta))
    name_b = (await _team_abbr(db, tb))
    metrics = [
        ("cum_ppg", "PPG"), ("cum_oppg", "OPPG"), ("cum_margin_pg", "Margin"),
        ("cum_fg_pct", "FG%"), ("cum_fg3_pct", "3P%"), ("cum_ft_pct", "FT%"),
        ("cum_reb_pg", "Rebs"), ("cum_ast_pg", "Assists"), ("cum_stl_pg", "Steals"),
        ("cum_blk_pg", "Blocks"), ("cum_tov_pg", "TOs"),
        ("cum_ortg", "ORTG"), ("cum_drtg", "DRTG"), ("cum_net_ortg", "Net Rtg"),
        ("cum_pace", "Pace"), ("cum_win_pct", "Win %"),
    ]
    result = {"compare": {}, "team_a": name_a, "team_b": name_b}
    for col, label in metrics:
        result["compare"][label] = {name_a: f(la[col]), name_b: f(lb[col])}
    return result


async def _team_abbr(db: AsyncSession, tid: int) -> str:
    r = await db.execute(text("SELECT abbreviation FROM nba.teams WHERE id = :t"), {"t": tid})
    return r.scalar_one_or_none() or str(tid)


async def _get_team_rankings(db: AsyncSession, args: dict) -> dict:
    """Rank NBA teams by category from nba.cumulative_game_stats."""
    cat = args.get("category", "net")
    if args.get("season_year"):
        season_id = await _resolve_season_id(db, args.get("season_year"))
    else:
        season_id = await _resolve_data_season_id(db, "cumulative_game_stats")
    limit = min(args.get("limit", 10), 30)
    col = {"net": "cum_net_ortg", "ortg": "cum_ortg", "drtg": "cum_drtg", "ppg": "cum_ppg", "pace": "cum_pace"}.get(cat)
    if not col:
        return {"error": f"Unknown category: {cat}. Use net, ortg, drtg, ppg, or pace."}
    order = "DESC" if cat != "drtg" else "ASC"

    sql = text(
        f"""SELECT team_id, {col} AS val FROM (
            SELECT team_id, {col}, ROW_NUMBER() OVER (PARTITION BY team_id ORDER BY game_date DESC) rn
            FROM nba.cumulative_game_stats WHERE season_id = :sid
        ) t WHERE rn = 1 ORDER BY val {order} NULLS LAST LIMIT :limit"""
    )
    r = await db.execute(sql, {"sid": season_id, "limit": limit})
    rows = r.mappings().all()
    ranking = []
    for idx, row in enumerate(rows, 1):
        abbr = await _team_abbr(db, row.team_id)
        ranking.append({"rank": idx, "team": abbr, "value": round(float(row.val), 1) if row.val is not None else None})
    return {"category": cat, "season_year": args.get("season_year"), "ranking": ranking}


async def _get_team_season_futures(db: AsyncSession, args: dict) -> dict:
    """Team season futures odds from nba.team_props.

    If a team_name is given, returns that team's full futures across books.
    If omitted, returns ALL teams ranked by championship odds from favorite to
    biggest underdog (lowest number = best odds = favorite sorts first).
    """
    season = await _resolve_props_season(db)

    # ----- ranked list of all teams (no team_name) -----
    if not args.get("team_name", "").strip():
        sql = text(
            """SELECT t.name AS team_name, p.bookmaker, p.championship_odds
               FROM nba.team_props p
               JOIN nba.teams t ON t.id = p.team_id
               WHERE p.season_year = :season AND p.championship_odds IS NOT NULL
               ORDER BY p.championship_odds ASC  -- smallest = favorite first
               """
        )
        r = await db.execute(sql, {"season": season})
        rows = r.mappings().all()
        if not rows:
            return {"error": f"No season futures found for season {season}"}

        best: dict[str, dict] = {}
        for row in rows:
            team = row.team_name
            if team not in best or row.championship_odds < best[team]["championship_odds"]:
                best[team] = {
                    "team_name": team,
                    "bookmaker": row.bookmaker,
                    "championship_odds": round(float(row.championship_odds), 1),
                }
        ranking = sorted(best.values(), key=lambda t: t["championship_odds"])
        favorite = ranking[0]["team_name"] if ranking else None
        return {
            "season": season,
            "note": "Ranked by championship odds, lowest (best) odds first = favorite. Only the best book price per team shown.",
            "favorite": favorite,
            "team_count": len(ranking),
            "ranking": ranking,
        }

    # ----- single-team lookup -----
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}

    sql = text(
        """SELECT * FROM nba.team_props
        WHERE team_id = :tid AND season_year = :season
        ORDER BY scraped_at DESC LIMIT 20"""
    )
    r = await db.execute(sql, {"tid": tid, "season": season})
    rows = r.mappings().all()
    if not rows:
        return {"error": f"No season futures found for team id {tid}"}

    def f(v):
        return round(float(v), 1) if v is not None else None

    by_book = {}
    for row in rows:
        bm = row.bookmaker or "?"
        if bm not in by_book:
            by_book[bm] = {"bookmaker": bm, "championship_odds": f(row.championship_odds), "make_playoffs_odds": f(row.make_playoffs_odds), "miss_playoffs_odds": f(row.miss_playoffs_odds), "win_total": f(row.win_total), "win_total_over_odds": f(row.win_total_over_odds), "win_total_under_odds": f(row.win_total_under_odds)}
    return {"season": season, "futures": list(by_book.values())}


async def _get_player_season_props(db: AsyncSession, args: dict) -> dict:
    """Player season award props from nba.player_season_props."""
    name = args.get("player_name", "").strip()
    if not name:
        return {"error": "player_name required"}
    season = await _resolve_props_season(db)

    sql = text(
        """SELECT * FROM nba.player_season_props
        WHERE season_year = :season AND LOWER(player_name) ILIKE :name
        ORDER BY scraped_at DESC"""
    )
    r = await db.execute(sql, {"season": season, "name": f"%{name.lower()}%"})
    rows = r.mappings().all()
    if not rows:
        return {"error": f"No season props found for player '{name}'"}

    seen = {}
    for row in rows:
        key = (row.prop_type, row.bookmaker)
        if key not in seen:
            seen[key] = row
    props = []
    for key, row in seen.items():
        props.append({
            "prop_type": row.prop_type,
            "bookmaker": row.bookmaker,
            "odds": row.odds,
            "implied_probability": round(float(row.implied_probability) * 100, 1) if row.implied_probability is not None else None,
        })
    return {"player_name": name, "season": season, "props": props}


async def _get_game_player_props(db: AsyncSession, args: dict) -> dict:
    """Player prop betting lines (DraftKings) for a specific game.

    Reads nba.player_daily_props (game_id = our internal nba.games.id as
    text). De-dupes repeating lines per (player, prop_type, direction).
    """
    gid = args.get("game_id")
    if not gid:
        return {"error": "game_id required"}
    player = (args.get("player_name") or "").strip()
    prop_type = (args.get("prop_type") or "").strip()

    sql = text(
        """SELECT game_id, player_name, team_id, prop_type, line, odds,
                  direction, bookmaker, scraped_at
           FROM nba.player_daily_props
           WHERE game_id = :gid
           ORDER BY player_name, prop_type, line"""
    )
    r = await db.execute(sql, {"gid": str(gid)})
    rows = r.mappings().all()

    # accent-insensitive player filter
    if player:
        want = _strip_accents(player).lower()
        rows = [x for x in rows if want in _strip_accents(x["player_name"]).lower()]
    if prop_type:
        pt = prop_type.lower()
        rows = [x for x in rows if pt in (x["prop_type"] or "").lower()]

    if not rows:
        return {
            "error": f"No player props found for game {gid}"
            + (f" / player '{player}'" if player else "")
            + " (Props are only ingested during the NBA season)",
        }

    # de-dupe by (player, prop_type, direction, bookmaker) keeping each line
    seen = set()
    props = []
    books_used = set()
    for r in rows:
        books_used.add(r["bookmaker"])
        key = (r["player_name"], r["prop_type"], r["direction"], r["bookmaker"])
        if key in seen:
            continue
        seen.add(key)
        props.append(
            {
                "player": r["player_name"],
                "prop_type": r["prop_type"],
                "line": float(r["line"]) if r["line"] is not None else None,
                "over": r["direction"].lower() == "over",
                "odds": r["odds"],
            }
        )
    return {
        "game_id": gid,
        "prop_count": len(props),
        "books": sorted(books_used),
        "props": props,
    }




# Alias map: model-invoked name variants -> canonical registered tool name.
_TOOL_ALIASES = {
    "get_team_futures": "get_team_season_futures",
    "get_team_future": "get_team_season_futures",
    "get_team_season_future": "get_team_season_futures",
    "get_player_props": "get_player_season_props",
    "get_player_season_prop": "get_player_season_props",
    "get_player_prop_lines": "get_game_player_props",
    "get_game_props": "get_game_player_props",
}


def _normalize_tool_name(name: str) -> str:
    """Map a model-invoked tool name to its canonical registered name."""
    if not name:
        return name
    return _TOOL_ALIASES.get(name.strip(), name.strip())




async def _get_game_writeup(db: AsyncSession, args: dict) -> dict:
    """Return Earl's published write-up (public + premium + props) for a game."""
    gid = args.get("game_id")
    if not gid:
        return {"error": "game_id required"}

    sql = text("""
        SELECT w.title, w.status, g.date,
               ht.name AS home_team, at2.name AS away_team,
               w.public_content, w.premium_content,
               w.prop_title, w.prop_content, w.published_at
        FROM nba.game_writeups w
        JOIN nba.games g ON g.id = w.game_id
        JOIN nba.teams ht ON ht.id = g.home_team_id
        JOIN nba.teams at2 ON at2.id = g.away_team_id
        WHERE w.game_id = :gid
    """)
    row = (await db.execute(sql, {"gid": gid})).mappings().first()
    if not row:
        return {"error": f"No write-up found for game {gid}"}
    return {
        "game_id": gid,
        "home_team": row.home_team,
        "away_team": row.away_team,
        "date": str(row.date) if row.date else None,
        "title": row.title,
        "status": row.status,
        "published_at": str(row.published_at) if row.published_at else None,
        "public_content": row.public_content,
        "premium_content": row.premium_content,
        "prop_title": row.prop_title,
        "prop_content": row.prop_content,
    }

_TOOL_HANDLERS = {
    "get_team_info": _get_team_info,
    "get_team_stats": _get_team_stats,
    "get_standings": _get_standings,
    "get_todays_games": _get_todays_games,
    "get_game_info": _get_game_info,
    "get_game_writeup": _get_game_writeup,
    "get_head_to_head": _get_head_to_head,
    "get_player_stats": _get_player_stats,
    "get_player_game_logs": _get_player_game_logs,
    "get_player_split_stats": _get_player_split_stats,
    "get_team_split_stats": _get_team_split_stats,
    "get_game_prediction": _get_game_prediction,
    "search_articles": _search_articles,
    "get_team_schedule": _get_team_schedule,
    "get_team_trends": _get_team_trends,
    "get_team_comparison": _get_team_comparison,
    "get_team_rankings": _get_team_rankings,
    "get_team_season_futures": _get_team_season_futures,
    "get_player_season_props": _get_player_season_props,
    "get_game_player_props": _get_game_player_props,
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
    func_name = _normalize_tool_name(tool_call.function.name)
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
