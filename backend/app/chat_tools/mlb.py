"""MLB-specific tool definitions and executors for the tool-calling chat engine.

Exports:
    TOOL_DEFINITIONS: List of OpenAI function-calling schemas.
    execute_mlb_tool: Async dispatcher that runs the right DB query.
"""

import difflib
import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone as dt_timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mlb import (
    MLBTeam,
    MLBPlayer,
    MLBSeason,
    MLBGamePrediction,
    MLBTeamSplit,
    MLBPlayerSplit,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling schema)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_teams",
            "description": "Search for MLB teams by name, abbreviation, or city. Returns matching teams with IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Team name (Rangers, Yankees), abbreviation (TEX, NYY), or city (Chicago, New York)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_stats",
            "description": "Get season stats for a specific team: record, runs scored/allowed, home/road splits, recent form.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {
                        "type": "string",
                        "description": "Full team name (e.g., 'Texas Rangers', 'Chicago Cubs')",
                    },
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_batting_stats",
            "description": "Get season batting stats for a team: BA, OBP, SLG, OPS, HR, RBI, runs, SB, and more. Aggregated from all players on the team.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {
                        "type": "string",
                        "description": "Full team name (e.g., 'Texas Rangers')",
                    },
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_pitching_stats",
            "description": "Get season pitching stats for a team: ERA, WHIP, K/9, BB/9, HR/9, BABIP, and bullpen stats. Aggregated from all pitchers on the team.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {
                        "type": "string",
                        "description": "Full team name (e.g., 'Texas Rangers')",
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
            "description": "Get current MLB standings with win/loss records, win pct, and division/conference info.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_todays_games",
            "description": "Get all MLB games scheduled on a given date. Games at 00:00-05:00 UTC are 'tonight' games from US timezones. CRITICAL: Only pass game_date if the user SPECIFICALLY asks about a different date. For 'today' or 'tonight' queries, OMIT game_date entirely so the function uses the correct America/Chicago date.",
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
            "description": "Get detailed info about a specific game: score, starting pitchers, betting lines, venue.",
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
            "description": "Get head-to-head results between two teams in the current season.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team1": {
                        "type": "string",
                        "description": "First team name (e.g., 'New York Yankees')",
                    },
                    "team2": {
                        "type": "string",
                        "description": "Second team name (e.g., 'Boston Red Sox')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent meetings to return (default 10, max 20)",
                    },
                },
                "required": ["team1", "team2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_injuries",
            "description": "Get injury report for a specific team: player name, injury type, IL status, return timeline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {
                        "type": "string",
                        "description": "Full team name (e.g., 'Los Angeles Dodgers')",
                    },
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_stats",
            "description": "Get batting or pitching stats for a specific player by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {
                        "type": "string",
                        "description": "Player full name (e.g., 'Shohei Ohtani', 'Aaron Judge')",
                    },
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_split_stats",
            "description": "Get a batter's split statistics for prop-bet research and platoon analysis: vs left-handed pitchers, vs right-handed pitchers, home/away, day/night, and per-city (venue) splits. Returns current-season and career numbers (AVG/OBP/SLG/OPS, PA, HR, RBI, BB, K). Use this to answer questions like 'how does this hitter perform vs LHP' or 'what are his numbers at this ballpark city'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {
                        "type": "string",
                        "description": "Player full name (e.g., 'Jose Ramirez', 'Aaron Judge')",
                    },
                    "split_type": {
                        "type": "string",
                        "enum": ["vs_lhp", "vs_rhp", "home", "away", "day", "night", "grass", "turf", "all", "city"],
                        "description": "Which split to return. 'vs_lhp'/'vs_rhp' for platoon, 'home'/'away', 'day'/'night', 'grass'/'turf', 'city' for all city splits, or 'all' for everything (default).",
                    },
                    "season": {
                        "type": "integer",
                        "description": "Optional MLB season year (e.g. 2025). Omit for career + current season.",
                    },
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_players",
            "description": "Fuzzy MLB player lookup: returns a ranked list of players matching a name even with typos or misspellings, so you can disambiguate (e.g. 'Soto' -> Juan Soto (NYY) vs Giovanni Soto). Use this when you're unsure of the exact spelling or want alternatives, then call get_player_stats / get_player_split_stats with player_name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {
                        "type": "string",
                        "description": "Name to search (typos allowed; accent-insensitive). e.g. 'Jose Ramres' or 'Judge'.",
                    },
                    "team": {
                        "type": "string",
                        "description": "Optional team abbreviation (e.g. 'NYY', 'LAD') to boost players on that team (resolves shared names like Soto/Judge).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max candidates to return (default 8).",
                    },
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_game_prediction",
            "description": "Get Earl's model prediction for a specific game: ATS, O/U, and moneyline probabilities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_id": {
                        "type": "integer",
                        "description": "Game ID",
                    },
                },
                "required": ["game_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_splits",
            "description": "Get situational splits for a team: home/away, day/night, grass/turf, vs RHP/LHP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {
                        "type": "string",
                        "description": "Full team name",
                    },
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_articles",
            "description": "Search for relevant news articles using semantic search. Filters by date range when provided. Returns article titles and summaries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query describing what you're looking for",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of articles to return (default 8, max 15)",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Earliest publish date (ISO: YYYY-MM-DD), inclusive from midnight UTC. Example: 2025-09-01",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Latest publish date (ISO: YYYY-MM-DD), inclusive through end of day UTC. Example: 2025-12-31",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_trends",
            "description": "Get a team's recent performance trends (runs scored/allowed, team batting avg/OBP/SLG/OPS, team ERA/WHIP, win%) over the last 5, 10, 15, and 20 games from rolling stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name, abbreviation, or city"},
                    "window": {"type": "string", "enum": ["5", "10", "15", "20", "all"], "description": "Trend window: '5', '10', '15', '20', or 'all' (default 'all')"},
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_comparison",
            "description": "Compare two MLB teams side by side on team batting (AVG, OBP, SLG, OPS) and team pitching (ERA, WHIP, K/9, BB/9) using cumulative season stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_a": {"type": "string", "description": "First team name/abbreviation/city"},
                    "team_b": {"type": "string", "description": "Second team name/abbreviation/city"},
                },
                "required": ["team_a", "team_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bullpen_stats",
            "description": "Get a team's bullpen quality: season ERA/WHIP/FIP/K/9, saves & blown saves, and L/R batting splits against the pen (opponents' AVG/OPS L vs R). Helps assess late-game/relief risk for fades and over/under.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name, abbreviation, or city"},
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_recent_form",
            "description": "Get a hitter's recent form over the trailing N days (default 30): games, PA, AVG/OBP/SLG/OPS, HR, RBI, runs, K%, and whether they're hot or cold. Aggregates from game logs to answer 'who's hot right now?'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player name (accent/typo tolerant)"},
                    "days": {"type": "integer", "description": "Trailing window in days (default 30, max 90)"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_pitching_rankings",
            "description": "Rank all MLB pitching staffs by a category (team ERA, WHIP, K/9, BB/9) using cumulative season stats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["era", "whip", "k9", "bb9"], "description": "Ranking category"},
                    "limit": {"type": "integer", "description": "Number of ranked teams to return (default 10)"},
                },
                "required": ["category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pitcher_form",
            "description": "Get a starting pitcher's recent form: YTD and last 5/10/15/20-game ERA, WHIP, K/9, BB/9, K/BB, quality start rate, plus home/road and day/night ERA splits and rest days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Pitcher name"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_season_futures",
            "description": "Season-long futures odds. OMIT team_name to get ALL teams ranked by championship odds from favorite to biggest underdog (lowest number = best odds = favorite, e.g. +350 beats +1200). Provide team_name to get a single team's full futures (championship, make/miss playoffs, win total over/under).",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name, abbreviation, or city (optional). Omit to rank all teams by championship odds, favorites first."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_season_props",
            "description": "Get a player's season-long award props (MVP, Cy Young, Rookie of the Year, etc.) with odds and implied probability.",
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
            "name": "get_game_weather",
            "description": "Get the weather forecast for an MLB game (temperature, wind speed/direction, condition). Useful for assessing outdoor ballpark conditions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_id": {"type": "integer", "description": "The MLB game ID"},
                },
                "required": ["game_id"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_team(db: AsyncSession, team_name: str) -> MLBTeam | None:
    """Try to resolve a team name/abbreviation to a team row."""
    stmt = select(MLBTeam).where(
        (MLBTeam.name.ilike(f"%{team_name}%"))
        | (MLBTeam.abbreviation.ilike(team_name))
    )
    result = await db.execute(stmt)
    team = result.scalars().first()
    if team:
        return team

    # Fuzzy fallback
    stmt = select(MLBTeam)
    result = await db.execute(stmt)
    all_teams = result.scalars().all()
    words = team_name.lower().split()
    for t in all_teams:
        name_lower = t.name.lower()
        abbr_lower = t.abbreviation.lower()
        if any(w in name_lower or w == abbr_lower for w in words):
            return t
    return None


async def _resolve_current_season(db: AsyncSession) -> MLBSeason | None:
    result = await db.execute(
        select(MLBSeason).order_by(MLBSeason.year.desc()).limit(1)
    )
    return result.scalars().first()


async def _resolve_props_season(db: AsyncSession) -> int:
    """Most recent season for which futures/props exist (upcoming or current)."""
    r = await db.execute(text(
        "SELECT GREATEST(COALESCE(MAX(season_year), 0), 0) FROM mlb.team_props"
    ))
    val = r.scalar_one_or_none()
    if not val:
        r2 = await db.execute(text(
            "SELECT GREATEST(COALESCE(MAX(season_year), 0), 0) FROM mlb.player_season_props"
        ))
        val = r2.scalar_one_or_none()
    cur = await _resolve_current_season(db)
    return int(val or (cur.year if cur else 0))


# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------


async def _search_teams(db: AsyncSession, args: dict) -> list[dict]:
    query = args.get("query", "")
    stmt = select(MLBTeam).where(
        (MLBTeam.name.ilike(f"%{query}%"))
        | (MLBTeam.abbreviation.ilike(query))
    ).limit(10)
    result = await db.execute(stmt)
    teams = result.scalars().all()

    if not teams:
        words = query.lower().split()
        if len(words) > 1:
            stmt = select(MLBTeam)
            result = await db.execute(stmt)
            all_teams = result.scalars().all()
            teams = [
                t for t in all_teams
                if any(w in t.name.lower() or w in t.abbreviation.lower() for w in words)
            ][:10]

    return [
        {
            "id": t.id,
            "name": t.name,
            "abbreviation": t.abbreviation,
            "league": t.league,
            "division": t.division,
        }
        for t in teams
    ]


async def _get_team_stats(db: AsyncSession, args: dict) -> dict:
    team_name = args.get("team_name", "")
    team = await _resolve_team(db, team_name)
    if not team:
        return {"error": f"Team not found: {team_name}"}

    season = await _resolve_current_season(db)
    if not season:
        return {"error": "No current season found"}

    # Record from games table
    sql = text("""
        SELECT
            COUNT(*) AS total_games,
            SUM(CASE
                WHEN (g.home_team_id = :tid AND g.home_score > g.away_score)
                 OR (g.away_team_id = :tid AND g.away_score > g.home_score)
                THEN 1 ELSE 0 END) AS wins,
            SUM(CASE
                WHEN (g.home_team_id = :tid AND g.home_score < g.away_score)
                 OR (g.away_team_id = :tid AND g.away_score < g.home_score)
                THEN 1 ELSE 0 END) AS losses,
            SUM(g.home_score) FILTER (WHERE g.home_team_id = :tid) AS home_runs_scored,
            SUM(g.away_score) FILTER (WHERE g.away_team_id = :tid) AS away_runs_scored,
            SUM(g.away_score) FILTER (WHERE g.home_team_id = :tid) AS home_runs_allowed,
            SUM(g.home_score) FILTER (WHERE g.away_team_id = :tid) AS away_runs_allowed
        FROM mlb.games g
        WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
          AND g.season_id = :sid
          AND g.status = 'FINAL'
    """)
    result = await db.execute(sql, {"tid": team.id, "sid": season.id})
    row = result.fetchone()

    data = {"team": team.name, "abbreviation": team.abbreviation, "season": season.year}

    if row and row.total_games:
        total = row.total_games
        wins = row.wins or 0
        losses = row.losses or 0
        rs = (row.home_runs_scored or 0) + (row.away_runs_scored or 0)
        ra = (row.home_runs_allowed or 0) + (row.away_runs_allowed or 0)
        data["record"] = {
            "wins": wins,
            "losses": losses,
            "games_played": total,
            "win_pct": round(wins / total, 3),
            "runs_scored": rs,
            "runs_allowed": ra,
            "run_diff": rs - ra,
        }
    else:
        data["record"] = {"wins": 0, "losses": 0, "games_played": 0}

    return data


async def _get_team_batting_stats(db: AsyncSession, args: dict) -> dict:
    team_name = args.get("team_name", "")
    team = await _resolve_team(db, team_name)
    if not team:
        return {"error": f"Team not found: {team_name}"}

    season = await _resolve_current_season(db)
    if not season:
        return {"error": "No current season found"}

    sql = text("""
        SELECT
            COUNT(DISTINCT player_id) AS batters_used,
            SUM(at_bats) AS ab,
            SUM(hits) AS hits,
            SUM(doubles) AS dbl,
            SUM(triples) AS trp,
            SUM(home_runs) AS hr,
            SUM(runs_batted_in) AS rbi,
            SUM(runs) AS runs,
            SUM(base_on_balls) AS bb,
            SUM(strikeouts) AS so,
            SUM(stolen_bases) AS sb,
            SUM(caught_stealing) AS cs,
            SUM(hit_by_pitch) AS hbp,
            SUM(sacrifice_flies) AS sf,
            SUM(total_bases) AS tb
        FROM mlb.batting_stats
        WHERE team_id = :tid
          AND season_id = :sid
    """)
    result = await db.execute(sql, {"tid": team.id, "sid": season.id})
    row = result.fetchone()

    if not row or not row.ab or row.ab == 0:
        return {"error": f"No batting stats found for {team.name}"}

    ab = row.ab
    hits = row.hits or 0
    bb = row.bb or 0
    hbp = row.hbp or 0
    sf = row.sf or 0
    tb = row.tb or 0
    pa = ab + bb + hbp + sf

    return {
        "team": team.name,
        "season": season.year,
        "batters_used": row.batters_used,
        "plate_appearances": pa,
        "at_bats": ab,
        "runs": row.runs or 0,
        "hits": hits,
        "doubles": row.dbl or 0,
        "triples": row.trp or 0,
        "home_runs": row.hr or 0,
        "rbi": row.rbi or 0,
        "walks": bb,
        "strikeouts": row.so or 0,
        "stolen_bases": row.sb or 0,
        "caught_stealing": row.cs or 0,
        "hbp": hbp,
        "sac_fly": sf,
        "total_bases": tb,
        "avg": round(hits / ab, 3),
        "obp": round((hits + bb + hbp) / pa, 3) if pa > 0 else None,
        "slg": round(tb / ab, 3),
        "ops": round((hits + bb + hbp) / pa + tb / ab, 3) if pa > 0 else None,
    }


async def _get_team_pitching_stats(db: AsyncSession, args: dict) -> dict:
    team_name = args.get("team_name", "")
    team = await _resolve_team(db, team_name)
    if not team:
        return {"error": f"Team not found: {team_name}"}

    season = await _resolve_current_season(db)
    if not season:
        return {"error": "No current season found"}

    sql = text("""
        SELECT
            COUNT(DISTINCT player_id) AS pitchers_used,
            COUNT(*) AS all_rows,
            SUM(games_played) AS gp_sum,
            SUM(games_started) AS gs,
            SUM(wins) AS wins,
            SUM(losses) AS losses,
            SUM(saves) AS saves,
            SUM(innings_pitched) AS ip,
            SUM(hits) AS ha,
            SUM(runs) AS ra,
            SUM(earned_runs) AS er,
            SUM(home_runs) AS hr,
            SUM(base_on_balls) AS bb,
            SUM(strikeouts) AS so,
            SUM(hit_by_pitch) AS hb,
            SUM(batters_faced) AS bf
        FROM mlb.pitching_stats
        WHERE team_id = :tid
          AND season_id = :sid
    """)
    result = await db.execute(sql, {"tid": team.id, "sid": season.id})
    row = result.fetchone()

    if not row or not row.ip or row.ip == 0:
        return {"error": f"No pitching stats found for {team.name}"}

    ip = row.ip or 0
    er = row.er or 0
    ha = row.ha or 0
    bb = row.bb or 0
    so = row.so or 0
    hr_allowed = row.hr or 0
    bf = row.bf or 0

    era = (er / ip) * 9 if ip > 0 else None
    whip = (ha + bb) / ip if ip > 0 else None
    k_p9 = (so / ip) * 9 if ip > 0 else None
    bb_p9 = (bb / ip) * 9 if ip > 0 else None
    hr_p9 = (hr_allowed / ip) * 9 if ip > 0 else None

    # BABIP = (H - HR) / (BF - BB - SO - HR)
    babip = None
    denom = bf - bb - so - hr_allowed
    if denom > 0:
        babip = round((ha - hr_allowed) / denom, 3)

    return {
        "team": team.name,
        "season": season.year,
        "pitchers_used": row.pitchers_used,
        "total_pitcher_rows": row.all_rows,
        "games_started": row.gs or 0,
        "wins": row.wins or 0,
        "losses": row.losses or 0,
        "saves": row.saves or 0,
        "innings_pitched": round(float(ip), 1),
        "hits_allowed": ha,
        "runs_allowed": row.ra or 0,
        "earned_runs": er,
        "home_runs_allowed": hr_allowed,
        "walks": bb,
        "strikeouts": so,
        "era": round(era, 2) if era is not None else None,
        "whip": round(whip, 2) if whip is not None else None,
        "k_per_9": round(k_p9, 2) if k_p9 is not None else None,
        "bb_per_9": round(bb_p9, 2) if bb_p9 is not None else None,
        "hr_per_9": round(hr_p9, 2) if hr_p9 is not None else None,
        "babip": babip,
    }


async def _get_standings(db: AsyncSession, args: dict) -> list[dict]:
    season = await _resolve_current_season(db)
    if not season:
        return [{"error": "No current season found"}]

    sql = text("""
        SELECT
            t.id, t.name, t.abbreviation, t.league, t.division,
            COUNT(*) FILTER (WHERE g.status = 'FINAL') AS gp,
            SUM(CASE
                WHEN (g.home_team_id = t.id AND g.home_score > g.away_score)
                  OR (g.away_team_id = t.id AND g.away_score > g.home_score)
                THEN 1 ELSE 0 END) AS wins,
            SUM(CASE
                WHEN (g.home_team_id = t.id AND g.home_score < g.away_score)
                  OR (g.away_team_id = t.id AND g.away_score < g.home_score)
                THEN 1 ELSE 0 END) AS losses
        FROM mlb.teams t
        JOIN mlb.games g ON (g.home_team_id = t.id OR g.away_team_id = t.id)
            AND g.season_id = :sid
        GROUP BY t.id, t.name, t.abbreviation, t.league, t.division
        ORDER BY t.league, t.division, wins DESC
    """)
    result = await db.execute(sql, {"sid": season.id})
    rows = result.fetchall()
    return [
        {
            "id": r.id,
            "name": r.name,
            "abbreviation": r.abbreviation,
            "league": r.league,
            "division": r.division,
            "wins": r.wins or 0,
            "losses": r.losses or 0,
            "games_played": r.gp or 0,
            "win_pct": round(r.wins / r.gp, 3) if r.gp and r.gp > 0 else None,
        }
        for r in rows
    ]


async def _get_todays_games(db: AsyncSession, args: dict) -> list[dict]:
    game_date_str = args.get("game_date")
    if game_date_str:
        game_date = date.fromisoformat(game_date_str)
        # When an explicit date is passed (e.g., DeepSeek passes '2026-07-11'),
        # start at 00:00 UTC that day BUT extend 5h past midnight to catch
        # US 'tonight' games that land at 00:00-05:00 UTC.
        day_start = datetime.combine(game_date, datetime.min.time()).replace(tzinfo=dt_timezone.utc)
        day_end = day_start + timedelta(days=1, hours=5)
    else:
        # Use America/Chicago timezone for "today" — 17:47 CDT on Friday means
        # games from midnight CDT through next midnight CDT.
        # Midnight CDT = 05:00 UTC the same day, so we query UTC range:
        #   start: 05:00 UTC today (game_date)
        #   end:   05:00 UTC tomorrow
        now_utc = datetime.now(dt_timezone.utc)
        cdt_offset = timedelta(hours=-5)  # UTC-5 (CDT)
        now_cdt = now_utc.astimezone(dt_timezone(cdt_offset))
        chicago_date = now_cdt.date()
        # Start of Chicago day in UTC
        start_cdt = datetime.combine(chicago_date, datetime.min.time()).replace(tzinfo=dt_timezone(cdt_offset))
        end_cdt = start_cdt + timedelta(days=1)
        day_start = start_cdt.astimezone(dt_timezone.utc)
        day_end = end_cdt.astimezone(dt_timezone.utc)

    sql = text("""
        SELECT
            g.id AS game_id,
            ht.name AS home_team,
            at.name AS away_team,
            g.date,
            g.status,
            g.home_score,
            g.away_score,
            g.venue,
            c.closing_spread,
            c.closing_ou,
            c.closing_home_ml,
            c.closing_away_ml
        FROM mlb.games g
        JOIN mlb.teams ht ON ht.id = g.home_team_id
        JOIN mlb.teams at ON at.id = g.away_team_id
        LEFT JOIN mlb.betting_lines_consolidated c ON c.game_id = g.id
        WHERE g.date >= :day_start AND g.date < :day_end
        ORDER BY g.date
    """)
    result = await db.execute(sql, {"day_start": day_start, "day_end": day_end})
    rows = result.fetchall()

    return [
        {
            "game_id": r.game_id,
            "home_team": r.home_team,
            "away_team": r.away_team,
            "game_time": str(r.date) if r.date else None,
            "status": r.status,
            "home_score": r.home_score,
            "away_score": r.away_score,
            "venue": r.venue,
            "lines": {
                "spread": float(r.closing_spread) if r.closing_spread else None,
                "over_under": float(r.closing_ou) if r.closing_ou else None,
                "home_moneyline": float(r.closing_home_ml) if r.closing_home_ml else None,
                "away_moneyline": float(r.closing_away_ml) if r.closing_away_ml else None,
            },
        }
        for r in rows
    ]


async def _get_game_info(db: AsyncSession, args: dict) -> dict:
    game_id = args.get("game_id")
    if not game_id:
        return {"error": "game_id required"}

    sql = text("""
        SELECT
            g.id,
            s.year,
            ht.name AS home_team,
            at.name AS away_team,
            g.date,
            g.status,
            g.home_score,
            g.away_score,
            g.venue,
            g.home_pitcher_name,
            g.away_pitcher_name,
            c.closing_spread,
            c.closing_spread_home_odds,
            c.closing_spread_away_odds,
            c.closing_ou,
            c.closing_over_odds,
            c.closing_under_odds,
            c.closing_home_ml,
            c.closing_away_ml,
            c.opening_spread,
            c.opening_ou,
            c.closing_home_implied_probability,
            c.closing_away_implied_probability
        FROM mlb.games g
        JOIN mlb.teams ht ON ht.id = g.home_team_id
        JOIN mlb.teams at ON at.id = g.away_team_id
        JOIN mlb.seasons s ON s.id = g.season_id
        LEFT JOIN mlb.betting_lines_consolidated c ON c.game_id = g.id
        WHERE g.id = :gid
    """)
    result = await db.execute(sql, {"gid": game_id})
    row = result.fetchone()
    if not row:
        return {"error": f"Game {game_id} not found"}

    def _f(v):
        return float(v) if v is not None else None

    # ── LIVE LINE MOVEMENT (from betting_lines: opening vs current, per book) ──
    # Opening = is_opening=true row; Current = latest is_opening=false row per book.
    bl = (await db.execute(text("""
        SELECT sportsbook, is_opening, spread, over_under,
               home_moneyline, away_moneyline, recorded_at
        FROM mlb.betting_lines
        WHERE game_id = :gid AND is_opening IS NOT NULL
        ORDER BY sportsbook, recorded_at
    """), {"gid": game_id})).fetchall()

    movement_by_book = {}
    for b in bl:
        book = b[0]; is_open = b[1]
        entry = movement_by_book.setdefault(book, {"opening": None, "current": None})
        key = "opening" if is_open else "current"
        if key == "current":
            # keep the LATEST current row per book (iteration is ordered by recorded_at)
            entry[key] = {
                "spread": _f(b[2]), "over_under": _f(b[3]),
                "home_ml": _f(b[4]), "away_ml": _f(b[5]), "recorded_at": str(b[6]),
            }
        elif is_open and entry["opening"] is None:
            entry["opening"] = {
                "spread": _f(b[2]), "over_under": _f(b[3]),
                "home_ml": _f(b[4]), "away_ml": _f(b[5]), "recorded_at": str(b[6]),
            }

    def _movement(open_val, curr_val):
        if open_val is None or curr_val is None:
            return None
        return round(curr_val - open_val, 2)

    line_movement = {}
    for book, e in movement_by_book.items():
        op = e["opening"]; cu = e["current"]
        line_movement[book] = {
            "opening_spread": op["spread"] if op else None,
            "current_spread": cu["spread"] if cu else None,
            "spread_movement": _movement(op["spread"] if op else None, cu["spread"] if cu else None),
            "opening_ou": op["over_under"] if op else None,
            "current_ou": cu["over_under"] if cu else None,
            "ou_movement": _movement(op["over_under"] if op else None, cu["over_under"] if cu else None),
            "opening_home_ml": op["home_ml"] if op else None,
            "current_home_ml": cu["home_ml"] if cu else None,
            "home_ml_movement": _movement(op["home_ml"] if op else None, cu["home_ml"] if cu else None),
            "current_recorded_at": cu["recorded_at"] if cu else None,
        }

    return {
        "game_id": row.id,
        "home_team": row.home_team,
        "away_team": row.away_team,
        "game_time": str(row.date) if row.date else None,
        "status": row.status,
        "score": {"home": row.home_score, "away": row.away_score},
        "starting_pitchers": {
            "home": row.home_pitcher_name,
            "away": row.away_pitcher_name,
        },
        "venue": row.venue,
        "lines": {
            "spread": _f(row.closing_spread),
            "spread_home_odds": _f(row.closing_spread_home_odds),
            "spread_away_odds": _f(row.closing_spread_away_odds),
            "over_under": _f(row.closing_ou),
            "over_odds": _f(row.closing_over_odds),
            "under_odds": _f(row.closing_under_odds),
            "home_moneyline": _f(row.closing_home_ml),
            "away_moneyline": _f(row.closing_away_ml),
            "opening_spread": _f(row.opening_spread),
            "opening_ou": _f(row.opening_ou),
            "implied_home_pct": _f(row.closing_home_implied_probability),
            "implied_away_pct": _f(row.closing_away_implied_probability),
        },
        "line_movement": line_movement,
    }


async def _get_head_to_head(db: AsyncSession, args: dict) -> dict:
    team1 = args.get("team1", "")
    team2 = args.get("team2", "")
    limit = min(args.get("limit", 10), 20)

    t1 = await _resolve_team(db, team1)
    t2 = await _resolve_team(db, team2)
    if not t1 or not t2:
        return {"error": f"Could not find teams: {team1} / {team2}"}

    season = await _resolve_current_season(db)
    if not season:
        return {"error": "No current season found"}

    sql = text("""
        SELECT
            g.id, g.date, g.status, g.home_score, g.away_score,
            g.venue, ht.name AS home_team, at.name AS away_team
        FROM mlb.games g
        JOIN mlb.teams ht ON ht.id = g.home_team_id
        JOIN mlb.teams at ON at.id = g.away_team_id
        WHERE ((g.home_team_id = :t1 AND g.away_team_id = :t2)
            OR (g.home_team_id = :t2 AND g.away_team_id = :t1))
          AND g.season_id = :sid
        ORDER BY g.date DESC
        LIMIT :lim
    """)
    result = await db.execute(sql, {"t1": t1.id, "t2": t2.id, "sid": season.id, "lim": limit})
    rows = result.fetchall()

    games = []
    for r in rows:
        winner = None
        if r.status == "FINAL" and r.home_score is not None and r.away_score is not None:
            winner = r.home_team if r.home_score > r.away_score else r.away_team
        games.append({
            "game_id": r.id,
            "date": str(r.date) if r.date else None,
            "status": r.status,
            "home_team": r.home_team,
            "away_team": r.away_team,
            "score": f"{r.home_score}-{r.away_score}" if r.home_score is not None else None,
            "winner": winner,
            "venue": r.venue,
        })

    return {"team1": t1.name, "team2": t2.name, "season": season.year, "games": games}


async def _get_injuries(db: AsyncSession, args: dict) -> list[dict] | dict:
    team_name = args.get("team_name", "")
    team = await _resolve_team(db, team_name)
    if not team:
        return {"error": f"Team not found: {team_name}"}

    sql = text("""
        SELECT i.injury_type, i.status, i.injury_date, i.expected_return,
               i.description, p.name AS player_name
        FROM mlb.injuries i
        LEFT JOIN mlb.players p ON p.id = i.player_id
        WHERE i.team_id = :tid AND i.is_active = True
    """)
    result = await db.execute(sql, {"tid": team.id})
    rows = result.fetchall()

    if not rows:
        return {"message": f"No active injuries for {team.name}"}

    return [
        {
            "player": r.player_name or f"Player #{i}",
            "injury_type": r.injury_type,
            "status": r.status,
            "injury_date": str(r.injury_date) if r.injury_date else None,
            "expected_return": str(r.expected_return) if r.expected_return else None,
            "description": r.description,
        }
        for i, r in enumerate(rows, 1)
    ]


async def _get_player_stats(db: AsyncSession, args: dict) -> dict:
    player_name = args.get("player_name", "")
    stmt = select(MLBPlayer).where(
        MLBPlayer.name.ilike(f"%{player_name}%")
    ).limit(5)
    result = await db.execute(stmt)
    players = result.scalars().all()
    if not players:
        return {"error": f"Player not found: {player_name}"}

    player = players[0]
    season = await _resolve_current_season(db)

    data = {"player": player.name, "team_id": player.team_id, "position": player.position}

    if season:
        # Batting season stats
        sql = text("""
            SELECT
                games_played, at_bats, hits, doubles, triples, home_runs,
                runs_batted_in, runs, base_on_balls, strikeouts,
                stolen_bases, avg, obp, slg, ops, total_bases
            FROM mlb.batting_stats
            WHERE player_id = :pid AND season_id = :sid
        """)
        result = await db.execute(sql, {"pid": player.id, "sid": season.id})
        row = result.fetchone()
        if row and row.games_played and row.games_played > 0:
            data["batting"] = {
                "games_played": row.games_played,
                "at_bats": row.at_bats,
                "hits": row.hits,
                "doubles": row.doubles,
                "triples": row.triples,
                "home_runs": row.home_runs,
                "rbi": row.runs_batted_in,
                "runs": row.runs,
                "walks": row.base_on_balls,
                "strikeouts": row.strikeouts,
                "stolen_bases": row.stolen_bases,
                "avg": round(row.avg, 3) if row.avg else None,
                "obp": round(row.obp, 3) if row.obp else None,
                "slg": round(row.slg, 3) if row.slg else None,
                "ops": round(row.ops, 3) if row.ops else None,
            }

        # Pitching season stats
        sql = text("""
            SELECT
                games_played, games_started, wins, losses, saves, innings_pitched,
                hits, runs, earned_runs, home_runs,
                base_on_balls, strikeouts, era, whip
            FROM mlb.pitching_stats
            WHERE player_id = :pid AND season_id = :sid
        """)
        result = await db.execute(sql, {"pid": player.id, "sid": season.id})
        row = result.fetchone()
        if row and row.games_played and row.games_played > 0:
            data["pitching"] = {
                "games": row.games_played,
                "games_started": row.games_started,
                "wins": row.wins,
                "losses": row.losses,
                "saves": row.saves,
                "innings_pitched": row.innings_pitched,
                "hits_allowed": row.hits,
                "runs_allowed": row.runs,
                "earned_runs": row.earned_runs,
                "home_runs_allowed": row.home_runs,
                "walks": row.base_on_balls,
                "strikeouts": row.strikeouts,
                "era": round(row.era, 2) if row.era else None,
                "whip": round(row.whip, 2) if row.whip else None,
            }

    return data


def _norm_name(s: str) -> str:
    """Accent- and case-insensitive name key (NFD-stripped lowercase)."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()


_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\.?\s*$", re.IGNORECASE)


def _strip_suffix(s: str) -> str:
    """Remove a trailing name suffix (Jr., Sr., II, III, IV, V) from a player
    name so suffixed and non-suffixed variants match on their core name.
    E.g. "Fernando Tatis Jr." -> "Fernando Tatis"."""
    return _SUFFIX_RE.sub("", s or "")


async def _search_players(db: AsyncSession, name: str, team_abbr: str = "", limit: int = 8):
    """Fuzzy player lookup. Returns a ranked list of candidate dicts
    ``{player_id, name, team, position, bats, score, exact}`` best-first.

    Accent/case-insensitive (NFD) with token-overlap + Levenshtein-style fuzzy
    (SequenceMatcher) scoring so typos still rank well. Optional ``team_abbr``
    (e.g. "NYY") boosts players on that team to disambiguate shared names
    ("Soto" + NYY -> Juan).
    """
    q = _norm_name(name)
    if not q:
        return []
    q_tokens = q.split()
    q_set = set(q_tokens)
    team_abbr = (team_abbr or "").upper()

    rows = (await db.execute(text(
        """
        SELECT p.id, p.name, p.position, p.team_id, p.bats,
               t.abbreviation AS team_abbr,
               EXISTS (
                   SELECT 1 FROM mlb.batting_game_stats bgs
                   JOIN mlb.games g ON g.id = bgs.game_id
                   WHERE bgs.player_id = p.id AND g.season_id = 21
               ) AS has_season_data
        FROM mlb.players p
        LEFT JOIN mlb.teams t ON t.id = p.team_id
        """
    ))).fetchall()
    if not rows:
        return []

    results = []
    for pid, rname, pos, team_id, bats, tabbr, has_season_data in rows:
        pn = _norm_name(rname or "")
        ptokens = pn.split()
        if not ptokens:
            continue
        # core name (suffix-stripped, NFD) so "Jr.\" doesn't penalize a suffixed
        # player when the user omits the suffix (fixes Tatis/Guerrero/Young/etc.)
        core_candidate = _norm_name(_strip_suffix(rname or ""))
        core_query = _norm_name(_strip_suffix(name))
        q_tokens_local = core_query.split()
        c_tokens = core_candidate.split()

        exact = (pn == q)  # full normalized match (query includes suffix)
        # core overlap + similarity on suffix-stripped names
        core_exact = (core_candidate == core_query)
        overlap = len(set(q_tokens) & set(ptokens))
        core_overlap = len(set(q_tokens_local) & set(c_tokens))

        # Levenshtein-ish similarity on both full and core names
        ratio = difflib.SequenceMatcher(None, q, pn).ratio()
        core_ratio = difflib.SequenceMatcher(None, core_query, core_candidate).ratio()

        tok_ratio = max(
            (difflib.SequenceMatcher(None, qt, pt).ratio() for qt in q_tokens for pt in ptokens),
            default=0.0,
        )
        # team boost strong for shared names
        team_boost = 0.35 if tabbr and tabbr == team_abbr else 0.0
        # suffix-aware bonus: match on core name strongly, reward active-season data
        suffix_bonus = 1.5 if core_exact else 0.0
        active_bonus = 0.6 if has_season_data else 0.0
        score = core_overlap + core_ratio * 1.5 + tok_ratio * 1.0 + team_boost + suffix_bonus + active_bonus + (2.0 if exact else 0.0)
        if exact or core_exact or core_overlap >= 1 or tok_ratio >= 0.7 or ratio >= 0.55:
            results.append({
                "player_id": pid,
                "name": rname,
                "team": tabbr,
                "position": pos,
                "bats": bats,
                "has_season_data": bool(has_season_data),
                "score": round(score, 3),
                "exact": core_exact,
                "_core": core_candidate,
            })

    # sort: strong team match first, then core-exact, then active-season data,
    # then score. This resolves Sr./Jr./phantom collisions to the real active player.
    results.sort(
        key=lambda r: ("" if r["_core"] == core_query else "zz",
                       not r["has_season_data"],
                       not r["exact"],
                       -r["score"])
    )
    return results[:limit]


async def _resolve_hitter(db: AsyncSession, name: str, team_abbr: str = ""):
    """Find a single best MLBPlayer by (accent-insensitive, fuzzy) name.
    Returns the top match or None."""
    hits = await _search_players(db, name, team_abbr=team_abbr, limit=1)
    if not hits:
        return None
    return await db.get(MLBPlayer, hits[0]["player_id"])


async def _team_abbr_of(db: AsyncSession, player) -> str:
    """Resolve a player's current team abbreviation."""
    if getattr(player, "team_id", None):
        row = (await db.execute(
            text("SELECT abbreviation FROM mlb.teams WHERE id = :tid"), {"tid": player.team_id}
        )).first()
        if row:
            return row[0]
    return ""


async def _get_player_split_stats(db: AsyncSession, args: dict) -> dict:
    """Return a batter's split stats (L/R, home/away, day/night, grass/turf, city)."""
    player_name = args.get("player_name", "")
    split_type = args.get("split_type", "all") or "all"
    season_year = args.get("season")
    team_abbr = args.get("team", "") or ""

    player = await _resolve_hitter(db, player_name, team_abbr=team_abbr)
    if not player:
        # No confident match -> suggest close candidates so the LLM can retry.
        suggestions = await _search_players(db, player_name, team_abbr=team_abbr, limit=5)
        return {
            "player": player_name,
            "error": f"Player not found: {player_name}",
            "suggestions": [
                {"name": s["name"], "team": s["team"], "position": s["position"], "player_id": s["player_id"]}
                for s in suggestions
            ],
            "help": "No exact match. Use one of the suggested player ids (e.g. via search_players) and retry get_player_split_stats.",
        }

    # Resolve season scope
    season_id = None
    season_label = "career"
    if season_year:
        season = (await db.execute(select(MLBSeason).where(MLBSeason.year == season_year))).scalar_one_or_none()
        if season:
            season_id = season.id
            season_label = str(season_year)
    else:
        # No explicit season -> provide current-season AND career both.
        pass

    # Normalize split filter
    if split_type == "city":
        like_pat = "city_%"
        exact = None
    elif split_type == "all":
        like_pat = None
        exact = None
    else:
        like_pat = None
        exact = split_type

    # Query stored splits
    q = (
        select(MLBPlayerSplit)
        .where(MLBPlayerSplit.player_id == player.id)
        .order_by(MLBPlayerSplit.split_type, MLBPlayerSplit.season_id)
    )
    if exact:
        q = q.where(MLBPlayerSplit.split_type == exact)
    if like_pat:
        q = q.where(MLBPlayerSplit.split_type.like(like_pat))
    rows = (await db.execute(q)).scalars().all()

    if not rows:
        return {
            "player": player.name,
            "note": "No split data stored for this player yet. The mlb-splits refresh job has not populated them.",
            "splits": [],
        }

    def fmt(r: MLBPlayerSplit) -> dict:
        return {
            "split_type": r.split_type,
            "label": r.split_label,
            "season": r.season_id if r.season_id else "career",
            "games": r.games_played,
            "plate_appearances": r.plate_appearances,
            "at_bats": r.at_bats,
            "hits": r.hits,
            "home_runs": r.home_runs,
            "rbi": r.runs_batted_in,
            "walks": r.base_on_balls,
            "strikeouts": r.strikeouts,
            "avg": round(r.avg, 3) if r.avg is not None else None,
            "obp": round(r.obp, 3) if r.obp is not None else None,
            "slg": round(r.slg, 3) if r.slg is not None else None,
            "ops": round(r.ops, 3) if r.ops is not None else None,
        }

    # Filter: if exact/all and no season year -> split into current-season vs career
    if season_year:
        return {"player": player.name, "season": season_label,
                "splits": [fmt(r) for r in rows if r.season_id == season_id]}

    current_season = await _resolve_current_season(db)
    current_id = current_season.id if current_season else None
    current = [r for r in rows if r.season_id == current_id]
    career = [r for r in rows if r.season_id is None]
    return {
        "player": player.name,
        "current_season": season_label if not current_season else current_season.year,
        "current_season_splits": [fmt(r) for r in current],
        "career_splits": [fmt(r) for r in career],
    }


async def _search_players_tool(db: AsyncSession, args: dict) -> dict:
    """Executor for the fuzzy search_players tool."""
    player_name = args.get("player_name", "")
    team = args.get("team", "") or ""
    limit = args.get("limit") or 8
    hits = await _search_players(db, player_name, team_abbr=team, limit=int(limit))
    if not hits:
        return {"query": player_name, "results": [], "count": 0}
    return {
        "query": player_name,
        "count": len(hits),
        "results": [
            {
                "name": h["name"],
                "team": h["team"],
                "position": h["position"],
                "bats": h["bats"],
                "player_id": h["player_id"],
                "match_score": h["score"],
            }
            for h in hits
        ],
    }


async def _get_game_prediction(db: AsyncSession, args: dict) -> dict:
    game_id = args.get("game_id")
    if not game_id:
        return {"error": "game_id required"}

    stmt = select(MLBGamePrediction).where(
        MLBGamePrediction.game_id == game_id
    ).limit(1)
    result = await db.execute(stmt)
    pred = result.scalars().first()
    if not pred:
        return {"error": f"No prediction found for game {game_id}"}

    def _f(v):
        return float(v) if v is not None else None

    return {
        "game_id": pred.game_id,
        "predicted_home_runs": _f(pred.predicted_home_runs),
        "predicted_away_runs": _f(pred.predicted_away_runs),
        "predicted_total": _f(pred.predicted_total),
        "predicted_margin": _f(pred.predicted_margin),
        "ou_pick": pred.ou_pick,
        "ou_confidence_calibrated": _f(pred.ou_conf_cal),
        "run_line_pick": pred.run_line_pick,
        "rl_confidence_calibrated": _f(pred.rl_conf_cal),
        "ml_pick": pred.ml_pick,
        "ml_confidence_calibrated": _f(pred.ml_conf_cal),
        "rl_confidence_raw": _f(pred.rl_conf),
        "ml_confidence_raw": _f(pred.ml_conf),
        "ou_confidence_raw": _f(pred.ou_conf),
        "ats_expected_value": _f(pred.ats_ev),
        "ou_expected_value": _f(pred.ou_ev),
        "ml_expected_value": _f(pred.ml_ev),
        "source": pred.source,
    }


async def _get_team_splits(db: AsyncSession, args: dict) -> dict:
    team_name = args.get("team_name", "")
    team = await _resolve_team(db, team_name)
    if not team:
        return {"error": f"Team not found: {team_name}"}

    season = await _resolve_current_season(db)
    if not season:
        return {"error": "No current season found"}

    stmt = select(MLBTeamSplit).where(
        MLBTeamSplit.team_id == team.id,
        MLBTeamSplit.season_id == season.id,
    )
    result = await db.execute(stmt)
    splits = result.scalars().all()

    return {
        "team": team.name,
        "season": season.year,
        "splits": [
            {
                "split_type": s.split_type,
                "games": s.games,
                "wins": s.wins or 0,
                "losses": s.losses or 0,
                "w_pct": round(s.wins / s.games, 3) if s.games and s.games > 0 else None,
                "runs_scored": s.runs_scored or 0,
                "runs_allowed": s.runs_allowed or 0,
                "avg": round(s.avg, 3) if s.avg else None,
                "ops": round(s.ops, 3) if s.ops else None,
                "home_runs": s.home_runs or 0,
                "era": round(s.era, 2) if s.era else None,
                "whip": round(s.whip, 2) if s.whip else None,
            }
            for s in (splits or [])
        ],
    }


async def _search_articles_tool(db: AsyncSession, args: dict) -> list[dict]:
    """Search for articles via pgvector with optional date filter."""
    from app.ingestion.pgvector_search import search_articles

    query = args.get("query", "")
    limit = min(args.get("limit", 8), 15)

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
        db, query, top_k=limit, sport="mlb",
        date_from=date_from, date_to=date_to,
    )
    return [
        {
            "title": a.get("title", "Untitled"),
            "source": a.get("source_name", "Unknown"),
            "text": (a.get("text", "") or "")[:2000],
        }
        for a in articles
    ]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def _get_team_trends(db: AsyncSession, args: dict) -> dict:
    """Recent performance trends from mlb.team_rolling_stats."""
    team = await _resolve_team(db, args.get("team_name", ""))
    if not team:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    window = args.get("window", "all").strip().lower()

    suffixes = ["5", "10", "15", "20"]
    if window in suffixes:
        suffixes = [window]

    # Column sets per rolling window (schema is irregular across 5/10/15/20)
    window_cols = {
        "5": ["avg5", "obp5", "slg5", "ops5", "era5", "whip5", "k9_5", "bb9_5"],
        "10": ["avg10", "obp10", "slg10", "ops10", "era10", "whip10", "k9_10", "bb9_10"],
        "15": ["avg15", "slg15", "ops15", "era15", "whip15"],
        "20": ["avg20", "slg20", "ops20", "era20", "whip20"],
    }
    selected = []
    for sfx in suffixes:
        selected.extend(window_cols[sfx])

    sql = text(
        f"""SELECT game_id, game_date, team_side, rf, ra, home_score, away_score, closing_ou,
              win_pct, win_pct5, win_pct10, win_pct15, spread_pct, over_pct, over_pct5, over_pct10, over_pct15, {', '.join(selected)}
        FROM mlb.team_rolling_stats
        WHERE team_id = :tid
        ORDER BY game_date DESC LIMIT 40"""
    )
    r = await db.execute(sql, {"tid": team.id})
    rows = r.mappings().all()
    if not rows:
        return {"error": f"No rolling stats found for {team.name}"}

    def f(v):
        return round(float(v), 3) if v is not None else None

    games = []
    for row in rows[:8]:
        g = {"date": str(row.game_date)[:10], "side": row.team_side, "runs_for": row.rf, "runs_against": row.ra}
        for col in selected:
            g[col] = f(row[col])
        games.append(g)

    latest = rows[0]
    summary = {"win_pct": f(latest.win_pct), "win_pct_5": f(latest.win_pct5), "win_pct_10": f(latest.win_pct10), "over_pct_5": f(latest.over_pct5), "over_pct_10": f(latest.over_pct10)}
    for sfx in suffixes:
        for col in window_cols[sfx]:
            summary[col] = f(latest[col])

    return {"team": team.name, "abbreviation": team.abbreviation, "windows": suffixes, "latest_summary": summary, "recent_games": games}


async def _get_team_comparison(db: AsyncSession, args: dict) -> dict:
    """Side-by-side team comparison from mlb.cumulative_game_stats."""
    a = await _resolve_team(db, args.get("team_a", ""))
    b = await _resolve_team(db, args.get("team_b", ""))
    if not a or not b:
        missing = [args.get("team_a"), args.get("team_b")] if not a else [args.get("team_b")]
        return {"error": f"Team(s) not found: {', '.join(str(m) for m in missing)}"}

    sql = text(
        """SELECT game_id, bat_at_bats, bat_hits, bat_runs, bat_home_runs, bat_walks, bat_strikeouts,
              bat_total_bases, bat_plate_appearances, cum_avg, cum_obp, cum_slg, cum_ops, cum_babip, cum_k_rate, cum_bb_rate,
              pitch_ip, pitch_er, pitch_strikeouts, pitch_batters_faced, cum_era, cum_whip, cum_k9, cum_bb9
        FROM mlb.cumulative_game_stats
        WHERE team_id IN (:a, :b)
        ORDER BY game_timestamp DESC LIMIT 4"""
    )
    r = await db.execute(sql, {"a": a.id, "b": b.id})
    rows = r.mappings().all()
    if not rows:
        return {"error": f"Cumulative stats not found for {a.name} vs {b.name}"}

    def f(v):
        return round(float(v), 3) if v is not None else None

    la = await db.execute(text(
        "SELECT * FROM mlb.cumulative_game_stats WHERE team_id = :a ORDER BY game_timestamp DESC LIMIT 1"), {"a": a.id})
    lb = await db.execute(text(
        "SELECT * FROM mlb.cumulative_game_stats WHERE team_id = :b ORDER BY game_timestamp DESC LIMIT 1"), {"b": b.id})
    da, db_ = la.mappings().first(), lb.mappings().first()
    if not da or not db_:
        return {"error": f"Cumulative stats not found for {a.name} vs {b.name}"}

    batting = [("cum_avg", "Batting AVG"), ("cum_obp", "Batting OBP"), ("cum_slg", "Batting SLG"), ("cum_ops", "Batting OPS"), ("cum_k_rate", "K Rate"), ("cum_bb_rate", "BB Rate")]
    pitching = [("cum_era", "Team ERA"), ("cum_whip", "Team WHIP"), ("cum_k9", "Team K/9"), ("cum_bb9", "Team BB/9")]
    result = {"compare": {}}
    for col, label in batting + pitching:
        result["compare"][label] = {
            a.abbreviation: f(da[col]),
            b.abbreviation: f(db_[col]),
        }
    result["team_a"] = f"{a.name} ({a.abbreviation})"
    result["team_b"] = f"{b.name} ({b.abbreviation})"
    return result


async def _get_team_pitching_rankings(db: AsyncSession, args: dict) -> dict:
    """Rank MLB pitching staffs by ERA/WHIP/K9/BB9 from cumulative_game_stats."""
    cat = args.get("category", "era")
    limit = min(args.get("limit", 10), 30)
    col = {"era": "cum_era", "whip": "cum_whip", "k9": "cum_k9", "bb9": "cum_bb9"}.get(cat)
    if not col:
        return {"error": f"Unknown category: {cat}. Use era, whip, k9, or bb9."}
    order = "ASC" if cat in ("era", "whip", "bb9") else "DESC"

    # Fetch the latest cumulative row per team, then rank
    sql = text(
        f"""SELECT team_id, {col} AS val FROM (
            SELECT team_id, {col}, ROW_NUMBER() OVER (PARTITION BY team_id ORDER BY game_timestamp DESC) rn
            FROM mlb.cumulative_game_stats
        ) t WHERE rn = 1
        ORDER BY val {order} NULLS LAST
        LIMIT :limit"""
    )
    r = await db.execute(sql, {"limit": limit})
    rows = r.mappings().all()
    ranking = []
    for idx, row in enumerate(rows, 1):
        team = await db.execute(text("SELECT name, abbreviation FROM mlb.teams WHERE id = :tid"), {"tid": row.team_id})
        t = team.mappings().first()
        ranking.append({"rank": idx, "team": (t.name if t else "?"), "abbreviation": (t.abbreviation if t else "?"), "value": round(float(row.val), 3) if row.val is not None else None})
    return {"category": cat, "ranking": ranking}


async def _get_pitcher_form(db: AsyncSession, args: dict) -> dict:
    """Pitcher recent form from mlb.pitcher_rolling_stats."""
    name = args.get("player_name", "").strip()
    if not name:
        return {"error": "player_name required"}
    player = (await db.execute(select(MLBPlayer).where(MLBPlayer.name.ilike(f"%{name}%")).limit(1))).scalars().first()
    if not player:
        return {"error": f"Player not found: {name}"}

    # pitcher_rolling_stats.player_id stores the MLB StatsAPI id (mlb_id), not mlb.players.id
    pid = player.mlb_id or player.id
    sql = text(
        """SELECT * FROM mlb.pitcher_rolling_stats
        WHERE player_id = :pid
        ORDER BY game_date DESC LIMIT 1"""
    )
    r = await db.execute(sql, {"pid": pid})
    row = r.mappings().first()
    if not row:
        return {"error": f"No pitching stats found for {player.name}"}

    def f(v):
        return round(float(v), 3) if v is not None else None

    def win(w):
        return {"era": f(row[f"era_{w}"]), "whip": f(row[f"whip_{w}"]), "k9": f(row[f"k9_{w}"]), "bb9": f(row[f"bb9_{w}"]), "kbb": f(row.get(f"kbb_{w}"))}

    return {
        "player": player.name,
        "team_abbr": row.team_abbr,
        "is_starter": bool(row.is_starter),
        "rest_days": row.rest_days,
        "latest_start": str(row.game_date)[:10],
        "this_start": {"ip": row.ip_outs / 3 if row.ip_outs else None, "er": row.er, "k": row.strikeouts, "era": f(row.era_this_start), "whip": f(row.whip_this_start), "quality_start": bool(row.is_quality_start)},
        "ytd": {"era": f(row.era_ytd), "whip": f(row.whip_ytd), "k9": f(row.k9_ytd), "bb9": f(row.bb9_ytd), "kbb": f(row.kbb_ytd), "fip": f(row.fip_ytd), "qs_rate": f(row.qs_rate_ytd), "starts": row.starts_ytd},
        "last_5": win(5), "last_10": win(10), "last_15": win(15), "last_20": win(20),
        "splits": {"home_era": f(row.home_era_ytd), "road_era": f(row.road_era_ytd), "day_era": f(row.day_era_ytd), "night_era": f(row.night_era_ytd)},
    }


async def _get_bullpen_stats(db: AsyncSession, args: dict) -> dict:
    """Team bullpen quality: season ERA/WHIP/FIP/K9 + L/R batting splits vs the pen."""
    team = await _resolve_team(db, args.get("team_name", ""))
    if not team:
        return {"error": f"Team not found: {args.get('team_name', '')}"}

    sql = text("""
        SELECT era, whip, fip, strikeouts, walks, hits, home_runs, innings_pitched,
               saves, blown_saves, hold, left_avg, right_avg,
               left_ops, right_ops
        FROM mlb.bullpen_stats
        WHERE team_id = :tid
        ORDER BY season_id DESC NULLS LAST
        LIMIT 1
    """)
    row = (await db.execute(sql, {"tid": team.id})).mappings().first()
    if not row:
        return {"error": f"No bullpen stats found for {team.name}"}

    def f(v):
        return round(float(v), 3) if v is not None else None

    k9 = None
    if row.innings_pitched and row.strikeouts:
        ip = float(row.innings_pitched)
        k9 = round(float(row.strikeouts) / ip * 9, 2) if ip else None

    return {
        "team": f"{team.name} ({team.abbreviation})",
        "season_era": f(row.era),
        "whip": f(row.whip),
        "fip": f(row.fip),
        "k9": k9,
        "opp_avg_vs_left": f(row.left_avg),
        "opp_avg_vs_right": f(row.right_avg),
        "opp_ops_vs_left": f(row.left_ops),
        "opp_ops_vs_right": f(row.right_ops),
        "saves": row.saves,
        "blown_saves": row.blown_saves,
        "holds": row.hold,
    }


async def _get_player_recent_form(db: AsyncSession, args: dict) -> dict:
    """Hitter's recent form over a trailing day window (e.g. last 30 days)."""
    player_name = args.get("player_name", "")
    days = min(max(int(args.get("days") or 30), 1), 90)
    player = await _resolve_hitter(db, player_name)
    if not player:
        return {"error": f"Player not found: {player_name}", "suggestions": [{"name": s["name"], "team": s["team"], "position": s["position"], "player_id": s["player_id"]} for s in await _search_players(db, player_name, limit=5)]}

    sql = text("""
        SELECT count(*) AS games,
               sum(plate_appearances) AS pa,
               sum(at_bats) AS ab,
               sum(hits) AS hits,
               sum(doubles) AS dbl,
               sum(triples) AS trp,
               sum(home_runs) AS hr,
               sum(total_bases) AS tb,
               sum(runs) AS runs,
               sum(runs_batted_in) AS rbi,
               sum(base_on_balls) AS bb,
               sum(strikeouts) AS k
        FROM mlb.batting_game_stats bg
        JOIN mlb.games g ON g.id = bg.game_id
        WHERE bg.player_id = :pid
          AND g.date >= now() - (:days * interval '1 day')
          AND g.status = 'FINAL'
    """)
    row = (await db.execute(sql, {"pid": player.id, "days": days})).mappings().first()
    if not row or not row.games:
        return {"player": player.name, "games": 0, "note": f"No finalized games in the last {days} days"}

    ab = row.ab or 0
    pa = row.pa or 0
    avg = round(float(row.hits) / ab, 3) if ab else None
    obp = round((float(row.hits) + float(row.bb)) / (ab + (row.bb or 0)), 3) if (ab + (row.bb or 0)) else None
    slg = round(float(row.tb or 0) / ab, 3) if ab else None
    ops = round((float(avg or 0) + float(slg or 0)), 3) if (avg is not None and slg is not None) else None
    k_pct = round(float(row.k or 0) / pa * 100, 1) if pa else None
    bb_pct = round(float(row.bb or 0) / pa * 100, 1) if pa else None

    hot = None
    if avg is not None:
        hot = "hot" if avg >= 0.300 else ("cold" if avg <= 0.220 else "neutral")

    return {
        "player": player.name,
        "team_abbr": await _team_abbr_of(db, player),
        "window_days": days,
        "games": row.games,
        "pa": pa,
        "avg": avg,
        "obp": obp,
        "slg": slg,
        "ops": ops,
        "home_runs": row.hr,
        "doubles": row.dbl,
        "triples": row.trp,
        "rbi": row.rbi,
        "runs": row.runs,
        "k_pct": k_pct,
        "bb_pct": bb_pct,
        "form": hot,
    }


async def _get_team_season_futures(db: AsyncSession, args: dict) -> dict:
    """Team season futures odds from mlb.team_props.

    If a team_name is given, returns that team's full futures across books.
    If omitted, returns ALL teams ranked by championship odds from favorite to
    biggest underdog (lowest number = best odds = favorite sorts first).
    """
    season = await _resolve_props_season(db)

    # ----- ranked list of all teams (no team_name) -----
    if not args.get("team_name", "").strip():
        sql = text(
            """SELECT t.name AS team_name, p.bookmaker, p.championship_odds
               FROM mlb.team_props p
               JOIN mlb.teams t ON t.id = p.team_id
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
    team = await _resolve_team(db, args.get("team_name", ""))
    if not team:
        return {"error": f"Team not found: {args.get('team_name', '')}"}

    sql = text(
        """SELECT * FROM mlb.team_props
        WHERE team_id = :tid AND season_year = :season
        ORDER BY scraped_at DESC LIMIT 20"""
    )
    r = await db.execute(sql, {"tid": team.id, "season": season})
    rows = r.mappings().all()
    if not rows:
        return {"error": f"No season futures found for {team.name}"}

    def f(v):
        return round(float(v), 1) if v is not None else None

    by_book = {}
    for row in rows:
        bm = row.bookmaker or "?"
        if bm not in by_book:
            by_book[bm] = {"bookmaker": bm, "championship_odds": f(row.championship_odds), "make_playoffs_odds": f(row.make_playoffs_odds), "miss_playoffs_odds": f(row.miss_playoffs_odds), "win_total": f(row.win_total), "win_total_over_odds": f(row.win_total_over_odds), "win_total_under_odds": f(row.win_total_under_odds)}
    return {"team": team.name, "abbreviation": team.abbreviation, "season": season, "futures": list(by_book.values())}


async def _get_player_season_props(db: AsyncSession, args: dict) -> dict:
    """Player season award props from mlb.player_season_props."""
    name = args.get("player_name", "").strip()
    if not name:
        return {"error": "player_name required"}
    season = await _resolve_props_season(db)

    sql = text(
        """SELECT * FROM mlb.player_season_props
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


async def _get_game_weather(db: AsyncSession, args: dict) -> dict:
    """Weather forecast for an MLB game from mlb.weather_forecasts."""
    gid = args.get("game_id")
    if not gid:
        return {"error": "game_id required"}
    sql = text(
        """SELECT * FROM mlb.weather_forecasts
        WHERE game_id = :gid
        ORDER BY forecast_observed_at DESC LIMIT 1"""
    )
    r = await db.execute(sql, {"gid": gid})
    row = r.mappings().first()
    if not row:
        return {"error": f"No weather forecast found for game {gid}"}
    return {
        "game_id": gid,
        "temperature_f": row.temperature,
        "wind_speed_mph": row.wind_speed,
        "wind_direction": row.wind_direction_cardinal,
        "condition": row.weather_condition,
        "observed_at": str(row.forecast_observed_at)[:16],
        "source": row.source,
    }


# Alias map: model-invoked name variants -> canonical registered tool name.
# DeepSeek occasionally omits the "_season_" segment (get_team_futures) or
# uses a slightly different tokenization; route those to the real tool so the
# research call succeeds instead of returning an "Unknown tool" error.
_TOOL_ALIASES = {
    "get_team_futures": "get_team_season_futures",
    "get_team_future": "get_team_season_futures",
    "get_team_season_future": "get_team_season_futures",
    "get_player_props": "get_player_season_props",
    "get_player_season_prop": "get_player_season_props",
    "get_defense_rankings": "get_team_pitching_rankings",
    "get_team_rankings": "get_team_pitching_rankings",
    "get_game_forecast": "get_game_weather",
    "get_weather": "get_game_weather",
}


def _normalize_tool_name(name: str) -> str:
    """Map a model-invoked tool name to its canonical registered name."""
    if not name:
        return name
    normalized = name.strip()
    return _TOOL_ALIASES.get(normalized, normalized)




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
        FROM mlb.game_writeups w
        JOIN mlb.games g ON g.id = w.game_id
        JOIN mlb.teams ht ON ht.id = g.home_team_id
        JOIN mlb.teams at2 ON at2.id = g.away_team_id
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

_TOOL_MAP = {
    "search_teams": _search_teams,
    "get_team_stats": _get_team_stats,
    "get_team_batting_stats": _get_team_batting_stats,
    "get_team_pitching_stats": _get_team_pitching_stats,
    "get_standings": _get_standings,
    "get_todays_games": _get_todays_games,
    "get_game_info": _get_game_info,
    "get_game_writeup": _get_game_writeup,
    "get_head_to_head": _get_head_to_head,
    "get_injuries": _get_injuries,
    "get_player_stats": _get_player_stats,
    "get_player_split_stats": _get_player_split_stats,
    "get_bullpen_stats": _get_bullpen_stats,
    "get_player_recent_form": _get_player_recent_form,
    "search_players": _search_players_tool,
    "get_game_prediction": _get_game_prediction,
    "get_team_splits": _get_team_splits,
    "search_articles": _search_articles_tool,
    "get_team_trends": _get_team_trends,
    "get_team_comparison": _get_team_comparison,
    "get_team_pitching_rankings": _get_team_pitching_rankings,
    "get_pitcher_form": _get_pitcher_form,
    "get_team_season_futures": _get_team_season_futures,
    "get_player_season_props": _get_player_season_props,
    "get_game_weather": _get_game_weather,
}


async def execute_mlb_tool(db: AsyncSession, tool_call) -> str:
    """Execute an MLB tool call and return a JSON result string.

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

    if func_name not in _TOOL_MAP:
        logger.warning("Unknown MLB tool called: %s", func_name)
        return json.dumps({"error": f"Unknown MLB tool: {func_name}"})

    logger.info("Executing MLB tool: %s args=%s", func_name, args)
    result = await _TOOL_MAP[func_name](db, args)
    return json.dumps(result, default=str, ensure_ascii=False)
