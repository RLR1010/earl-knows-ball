"""NFL-specific tool definitions and executors for the tool-calling chat engine.

All raw SQL queries use actual nfl schema column names (verified against the DB).
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone as dt_timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nfl import Team, Player, DepthChart

logger = logging.getLogger("earl.chat_tools.nfl")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _today_chicago() -> date:
    return datetime.now(dt_timezone(timedelta(hours=-5))).date()  # CDT


async def _resolve_season_year(db: AsyncSession) -> int:
    """Return the year of the most recent completed NFL season."""
    r = await db.execute(text(
        "SELECT MAX(s.year) FROM nfl.seasons s "
        "JOIN nfl.games g ON g.season_id = s.id "
        "WHERE g.status = 'FINAL'"
    ))
    val = r.scalar_one_or_none()
    if val is None:
        raise ValueError("No NFL seasons found")
    return val


async def _resolve_props_season(db: AsyncSession) -> int:
    """Return the most recent season for which futures/props exist (upcoming or current)."""
    r = await db.execute(text(
        "SELECT GREATEST(COALESCE(MAX(season_year), 0), 0) FROM nfl.team_props"
    ))
    val = r.scalar_one_or_none()
    if not val:
        r2 = await db.execute(text(
            "SELECT GREATEST(COALESCE(MAX(season_year), 0), 0) FROM nfl.player_season_props"
        ))
        val = r2.scalar_one_or_none()
    return int(val or await _resolve_season_year(db))


async def _resolve_season_id(db: AsyncSession, year: int | None = None) -> int:
    """Return the season id for the given year (default: latest)."""
    if year is None:
        year = await _resolve_season_year(db)
    r = await db.execute(
        text("SELECT id FROM nfl.seasons WHERE year = :y"), {"y": year}
    )
    val = r.scalar_one_or_none()
    if val is None:
        raise ValueError(f"No NFL season found for year {year}")
    return val


async def _resolve_team_id(db: AsyncSession, name_or_abbr: str) -> int | None:
    """Resolve team name/abbreviation/location to a team id."""
    clean = name_or_abbr.strip().lower()
    # Exact matches
    for col in ("abbreviation", "name"):
        r = await db.execute(
            text(f"SELECT id FROM nfl.teams WHERE LOWER({col}) = :q"),
            {"q": clean},
        )
        tid = r.scalar_one_or_none()
        if tid:
            return tid
    # Partial fallback
    r = await db.execute(
        text("SELECT id FROM nfl.teams WHERE LOWER(name) LIKE :q OR LOWER(abbreviation) LIKE :q"),
        {"q": f"%{clean}%"},
    )
    return r.scalar_one_or_none()


async def _resolve_team_name(db: AsyncSession, tid: int) -> str | None:
    r = await db.execute(
        text("SELECT name FROM nfl.teams WHERE id = :tid"), {"tid": tid}
    )
    return r.scalar_one_or_none()


async def _resolve_team_abbr(db: AsyncSession, name_or_abbr: str) -> str | None:
    """Resolve team name/abbreviation/location to an abbreviation string."""
    tid = await _resolve_team_id(db, name_or_abbr)
    if tid is None:
        return None
    r = await db.execute(
        text("SELECT abbreviation FROM nfl.teams WHERE id = :tid"), {"tid": tid}
    )
    return r.scalar_one_or_none()


async def _resolve_player_id(db: AsyncSession, player_name: str) -> int | None:
    """Resolve a player name to their id in nfl.players."""
    clean = player_name.strip()
    parts = clean.lower().split(" ", 1)
    r = await db.execute(
        text(
            "SELECT id FROM nfl.players WHERE LOWER(name) = :full "
            "OR LOWER(name) LIKE :first_last ORDER BY id LIMIT 1"
        ),
        {
            "full": clean.lower(),
            "first_last": f"{parts[0]}% {parts[-1]}%" if len(parts) > 1 else f"%{parts[0]}%",
        },
    )
    return r.scalar_one_or_none()


# ─── Tool Definitions ────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_team_info",
            "description": "Get basic info about an NFL team: name, abbreviation, location, conference, division, bye week, stadium.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {
                        "type": "string",
                        "description": "Team name or abbreviation (e.g., 'Chicago Bears', 'CHI', 'Packers')",
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
            "description": "Get season stats for an NFL team: PPG, OPPG, recent form record from completed games.",
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
            "description": "Get NFL standings: wins, losses, win pct for each team, grouped by conference and division.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_todays_games",
            "description": "Get all NFL games scheduled on a given date, including status, score. CRITICAL: Only pass game_date if the user SPECIFICALLY asks about a different date. For 'today' or 'this week' queries, OMIT game_date so it uses the correct America/Chicago date.",
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
            "name": "get_week_games",
            "description": "Get all NFL games for a specific week and season.",
            "parameters": {
                "type": "object",
                "properties": {
                    "week": {
                        "type": "integer",
                        "description": "NFL week (1-18 regular season, 19+ playoffs). Defaults to current week.",
                    },
                    "season_year": {
                        "type": "integer",
                        "description": "Season year (e.g., 2025). Defaults to current.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_game_info",
            "description": "Get detailed info about a specific NFL game: score, betting lines (spread, OU, ML), venue, roof type, status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_id": {
                        "type": "integer",
                        "description": "Game ID from get_todays_games, get_week_games, or other query.",
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
            "description": "Get head-to-head results between two NFL teams: recent meetings, scores.",
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
            "name": "get_injuries",
            "description": "Get injury report for an NFL team: player, position, injury type, practice/game status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation"},
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_depth_chart",
            "description": "Get the depth chart for an NFL team: positions and players ordered by depth slot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation"},
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_stats",
            "description": "Get season or weekly stats for an NFL player by name: passing, rushing, receiving.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player full name (e.g., 'Patrick Mahomes', 'Justin Jefferson')"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_weekly_log",
            "description": "Get a player's game-by-game weekly stats for a season. Great for seeing consistency and trends.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player full name"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_game_prediction",
            "description": "Get Earl's model prediction for an NFL game: ATS pick, O/U pick, moneyline with confidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_id": {"type": "integer", "description": "Game ID from get_todays_games or get_week_games."},
                },
                "required": ["game_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_articles",
            "description": "Search NFL news articles by semantic similarity. Filters by date range when provided. Returns titles, summaries, source, dates.",
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
            "description": "Get the full schedule (upcoming and past games) for an NFL team in a season.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                    "limit": {"type": "integer", "description": "Games to return (default 10, max 17)"},
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_trends",
            "description": "Get a team's recent performance trends (offense/defense, points, yards, ATS and O/U cover rates, streaks) over the last 3, 5, and 10 games from rolling stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                    "window": {"type": "string", "enum": ["3", "5", "10", "all"], "description": "Trend window: '3', '5', '10', or 'all' (default 'all')"},
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_defense_rankings",
            "description": "Rank all NFL defenses by a given category (e.g. points allowed, yards allowed, passing yards allowed, rushing yards allowed) using cumulative game stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["ppg_allowed", "ypg_allowed", "pass_ypg_allowed", "rush_ypg_allowed", "sacks", "interceptions"], "description": "Defense ranking category"},
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
            "name": "get_team_comparison",
            "description": "Compare two NFL teams side by side on offense and defense (PPG, yards/game, EPA/play, turnover differential) using cumulative game stats.",
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
            "name": "get_qb_stats",
            "description": "Get a quarterback's season cumulative stats (completions, yards, TDs, INTs, passer rating, ANY/A, rushing contribution).",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "QB player name"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_qb_trends",
            "description": "Get a quarterback's recent form over the last 3, 5, and 10 games (passer rating, ANY/A, TD/INT, yards) from rolling stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "QB player name"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_season_futures",
            "description": "Get a team's season-long futures odds (to win championship, make/miss playoffs, regular season win total, over/under).",
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
            "name": "get_player_season_props",
            "description": "Get season-long award/stat props for a player (e.g. MVP, Offensive Player of the Year, rushing leader) with odds and implied probability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player name"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                },
                "required": ["player_name"],
            },
        },
    },
]


# ─── Tool Implementations ─────────────────────────────────────────────────────

async def _get_team_info(db: AsyncSession, args: dict) -> dict:
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    r = await db.execute(
        text("SELECT * FROM nfl.teams WHERE id = :tid"), {"tid": tid}
    )
    row = r.mappings().first()
    if not row:
        return {"error": "Team not found"}
    return {
        "id": row.id,
        "name": row.name,
        "abbreviation": row.abbreviation,
        "conference": row.conference,
        "division": row.division,
        "bye_week": row.byeweek,
        "stadium": row.stadium if "stadium" in row else None,
    }


async def _get_team_stats(db: AsyncSession, args: dict) -> dict:
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    year = args.get("season_year") or await _resolve_season_year(db)

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
        FROM nfl.games g
        JOIN nfl.seasons s ON s.id = g.season_id
        WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
          AND s.year = :year
          AND g.game_type = 'REG'
          AND g.status = 'FINAL'
    """)
    r = await db.execute(sql, {"tid": tid, "year": year})
    row = r.mappings().first()
    if not row or row.total_games == 0:
        return {"error": "No game data found"}

    # Recent form (last 5)
    form_sql = text("""
        SELECT CASE
            WHEN (g.home_team_id = :tid AND g.home_score > g.away_score)
              OR (g.away_team_id = :tid AND g.away_score > g.home_score)
            THEN 'W' ELSE 'L' END AS result
        FROM nfl.games g
        JOIN nfl.seasons s ON s.id = g.season_id
        WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
          AND s.year = :year AND g.status = 'FINAL'
        ORDER BY g.date DESC LIMIT 5
    """)
    fr = await db.execute(form_sql, {"tid": tid, "year": year})
    form = "".join(r.result for r in fr.mappings())

    return {
        "record": f"{row.wins}-{row.losses}",
        "ppg": round(float(row.ppg or 0), 1),
        "oppg": round(float(row.oppg or 0), 1),
        "point_diff": round(float((row.ppg or 0) - (row.oppg or 0)), 1),
        "total_games": row.total_games,
        "recent_form": form,
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
        FROM nfl.teams t
        LEFT JOIN nfl.games g ON (g.home_team_id = t.id OR g.away_team_id = t.id)
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
        FROM nfl.games g
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at2 ON at2.id = g.away_team_id
        WHERE g.date::date = :d
        ORDER BY g.date ASC
    """)
    r = await db.execute(sql, {"d": parsed})
    games = []
    for row in r.mappings():
        games.append({
            "game_id": row.id,
            "week": row.week,
            "home_team": row.home_name,
            "away_team": row.away_name,
            "home_score": row.home_score,
            "away_score": row.away_score,
            "status": row.status,
            "time": str(row.date) if row.date else None,
        })
    return {"date": str(parsed), "games": games}


async def _get_week_games(db: AsyncSession, args: dict) -> dict:
    year = args.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)
    week = args.get("week")

    if not week:
        r = await db.execute(
            text("SELECT MAX(week) FROM nfl.games WHERE season_id = :sid AND status = 'FINAL'"),
            {"sid": sid},
        )
        week = r.scalar_one_or_none() or 1

    sql = text("""
        SELECT g.*, ht.name AS home_name, at2.name AS away_name
        FROM nfl.games g
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at2 ON at2.id = g.away_team_id
        WHERE g.season_id = :sid AND g.week = :week
        ORDER BY g.date ASC
    """)
    r = await db.execute(sql, {"sid": sid, "week": week})
    games = []
    for row in r.mappings():
        games.append({
            "game_id": row.id,
            "home_team": row.home_name,
            "away_team": row.away_name,
            "home_score": row.home_score,
            "away_score": row.away_score,
            "status": row.status,
            "date": str(row.date) if row.date else None,
            "roof_type": row.roof_type,
        })
    return {"season_year": year, "week": week, "games": games}


async def _get_game_info(db: AsyncSession, args: dict) -> dict:
    gid = args.get("game_id")

    sql = text("""
        SELECT g.*, ht.name AS home_name, at2.name AS away_name,
               gl.opening_spread, gl.closing_spread,
               gl.opening_ou, gl.closing_ou,
               gl.opening_home_ml, gl.closing_home_ml,
               gl.opening_away_ml, gl.closing_away_ml,
               gl.closing_home_implied_probability,
               gl.closing_away_implied_probability
        FROM nfl.games g
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at2 ON at2.id = g.away_team_id
        LEFT JOIN nfl.betting_lines_consolidated gl ON gl.game_id = g.id
        WHERE g.id = :gid
    """)
    r = await db.execute(sql, {"gid": gid})
    row = r.mappings().first()
    if not row:
        return {"error": f"Game not found: {gid}"}

    info = {
        "game_id": row.id,
        "week": row.week,
        "date": str(row.date) if row.date else None,
        "home_team": row.home_name,
        "away_team": row.away_name,
        "home_score": row.home_score,
        "away_score": row.away_score,
        "status": row.status,
        "venue": row.venue,
        "roof_type": row.roof_type,
    }
    if row.opening_spread is not None:
        info["betting_lines"] = {
            "opening_spread": str(row.opening_spread),
            "closing_spread": str(row.closing_spread),
            "opening_ou": str(row.opening_ou),
            "closing_ou": str(row.closing_ou),
            "opening_home_ml": str(row.opening_home_ml),
            "closing_home_ml": str(row.closing_home_ml),
            "opening_away_ml": str(row.opening_away_ml),
            "closing_away_ml": str(row.closing_away_ml),
            "home_implied_prob": round(float(row.closing_home_implied_probability or 0) * 100, 1),
            "away_implied_prob": round(float(row.closing_away_implied_probability or 0) * 100, 1),
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
        FROM nfl.games g
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at2 ON at2.id = g.away_team_id
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


async def _get_injuries(db: AsyncSession, args: dict) -> dict:
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}

    sql = text("""
        SELECT i.*, p.name AS player_name, p.position
        FROM nfl.injuries i
        JOIN nfl.players p ON p.id = i.player_id
        WHERE p.team_id = :tid
        ORDER BY i.game_status DESC, i.injury_type
    """)
    r = await db.execute(sql, {"tid": tid})
    injuries = []
    for row in r.mappings():
        injuries.append({
            "player": row.player_name,
            "position": row.position,
            "injury": row.injury_type,
            "practice_status": row.practice_status,
            "game_status": row.game_status,
            "updated": str(row.date_reported) if row.date_reported else None,
        })
    return {"injuries": injuries}


async def _get_depth_chart(db: AsyncSession, args: dict) -> dict:
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}

    stmt = select(DepthChart).where(
        DepthChart.team_id == tid
    ).order_by(DepthChart.position, DepthChart.slot)
    r = await db.execute(stmt)
    entries = []
    for dc in r.scalars():
        entries.append({
            "position": dc.position,
            "player": dc.player_name,
            "depth_slot": dc.slot,
        })
    return {"depth_chart": entries}


async def _get_player_stats(db: AsyncSession, args: dict) -> dict:
    player_name = args.get("player_name", "")
    year = args.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    # Find player by name
    clean = player_name.strip()
    parts = clean.lower().split(" ", 1)
    if len(parts) == 2:
        stmt = select(Player).where(
            Player.name.ilike(f"{parts[0]}% {parts[1]}%"),
        )
    else:
        stmt = select(Player).where(Player.name.ilike(f"%{parts[0]}%"))
    r = await db.execute(stmt)
    player = r.scalar_one_or_none()
    if not player:
        return {"error": f"Player not found: {player_name}"}

    sql = text("""
        SELECT
            COUNT(*) AS games_played,
            COALESCE(SUM(pass_yards), 0) AS pass_yds,
            COALESCE(SUM(pass_tds), 0) AS pass_td,
            COALESCE(SUM(pass_int), 0) AS ints,
            COALESCE(SUM(rush_attempts), 0) AS rush_att,
            COALESCE(SUM(rush_yards), 0) AS rush_yds,
            COALESCE(SUM(rush_tds), 0) AS rush_td,
            COALESCE(SUM(receptions), 0) AS rec,
            COALESCE(SUM(receiving_yards), 0) AS rec_yds,
            COALESCE(SUM(receiving_tds), 0) AS rec_td
        FROM nfl.player_weekly_stats
        WHERE player_id = :pid AND season_id = :sid
    """)
    r = await db.execute(sql, {"pid": player.id, "sid": sid})
    s = r.mappings().first()
    if not s or s.games_played == 0:
        return {"error": f"No stats for {player.name} in {year}"}

    return {
        "player": player.name,
        "position": player.position,
        "season_year": year,
        "games_played": s.games_played,
        "passing": {"yards": s.pass_yds, "tds": s.pass_td, "ints": s.ints},
        "rushing": {"attempts": s.rush_att, "yards": s.rush_yds, "tds": s.rush_td},
        "receiving": {"receptions": s.rec, "yards": s.rec_yds, "tds": s.rec_td},
    }


async def _get_player_weekly_log(db: AsyncSession, args: dict) -> dict:
    player_name = args.get("player_name", "")
    year = args.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    clean = player_name.strip().lower()
    parts = clean.split(" ", 1)
    if len(parts) == 2:
        stmt = select(Player).where(
            Player.name.ilike(f"{parts[0]}% {parts[1]}%"),
        )
    else:
        stmt = select(Player).where(Player.name.ilike(f"%{parts[0]}%"))
    r = await db.execute(stmt)
    player = r.scalar_one_or_none()
    if not player:
        return {"error": f"Player not found: {player_name}"}

    sql = text("""
        SELECT pws.*, g.week,
               ht.name AS opponent_name,
               CASE WHEN g.home_team_id = pws.team_id THEN 'home' ELSE 'away' END AS venue
        FROM nfl.player_weekly_stats pws
        JOIN nfl.games g ON g.id = pws.game_id
        LEFT JOIN nfl.teams ht ON ht.id = CASE
            WHEN g.home_team_id = pws.team_id THEN g.away_team_id
            ELSE g.home_team_id END
        WHERE pws.player_id = :pid AND pws.season_id = :sid
        ORDER BY g.week ASC
    """)
    r = await db.execute(sql, {"pid": player.id, "sid": sid})
    games = []
    for row in r.mappings():
        games.append({
            "week": row.week,
            "opponent": row.opponent_name,
            "venue": row.venue,
            "pass_yds": row.pass_yards,
            "pass_td": row.pass_tds,
            "rush_att": row.rush_attempts,
            "rush_yds": row.rush_yards,
            "rush_td": row.rush_tds,
            "rec": row.receptions,
            "targets": row.targets,
            "rec_yds": row.receiving_yards,
            "rec_td": row.receiving_tds,
        })
    return {"player": player.name, "season_year": year, "game_logs": games}


async def _get_team_trends(db: AsyncSession, args: dict) -> dict:
    """Recent performance trends from nfl.team_rolling_stats."""
    abbr = await _resolve_team_abbr(db, args.get("team_name", ""))
    if not abbr:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    season = args.get("season_year") or await _resolve_season_year(db)
    window = (args.get("window") or "all").strip().lower()

    suffixes = ["3", "5", "10"]
    if window in suffixes:
        suffixes = [window]

    # Metrics available per rolling window. r10 only exists for a subset of columns.
    metric_all = ["off_pts", "off_yds", "pass_yds", "rush_yds", "ypp", "def_pts", "def_yds", "point_diff", "yardage_diff", "turnover_margin", "win_pct", "cover_pct", "ou_over_pct", "margin", "ou_margin", "ats_margin"]
    metric_r10_subset = {"off_pts", "off_yds", "pass_yds", "rush_yds", "ypp", "def_pts", "def_yds", "point_diff", "yardage_diff", "turnover_margin", "win_pct", "cover_pct", "ou_over_pct", "margin", "ou_margin", "ats_margin"}

    selected = []
    for sfx in suffixes:
        for m in metric_all:
            if sfx == "10" and m not in metric_r10_subset:
                continue
            selected.append(f"{m}_r{sfx}")

    sql = text(
        f"""SELECT game_id, week, game_date, is_home, games_played, season_wins, season_losses, season_win_pct, win_streak, loss_streak, cover_streak, season_ats_pct, season_ou_over_pct, {', '.join(selected)}
        FROM nfl.team_rolling_stats
        WHERE team_abbr = :abbr AND season = :season
        ORDER BY game_date DESC LIMIT 20"""
    )
    r = await db.execute(sql, {"abbr": abbr, "season": season})
    rows = r.mappings().all()
    if not rows:
        return {"error": f"No rolling stats found for {abbr} in {season}"}

    def f(v):
        return round(float(v), 1) if v is not None else None

    games = []
    for row in rows[:5]:
        g = {"week": row.week, "date": str(row.game_date)[:10], "home": bool(row.is_home), "games_played": row.games_played}
        for col in selected:
            g[col] = f(row[col])
        games.append(g)

    latest = rows[0]
    summary = {"season_wins": latest.season_wins, "season_losses": latest.season_losses, "season_win_pct": f(latest.season_win_pct), "win_streak": latest.win_streak, "lose_streak": latest.loss_streak, "cover_streak": latest.cover_streak, "season_ats_pct": f(latest.season_ats_pct), "season_ou_over_pct": f(latest.season_ou_over_pct)}
    for col in selected:
        summary[col] = f(latest[col])

    return {"team_abbr": abbr, "season": season, "windows": suffixes, "latest_summary": summary, "recent_games": games}


async def _get_team_defense_rankings(db: AsyncSession, args: dict) -> dict:
    """Rank NFL defenses by category from cumulative_game_stats."""
    cat = args.get("category", "ppg_allowed")
    season = args.get("season_year") or await _resolve_season_year(db)
    limit = min(args.get("limit", 10), 32)

    # Map friendly category to the cumulative_table column. Lower is better for
    # the *_allowed / *_allowed-style columns; sacks/interceptions are higher-better.
    allowed_cats = {"ppg_allowed": "def_ppg_allowed", "ypg_allowed": "def_ypg_allowed", "pass_ypg_allowed": "def_pass_ypg_allowed", "rush_ypg_allowed": "def_rush_ypg_allowed"}
    high_wins = {"sacks": "def_sacks", "interceptions": "def_interceptions"}

    if cat in allowed_cats:
        col = allowed_cats[cat]
        order = "ASC"
    elif cat in high_wins:
        col = high_wins[cat]
        order = "DESC"
    else:
        return {"error": f"Unknown category: {cat}. Use one of: {list(allowed_cats) + list(high_wins)}"}

    sql = text(
        f"""SELECT team_abbr, {col} AS val
        FROM nfl.cumulative_game_stats
        WHERE season = :season
        GROUP BY team_abbr, {col}
        ORDER BY val {order} NULLS LAST
        LIMIT :limit"""
    )
    r = await db.execute(sql, {"season": season, "limit": limit})
    rows = r.mappings().all()
    ranking = []
    for idx, row in enumerate(rows, 1):
        ranking.append({"rank": idx, "team_abbr": row.team_abbr, "value": round(float(row.val), 1) if row.val is not None else None})
    return {"category": cat, "season": season, "ranking": ranking}


async def _get_team_comparison(db: AsyncSession, args: dict) -> dict:
    """Side-by-side team comparison from cumulative_game_stats."""
    abbr_a = await _resolve_team_abbr(db, args.get("team_a", ""))
    abbr_b = await _resolve_team_abbr(db, args.get("team_b", ""))
    if not abbr_a or not abbr_b:
        missing = [args.get("team_a"), args.get("team_b")] if not abbr_a else [args.get("team_b")]
        return {"error": f"Team(s) not found: {', '.join(missing)}"}
    season = args.get("season_year") or await _resolve_season_year(db)

    metrics = {
        "off_ppg": "Offense PPG",
        "off_ypg": "Offense YPG",
        "off_pass_ypg": "Pass YPG",
        "off_rush_ypg": "Rush YPG",
        "off_epa_per_play": "Off EPA/Play",
        "def_ppg_allowed": "Defense PPG Allowed",
        "def_ypg_allowed": "Defense YPG Allowed",
        "def_epa_per_play": "Def EPA/Play",
        "turnover_margin_avg": "Turnover Differential (avg)",
    }
    cols = ", ".join(metrics.keys())
    sql = text(
        f"""SELECT team_abbr, {cols} FROM nfl.cumulative_game_stats
        WHERE team_abbr IN (:a, :b) AND season = :season"""
    )
    r = await db.execute(sql, {"a": abbr_a, "b": abbr_b, "season": season})
    rows = r.mappings().all()
    if len(rows) < 2:
        return {"error": f"Comparison data not found for {abbr_a} vs {abbr_b} in {season}"}

    dm = {row.team_abbr: row for row in rows}
    ra, rb = dm[abbr_a], dm[abbr_b]
    result = {"season": season, "compare": {}}
    for col, label in metrics.items():
        va = getattr(ra, col)
        vb = getattr(rb, col)
        result["compare"][label] = {
            abbr_a: round(float(va), 1) if va is not None else None,
            abbr_b: round(float(vb), 1) if vb is not None else None,
        }
    return result


async def _get_qb_stats(db: AsyncSession, args: dict) -> dict:
    """Season cumulative QB stats from nfl.qb_cumulative_stats."""
    pid = await _resolve_player_id(db, args.get("player_name", ""))
    if not pid:
        return {"error": f"Player not found: {args.get('player_name', '')}"}
    season = args.get("season_year") or await _resolve_season_year(db)

    sql = text(
        """SELECT * FROM nfl.qb_cumulative_stats
        WHERE player_id = :pid AND season = :season
        ORDER BY week DESC LIMIT 1"""
    )
    r = await db.execute(sql, {"pid": pid, "season": season})
    row = r.mappings().first()
    if not row:
        return {"error": f"No cumulative QB stats found for season {season}"}

    def f(v):
        return round(float(v), 1) if v is not None else None

    return {
        "season": season,
        "team_abbr": row.team_abbr,
        "games_played": row.games_played,
        "passing": {"yards": f(row.cum_pass_yds), "td": f(row.cum_pass_td), "int": f(row.cum_pass_int), "attempts": f(row.cum_pass_att), "completions": f(row.cum_pass_comp), "comp_pct": f(row.comp_pct), "ypa": f(row.ypa), "td_pct": f(row.td_pct), "int_pct": f(row.int_pct), "any_a": f(row.any_a), "passer_rating": f(row.passer_rating_cum)},
        "rushing": {"attempts": f(row.cum_rush_att), "yards": f(row.cum_rush_yds), "td": f(row.cum_rush_td)},
        "sacks": f(row.cum_sacks),
        "fumbles": f(row.cum_fumbles),
        "sack_rate": f(row.sack_rate),
    }


async def _get_qb_trends(db: AsyncSession, args: dict) -> dict:
    """QB recent form from nfl.qb_rolling_stats (3/5/10-game windows)."""
    pid = await _resolve_player_id(db, args.get("player_name", ""))
    if not pid:
        return {"error": f"Player not found: {args.get('player_name', '')}"}
    season = args.get("season_year") or await _resolve_season_year(db)

    sql = text(
        """SELECT week, game_date, team_abbr, opponent_abbr,
              comp_pct_3, ypa_3, any_a_3, passer_rating_3, td_pct_3, int_pct_3, games_3,
              comp_pct_5, ypa_5, any_a_5, passer_rating_5, td_pct_5, int_pct_5, games_5,
              comp_pct_10, ypa_10, any_a_10, passer_rating_10, td_pct_10, int_pct_10, games_10
           FROM nfl.qb_rolling_stats
           WHERE player_id = :pid AND season = :season
           ORDER BY week DESC LIMIT 1"""
    )
    r = await db.execute(sql, {"pid": pid, "season": season})
    row = r.mappings().first()
    if not row:
        return {"error": f"No rolling QB stats found for season {season}"}

    def f(v):
        return round(float(v), 1) if v is not None else None

    out = {"season": season, "team_abbr": row.team_abbr, "latest_week": row.week, "date": str(row.game_date)[:10], "opponent": row.opponent_abbr}
    for w in ("3", "5", "10"):
        out[f"last_{w}_games"] = {
            "games": row[f"games_{w}"],
            "comp_pct": f(row[f"comp_pct_{w}"]),
            "ypa": f(row[f"ypa_{w}"]),
            "any_a": f(row[f"any_a_{w}"]),
            "passer_rating": f(row[f"passer_rating_{w}"]),
            "td_pct": f(row[f"td_pct_{w}"]),
            "int_pct": f(row[f"int_pct_{w}"]),
        }
    return out


async def _get_team_season_futures(db: AsyncSession, args: dict) -> dict:
    """Team season futures odds from nfl.team_props."""
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    season = args.get("season_year") or await _resolve_props_season(db)

    sql = text(
        """SELECT * FROM nfl.team_props
        WHERE team_id = :tid AND season_year = :season
        ORDER BY scraped_at DESC LIMIT 1"""
    )
    r = await db.execute(sql, {"tid": tid, "season": season})
    row = r.mappings().first()
    if not row:
        return {"error": f"No season futures found for season {season}"}

    def f(v):
        return round(float(v), 1) if v is not None else None

    out = {"season": season, "bookmaker": row.bookmaker}
    for key, label in [("championship_odds", "championship_odds"), ("make_playoffs_odds", "make_playoffs_odds"), ("miss_playoffs_odds", "miss_playoffs_odds")]:
        v = getattr(row, key)
        out[label] = f(v)
    out["win_total"] = f(row.win_total)
    out["win_total_over_odds"] = f(row.win_total_over_odds)
    out["win_total_under_odds"] = f(row.win_total_under_odds)
    return out


async def _get_player_season_props(db: AsyncSession, args: dict) -> dict:
    """Season award/stat props for a player from nfl.player_season_props."""
    season = args.get("season_year") or await _resolve_props_season(db)
    name = args.get("player_name", "").strip()
    if not name:
        return {"error": "player_name required"}

    sql = text(
        """SELECT * FROM nfl.player_season_props
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


async def _get_game_prediction(db: AsyncSession, args: dict) -> dict:
    gid = args["game_id"]

    # game_predictions has game_id -> nfl.games.id, which has home/away_team_id -> nfl.teams.id
    sql = text("""
        SELECT gp.*, ht.name AS home_name, at.name AS away_name
        FROM nfl.game_predictions gp
        JOIN nfl.games g ON g.id = gp.game_id
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at ON at.id = g.away_team_id
        WHERE gp.game_id = :gid
        LIMIT 1
    """)
    r = await db.execute(sql, {"gid": gid})
    row = r.mappings().first()
    if not row:
        return {"error": f"No prediction found for game {gid}"}
    home_name = row.home_name
    away_name = row.away_name

    pred = {
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
    return pred


async def _search_articles(db: AsyncSession, args: dict) -> dict:
    """Search NFL articles via pgvector semantic search with optional date filter."""
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
        db, query, sport="nfl", top_k=limit,
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
    lim = min(args.get("limit", 10), 17)

    sql = text("""
        SELECT g.*, ht.name AS home_name, at2.name AS away_name
        FROM nfl.games g
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at2 ON at2.id = g.away_team_id
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
            "week": row.week,
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
    "get_week_games": _get_week_games,
    "get_game_info": _get_game_info,
    "get_head_to_head": _get_head_to_head,
    "get_injuries": _get_injuries,
    "get_depth_chart": _get_depth_chart,
    "get_player_stats": _get_player_stats,
    "get_player_weekly_log": _get_player_weekly_log,
    "get_game_prediction": _get_game_prediction,
    "search_articles": _search_articles,
    "get_team_schedule": _get_team_schedule,
    "get_team_trends": _get_team_trends,
    "get_team_defense_rankings": _get_team_defense_rankings,
    "get_team_comparison": _get_team_comparison,
    "get_qb_stats": _get_qb_stats,
    "get_qb_trends": _get_qb_trends,
    "get_team_season_futures": _get_team_season_futures,
    "get_player_season_props": _get_player_season_props,
}


async def execute_nfl_tool(db: AsyncSession, tool_call) -> str:
    """Execute an NFL tool call and return a JSON result string.

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
        logger.warning("Unknown NFL tool: %s", func_name)
        return json.dumps({"error": f"Unknown tool: {func_name}"})

    logger.info("NFL tool: %s args=%s", func_name, args)
    try:
        # Use a savepoint so failures don't abort the outer transaction
        async with db.begin_nested():
            result = await handler(db, args)
        return json.dumps(result, default=str, ensure_ascii=False)
    except Exception as e:
        logger.exception("Error in NFL tool %s: %s", func_name, e)
        try:
            await db.rollback()
        except Exception:
            pass
        return json.dumps({"error": f"Error executing {func_name}: {str(e)}"})
