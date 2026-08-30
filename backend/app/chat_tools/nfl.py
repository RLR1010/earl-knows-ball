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


async def _resolve_data_season_year(db: AsyncSession) -> int:
    """Year of the most recent season that ACTUALLY has rolling-stats rows.

    This is the safety net for matchup/trends queries: during preseason the
    calendar-latest season (2026) has zero rolling stats, so we fall back to the
    latest season that has data (e.g. 2025) instead of returning an empty result.
    Mirrors NBA's _resolve_data_season_id behavior.
    """
    r = await db.execute(text(
        "SELECT MAX(season) FROM nfl.team_rolling_stats"
    ))
    val = r.scalar_one_or_none()
    if val is None:
        return await _resolve_season_year(db)
    return int(val)


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


async def _match_players(db: AsyncSession, player_name: str) -> list:
    """Return canonical Player ORM rows matching a name, WITHOUT guessing.
    Matches "First Last" / "First-Last" / single-name prefixes. Excludes any
    remaining ESPN-* placeholder rows. Returns [] if none match."""
    clean = player_name.strip()
    parts = clean.lower().split(" ", 1)
    stmt = select(Player).where(Player.name.ilike(
        f"{parts[0]}% {parts[1]}%" if len(parts) == 2 else "%" + parts[0] + "%"
    ))
    stmt = stmt.where(Player.name.notlike("ESPN-%"))
    r = await db.execute(stmt)
    return list(r.scalars().all())


async def _resolve_player_record(db: AsyncSession, player_name: str) -> tuple:
    """Conservative player resolution used by player tools.

    Returns (player_obj, note) where note is "" when unambiguous/clean, or a
    short warning when we had to disambiguate (multiple matches). Prefers the
    row with a real espn_id (canonical) over a same-named espn_id-less dup.
    If the name matches MULTIPLE espn_id-bearing rows (genuinely ambiguous),
    returns (None, "multiple-ambiguous") so callers never guess between
    same-named players.
    """
    matches = await _match_players(db, player_name)
    if not matches:
        return None, f"Player not found: {player_name}"
    with_espn = [m for m in matches if getattr(m, "espn_id", None)]
    if with_espn:
        if len(with_espn) == 1:
            return with_espn[0], ""
        # multiple espn-bearing rows with the same name -> genuinely ambiguous
        return None, f"Multiple players named {player_name} found; please be more specific"
    # no espn_id anywhere: unique name is fine; dup names w/o espn are ambiguous
    if len(matches) == 1:
        return matches[0], ""
    return None, f"Multiple players named {player_name} found; please be more specific"



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
            "description": "Get head-to-head results between two NFL teams: season meetings, aggregate series record (who leads, W-L, points) for a season. Pass season_year for any season (defaults to current).",
            "parameters": {
                "type": "object",
                "properties": {
                    "team1": {"type": "string", "description": "Team name or abbreviation"},
                    "team2": {"type": "string", "description": "Team name or abbreviation"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                    "limit": {"type": "integer", "description": "Meetings to return (default 10, max 20)"},
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
            "description": "Get a player's game-by-game weekly stats for a season: pass/rush/receiving. Optionally filter by month/date-range, home-or-away, or opponent (e.g. 'how did he play in October' or 'his numbers vs the Chiefs'). With filters returns an aggregate line too.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player full name"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                    "month": {"type": "integer", "description": "Calendar month 1-12 to filter to (e.g. 10 for October)"},
                    "start_date": {"type": "string", "description": "Start date inclusive (ISO YYYY-MM-DD)"},
                    "end_date": {"type": "string", "description": "End date inclusive (ISO YYYY-MM-DD)"},
                    "home_or_away": {"type": "string", "enum": ["home", "away", "all"], "description": "Filter home or away games (default 'all')"},
                    "opponent": {"type": "string", "description": "Filter to games vs this opponent team name/abbr"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_splits",
            "description": "Get a player's situational/career splits: home vs away, cold vs warm games, dome vs outdoor, grass vs turf, division vs non-division, primetime vs day. Optional split_type to request one (e.g. 'home','cold','dome','division','primetime'). Returns career splits by default; set season_year for one season. Great for 'is he better at home / in cold weather?' questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player full name (e.g., 'Patrick Mahomes', 'Justin Jefferson')"},
                    "split_type": {"type": "string", "description": "Optional: one split type to isolate (home, away, cold, mild, warm, outdoor_cold, dome, outdoor, grass, turf, precipitation, dry, division, non_division, primetime, day)"},
                    "season_year": {"type": "integer", "description": "Season year for a single-season split (defaults to career)"},
                },
                "required": ["player_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_player_stats",            "description": (
                "GENERAL-PURPOSE allowlisted player-stats query engine. Express ANY "
                "offensive/defensive/special-teams stat question as a structured spec. "
                "Use this when no specific get_* tool fits (e.g. \"most sacks on the road "
                "since 2022\", \"QBs by INTs in night home games\", arbitrary aggregates). "
                "Provide: stats (list of allowed stat names), optional aggregate "
                "(sum/avg/max/count, default sum), optional group_by (player/week), "
                "optional filters (season_year/week/min_week/max_week/team/opponent/"
                "home_or_away/game_type), optional top + order for leaderboards. "
                "Allowed stat names: " + ", ".join(sorted(__import__('app.chat_tools.nfl_query', fromlist=['NFL_COLUMNS']).NFL_COLUMNS.keys())) + ". "
                "Unsupported fields return an error, never injected SQL. season_year is "
                "the season START year (NFL season spans calendar years)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stats": {"type": "array", "items": {"type": "string"}, "description": "Stat name(s) to aggregate, e.g. ['sacks'], ['pass_yards','pass_td']. See description for full list."},
                    "aggregate": {"type": "string", "enum": ["sum", "avg", "max", "count"], "description": "Aggregate function (default sum)"},
                    "group_by": {"type": "array", "items": {"type": "string", "enum": ["player", "week"]}, "description": "Group results by player and/or week"},
                    "filters": {"type": "object", "description": "Optional filters dict: season_year(int), week(int), min_week(int), max_week(int), team(string), opponent(string), home_or_away(home|away), game_type(REG|PRE)"},
                    "player_name": {"type": "string", "description": "TOP-LEVEL single-player filter: the exact player name (e.g. 'Micah Parsons'). Use this, NOT a key inside 'filters'. Omit for a whole-league leaderboard (pair with group_by/top)."},
                    "position": {"type": "string", "description": "Optional: restrict to a position (QB, RB, WR, TE, LB, DE, DL, DT, CB, S, K, P)"},
                    "top": {"type": "integer", "description": "Limit rows (leaderboard). Use with order."},
                    "order": {"type": "string", "enum": ["desc", "asc"], "description": "Sort for leaderboards (default desc)"},
                },
                "required": ["stats"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_team_stats",
            "description": (
                "GENERAL-PURPOSE allowlisted team/game stats query engine. Express ANY "
                "team-level stat question as a structured spec. Use when no specific "
                "get_* team tool fits (e.g. \"Bills' record in October since 2022\", \"most "
                "total yards in 2024\", \"Chiefs turnover margin last season\", win% trends). "
                "Stats resolve to one source table automatically (nfl.games for records/points, "
                "nfl.game_stats for yards/turnovers/efficiency/advanced, nfl.team_rolling_stats for "
                "3/5/10-game windows). Provide: stats (allowed names), filters (team/season_year/"
                "opponent/home_or_away/min_week/max_week), optional top+order for leaderboards "
                "(group_by=['team'] for league-wide). Core stats: wins, losses, win_pct, "
                "points_for, avg_points_for, total_yards, avg_total_yards_per_game, pass_yards, "
                "rush_yards, turnovers, turnover_diff, sacks, third_down_pct, fourth_down_pct, "
                "red_zone_td_pct, passing_tds, rushing_tds, interceptions_thrown, fumbles. "
                "EFFICIENCY/ADVANCED: yards_per_play, pass_ypa, rush_ypa, passing_epa, rushing_epa, "
                "receiving_epa, passing_cpoe, explosive_plays, three_and_outs, passing_air_yards, "
                "passing_yards_after_catch, receiving_yards_after_catch, avg_time_of_possession_secs. "
                "DEFENSE DETAIL: def_tackles_solo, def_tackles_for_loss, def_sack_yards, def_qb_hits, "
                "def_fumbles_forced, def_pass_defended, def_interception_yards, def_tds, def_safeties. "
                "KICKING: fg_made, fg_attempts, fg_pct, fg_long, fg_made_50_59, pat_made, pat_pct. "
                "PUNTING: punts, punt_yards, punt_avg, punts_inside_20, punt_touchbacks, punt_net_avg. "
                "RETURNS: kickoff_returns, kickoff_return_yards, punt_returns, punt_return_yards. "
                "ROLLING (past-3/5/10-game windows): win_pct_5, off_yds_per_game_5, cover_pct_5, "
                "ou_over_pct_5, win_pct_3, win_pct_10. Unsupported fields return an error, never SQL. "
                "season_year is the season START year (NFL season spans calendar years)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stats": {"type": "array", "items": {"type": "string"}, "description": "Stat name(s) to compute, e.g. ['wins','losses'], ['total_yards']. See description for the list."},
                    "filters": {"type": "object", "description": "Optional filters dict: team(string), season_year(int), opponent(string), home_or_away(home|away), min_week(int), max_week(int)"},
                    "group_by": {"type": "array", "items": {"type": "string", "enum": ["team", "opponent"]}, "description": "Group by team/opponent (use top+order for a leaderboard, e.g. group_by=['team'])"},
                    "top": {"type": "integer", "description": "Limit rows (leaderboard)"},
                    "order": {"type": "string", "enum": ["desc", "asc"], "description": "Sort for leaderboards (default desc)"},
                },
                "required": ["stats"],
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
            "name": "get_team_game_log",
            "description": "Query an NFL team's game log by filters: date range or month, home/away, result, or opponent. Answers 'how many games did the Chiefs win in October', 'their record on the road', or 'how they've done vs the Bills'. Returns the record plus the per-game list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                    "month": {"type": "integer", "description": "Calendar month 1-12 to filter to (e.g. 10 for October)"},
                    "start_date": {"type": "string", "description": "Start date inclusive (ISO YYYY-MM-DD)"},
                    "end_date": {"type": "string", "description": "End date inclusive (ISO YYYY-MM-DD)"},
                    "home_or_away": {"type": "string", "enum": ["home", "away", "all"], "description": "Filter home or away games (default 'all')"},
                    "result": {"type": "string", "enum": ["win", "loss", "all"], "description": "Filter to wins or losses (default 'all')"},
                    "opponent": {"type": "string", "description": "Filter to games vs this opponent team name/abbr"},
                },
                "required": ["team_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_splits",
            "description": "Get an NFL team's situational splits for a season: overall/home/away records, plus home dome-vs-outdoor and turf-vs-grass splits when venue data is available. Answers 'how good are the Packers at home', 'dome vs outdoors', or 'on turf'.",
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
            "description": "Season-long futures odds. OMIT team_name to get ALL teams ranked by championship odds from favorite to biggest underdog (lowest number = best odds = favorite, e.g. -120 or +200 beats +4000). Provide team_name to get a single team's full futures (championship, make/miss playoffs, win total over/under).",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_name": {"type": "string", "description": "Team name or abbreviation (optional). Omit to rank all teams by championship odds, favorites first."},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_player_trends",
            "description": "Get a player's game-by-game stat trends (last N games) from the rolling-stats tables, position-aware: QBs -> passing, RB/WR/TE -> rushing+receiving, K/P -> kicking, defenders (LB/DE/DT/DL/CB/S) -> defensive tackles/sacks/INTs etc. Returns cumulative season totals plus 3/5/10-game rolling windows. Use for 'how is X trending', 'last 5 games', 'hot/cold' questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_name": {"type": "string", "description": "Player name"},
                    "season_year": {"type": "integer", "description": "Season year (defaults to current)"},
                    "last_n": {"type": "integer", "description": "Number of most-recent games to return (default 5; max 17)"},
                    "include_cumulative": {"type": "boolean", "description": "Include season-to-date cumulative totals in the response (default true)"},
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
    year = args.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)
    lim = min(args.get("limit", 10), 20)

    sql = text("""
        SELECT g.*, ht.name AS home_name, at2.name AS away_name
        FROM nfl.games g
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at2 ON at2.id = g.away_team_id
        WHERE ((g.home_team_id = :t1 AND g.away_team_id = :t2)
            OR (g.home_team_id = :t2 AND g.away_team_id = :t1))
          AND g.status = 'FINAL'
          AND g.season_id = :sid
        ORDER BY g.date DESC LIMIT :lim
    """)
    r = await db.execute(sql, {"t1": t1, "t2": t2, "sid": sid, "lim": lim})
    meetings = []
    t1_wins = 0
    t2_wins = 0
    t1_pts = 0
    t2_pts = 0
    for row in r.mappings():
        p1 = row.home_score if row.home_team_id == t1 else row.away_score
        p2 = row.away_score if row.home_team_id == t1 else row.home_score
        if p1 is not None and p2 is not None:
            t1_pts += p1
            t2_pts += p2
            if p1 > p2:
                t1_wins += 1
            else:
                t2_wins += 1
        winner_t1 = p1 is not None and p2 is not None and p1 > p2
        winner = row.home_name if winner_t1 else row.away_name
        meetings.append({
            "date": str(row.date) if row.date else None,
            "week": getattr(row, "week", None),
            "home": row.home_name,
            "away": row.away_name,
            "score": f"{row.home_score}-{row.away_score}",
            "winner": winner,
        })
    return {
        "team1": args.get("team1"),
        "team2": args.get("team2"),
        "season_year": year,
        "aggregate": {
            "games": len(meetings),
            "team1_wins": t1_wins,
            "team2_wins": t2_wins,
            "team1_points": t1_pts,
            "team2_points": t2_pts,
        },
        "meetings": meetings,
    }


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

    player, warn = await _resolve_player_record(db, player_name)
    if not player:
        return {"error": warn}

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
            COALESCE(SUM(receiving_tds), 0) AS rec_td,
            COALESCE(SUM(tackles_combined), 0) AS tackles,
            COALESCE(SUM(tackles_solo), 0) AS tackles_solo,
            COALESCE(SUM(tackles_assist), 0) AS tackles_assist,
            COALESCE(SUM(tfl), 0) AS tfl,
            COALESCE(SUM(sacks), 0) AS sacks,
            COALESCE(SUM(qb_hits), 0) AS qb_hits,
            COALESCE(SUM(hurries), 0) AS hurries,
            COALESCE(SUM(stuffs), 0) AS stuffs,
            COALESCE(SUM(passes_defended), 0) AS passes_defended,
            COALESCE(SUM(interceptions), 0) AS def_int,
            COALESCE(SUM(def_int_yards), 0) AS def_int_yards,
            COALESCE(SUM(fumbles_forced), 0) AS fumbles_forced,
            COALESCE(SUM(fumbles_recovered), 0) AS fumbles_recovered,
            COALESCE(SUM(defensive_tds), 0) AS defensive_tds,
            COALESCE(SUM(safeties), 0) AS safeties,
            COALESCE(SUM(kick_return_yards), 0) AS kret_yds,
            COALESCE(SUM(kick_return_tds), 0) AS kret_td,
            COALESCE(SUM(punt_return_yards), 0) AS pret_yds,
            COALESCE(SUM(punt_return_tds), 0) AS pret_td,
            COALESCE(SUM(punts), 0) AS punts,
            COALESCE(SUM(punt_yards), 0) AS punt_yds,
            COALESCE(SUM(field_goals_made), 0) AS fg,
            COALESCE(SUM(field_goals_attempted), 0) AS fga
        FROM nfl.player_weekly_stats
        WHERE player_id = :pid AND season_id = :sid
    """)
    r = await db.execute(sql, {"pid": player.id, "sid": sid})
    s = r.mappings().first()
    if not s or s.games_played == 0:
        return {"error": f"No stats for {player.name} in {year}"}

    # Team(s) the player suited up for this season (handles mid-season trades).
    team_sql = text("""
        SELECT t.name
        FROM (SELECT DISTINCT team_id FROM nfl.player_weekly_stats
              WHERE player_id = :pid AND season_id = :sid) d
        JOIN nfl.teams t ON t.id = d.team_id
        ORDER BY t.name
    """)
    tr = await db.execute(team_sql, {"pid": player.id, "sid": sid})
    teams = [row[0] for row in tr.fetchall()]

    return {
        "player": player.name,
        "position": player.position,
        "team": ", ".join(teams) if teams else None,
        "season_year": year,
        "games_played": s.games_played,
        "passing": {"yards": s.pass_yds, "tds": s.pass_td, "ints": s.ints},
        "rushing": {"attempts": s.rush_att, "yards": s.rush_yds, "tds": s.rush_td},
        "receiving": {"receptions": s.rec, "yards": s.rec_yds, "tds": s.rec_td},
        "defense": {
            "tackles": s.tackles, "tackles_solo": s.tackles_solo, "tackles_assist": s.tackles_assist,
            "tfl": s.tfl, "sacks": s.sacks, "qb_hits": s.qb_hits, "hurries": s.hurries,
            "stuffs": s.stuffs, "passes_defended": s.passes_defended, "interceptions": s.def_int,
            "int_yards": s.def_int_yards, "fumbles_forced": s.fumbles_forced,
            "fumbles_recovered": s.fumbles_recovered, "defensive_tds": s.defensive_tds,
            "safeties": s.safeties,
        },
        "special_teams": {
            "kick_return_yards": s.kret_yds, "kick_return_tds": s.kret_td,
            "punt_return_yards": s.pret_yds, "punt_return_tds": s.pret_td,
            "punts": s.punts, "punt_yards": s.punt_yds,
            "field_goals": f"{s.fg}/{s.fga}",
        },
    }


async def _get_team_splits(db: AsyncSession, args: dict) -> dict:
    """Compute an NFL team's situational splits from the games table: home/away,
    roof type (dome vs outdoor), and surface (turf vs grass). No splits table
    exists for NFL, so this derives the records from nfl.games directly.

    args:
        team_name: str (required)
        season_year: int (optional; default current)
    """
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    year = args.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    sql = text("""
        SELECT g.id, g.week, g.date, g.home_team_id, g.away_team_id,
               g.home_score, g.away_score, g.roof_type, g.surface, g.venue
        FROM nfl.games g
        WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
          AND g.season_id = :sid AND g.game_type = 'REG' AND g.status = 'FINAL'
        ORDER BY g.date
    """)
    r = await db.execute(sql, {"tid": tid, "sid": sid})
    rows = r.mappings().all()

    def result_and_pts(row):
        is_home = row.home_team_id == tid
        pf = row.home_score if is_home else row.away_score
        pa = row.away_score if is_home else row.home_score
        res = "win" if pf > pa else "loss"
        return res, pf, pa, is_home

    def bucket(label, rows):
        wins = losses = pf = pa = 0
        for row in rows:
            res, p1, p2, _ = result_and_pts(row)
            if res == "win":
                wins += 1
            else:
                losses += 1
            pf += p1
            pa += p2
        n = wins + losses
        return {
            "games": n,
            "record": f"{wins}-{losses}",
            "win_pct": round(wins / n, 3) if n else None,
            "points_for": pf,
            "points_against": pa,
            "point_diff": pf - pa,
        }

    home = [row for row in rows if row.home_team_id == tid]
    away = [row for row in rows if row.away_team_id == tid]

    # split by venue roof (only meaningful for the team's home games)
    def _is_turf(surface):
        s = str(surface or "").lower()
        return "turf" in s or "artificial" in s or "astroturf" in s

    dome = [row for row in home if str(row.roof_type or "").lower() in ("dome", "retractable")]
    outdoor = [row for row in home if row not in dome]
    turf = [row for row in home if row.surface and _is_turf(row.surface)]
    grass = [row for row in home if row.surface and not _is_turf(row.surface)]

    splits = {
        "overall": bucket("overall", rows),
        "home": bucket("home", home),
        "away": bucket("away", away),
    }
    # only include dome/outdoor/turf/grass when the venue data is populated
    if any(row.roof_type for row in home):
        splits["home_dome_or_retractable"] = bucket("dome", dome)
        splits["home_outdoor"] = bucket("outdoor", outdoor)
    if any(row.surface for row in home):
        splits["home_turf"] = bucket("turf", turf)
        splits["home_grass"] = bucket("grass", grass)

    return {"team": args.get("team_name"), "season_year": year, "splits": splits}


def _coerce_date(value):
    """Coerce a user-supplied date ('2026-08-23', '2026/08/23', already a date) to a
    datetime.date. The LLM passes dates as strings; asyncpg needs a date/datetime instance.
    Returns None if it can't be parsed (callers treat None as unset)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except (ValueError, TypeError):
                continue
        try:
            return date.fromisoformat(value.strip())
        except (ValueError, TypeError):
            return None
    return None


async def _get_team_game_log(db: AsyncSession, args: dict) -> dict:
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    year = args.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    month = args.get("month")
    start_date = args.get("start_date")
    end_date = args.get("end_date")
    hod = (args.get("home_or_away") or "all").lower()
    result_filter = (args.get("result") or "all").lower()
    opponent = args.get("opponent")

    frags = ["(g.home_team_id = :tid OR g.away_team_id = :tid)", "g.season_id = :sid"]
    if hod == "home":
        frags.append("g.home_team_id = :tid")
    elif hod == "away":
        frags.append("g.away_team_id = :tid")
    if month is not None:
        frags.append("EXTRACT(MONTH FROM g.date) = :month")
    if start_date:
        frags.append("g.date >= :start_date")
    if end_date:
        frags.append("g.date <= :end_date")
    if opponent:
        oid = await _resolve_team_id(db, opponent)
        if not oid:
            return {"error": f"Opponent team not found: {opponent}"}
        frags.append("(g.home_team_id = :oid OR g.away_team_id = :oid)")

    params = {"tid": tid, "sid": sid, "month": month,
              "start_date": _coerce_date(start_date), "end_date": _coerce_date(end_date)}
    if opponent:
        oid = await _resolve_team_id(db, opponent)
        if not oid:
            return {"error": f"Opponent team not found: {opponent}"}
        params["oid"] = oid
    where = " AND ".join(frags)

    sql = text(f"""
        SELECT g.id, g.week, g.date, g.home_team_id, g.away_team_id,
               g.home_score, g.away_score, ht.name AS home_name, at2.name AS away_name
        FROM nfl.games g
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at2 ON at2.id = g.away_team_id
        WHERE {where}
          AND g.game_type = 'REG' AND g.status = 'FINAL'
        ORDER BY g.week ASC
    """)
    r = await db.execute(sql, params)
    rows = r.mappings().all()

    def result_for(row):
        if row.home_team_id == tid:
            return "win" if row.home_score > row.away_score else "loss"
        return "win" if row.away_score > row.home_score else "loss"

    if result_filter in ("win", "loss"):
        rows = [row for row in rows if result_for(row) == result_filter]

    wins = sum(1 for row in rows if result_for(row) == "win")
    losses = sum(1 for row in rows if result_for(row) == "loss")
    scored = sum((row.home_score if row.home_team_id == tid else row.away_score) or 0 for row in rows)
    allowed = sum((row.away_score if row.home_team_id == tid else row.home_score) or 0 for row in rows)

    games_list = []
    for row in rows[:20]:
        opp = row.away_name if row.home_team_id == tid else row.home_name
        games_list.append({
            "game_id": row.id,
            "week": row.week,
            "date": str(row.date),
            "home": row.home_team_id == tid,
            "opponent": opp,
            "result": result_for(row),
            "points_for": row.home_score if row.home_team_id == tid else row.away_score,
            "points_against": row.away_score if row.home_team_id == tid else row.home_score,
        })

    return {
        "team": args.get("team_name"),
        "season_year": year,
        "filters": {"month": month, "start_date": start_date, "end_date": end_date,
                     "home_or_away": hod, "result": result_filter, "opponent": opponent},
        "games_played": len(rows),
        "wins": wins,
        "losses": losses,
        "win_pct": round(wins / len(rows), 3) if rows else None,
        "points_for": scored,
        "points_against": allowed,
        "point_diff": scored - allowed,
        "games": games_list,
    }


async def _get_player_weekly_log(db: AsyncSession, args: dict) -> dict:
    player_name = args.get("player_name", "")
    year = args.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    month = args.get("month")
    start_date = args.get("start_date")
    end_date = args.get("end_date")
    hod = (args.get("home_or_away") or "all").lower()
    opponent = args.get("opponent")

    player, warn = await _resolve_player_record(db, player_name)
    if not player:
        return {"error": warn}

    frags = ["pws.player_id = :pid", "pws.season_id = :sid"]
    params = {"pid": player.id, "sid": sid, "month": month,
              "start_date": _coerce_date(start_date), "end_date": _coerce_date(end_date)}
    if hod == "home":
        frags.append("g.home_team_id = pws.team_id")
    elif hod == "away":
        frags.append("g.away_team_id = pws.team_id")
    if month is not None:
        frags.append("EXTRACT(MONTH FROM g.date) = :month")
    if start_date:
        frags.append("g.date >= :start_date")
    if end_date:
        frags.append("g.date <= :end_date")
    if opponent:
        oid = await _resolve_team_id(db, opponent)
        if not oid:
            return {"error": f"Opponent team not found: {opponent}"}
        params["oid"] = oid
        frags.append("(g.home_team_id = :oid OR g.away_team_id = :oid)")
    where = " AND ".join(frags)

    sql = text(f"""
        SELECT pws.*, g.week,
               t.name AS team_name,
               ht.name AS opponent_name,
               CASE WHEN g.home_team_id = pws.team_id THEN 'home' ELSE 'away' END AS venue
        FROM nfl.player_weekly_stats pws
        JOIN nfl.teams t ON t.id = pws.team_id
        JOIN nfl.games g ON g.id = pws.game_id
        LEFT JOIN nfl.teams ht ON ht.id = CASE
            WHEN g.home_team_id = pws.team_id THEN g.away_team_id
            ELSE g.home_team_id END
        WHERE {where}
        ORDER BY g.week ASC
    """)
    r = await db.execute(sql, params)
    games = []
    for row in r.mappings():
        games.append({
            "week": row.week,
            "team": row.team_name,
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
            "tackles": row.tackles_combined,
            "sacks": row.sacks,
            "tfl": row.tfl,
            "qb_hits": row.qb_hits,
            "passes_defended": row.passes_defended,
            "interceptions": row.interceptions,
            "fumbles_forced": row.fumbles_forced,
            "fumbles_recovered": row.fumbles_recovered,
            "kick_return_yards": row.kick_return_yards,
            "punt_return_yards": row.punt_return_yards,
        })

    # Aggregate the filtered games (pass/rush/receiving totals + per-game) when
    # any filter dimension is active, so 'how did he do in October' returns a line.
    aggregate = None
    if any([month, start_date, end_date, hod != "all", opponent]):
        n = len(games)
        def _s(key):
            return sum((g.get(key) or 0) for g in games)
        py, pt = _s("pass_yds"), _s("pass_td")
        ry, rt = _s("rush_yds"), _s("rush_td")
        rec, ryd, rtd, tar = _s("rec"), _s("rec_yds"), _s("rec_td"), _s("targets")
        tk, sk, qh = _s("tackles"), _s("sacks"), _s("qb_hits")
        di, ff, fr = _s("interceptions"), _s("fumbles_forced"), _s("fumbles_recovered")
        aggregate = {
            "games": n,
            "pass_yards": py, "pass_td": pt,
            "rush_yards": ry, "rush_td": rt,
            "receptions": rec, "targets": tar, "receiving_yards": ryd, "receiving_td": rtd,
            "tackles": tk, "sacks": sk, "qb_hits": qh,
            "interceptions": di, "fumbles_forced": ff, "fumbles_recovered": fr,
            "pass_yards_per_game": round(py / n, 1) if n else None,
            "rush_yards_per_game": round(ry / n, 1) if n else None,
            "receiving_yards_per_game": round(ryd / n, 1) if n else None,
            "total_tds": pt + rt + rtd + _s("defensive_tds") + _s("kick_return_tds") + _s("punt_return_tds"),
        }

    return {
        "player": player.name,
        "team": games[0]["team"] if games else None,
        "season_year": year,
        "filters": {"month": month, "start_date": start_date, "end_date": end_date,
                     "home_or_away": hod, "opponent": opponent},
        "aggregate": aggregate,
        "game_logs": games,
    }


async def _get_player_trends(db: AsyncSession, args: dict) -> dict:
    """Position-aware player trend tool reading the rolling-stats tables.
    QBs -> qb_rolling_stats; RB/WR/TE -> skill_rolling_stats;
    K/P -> kicker_rolling_stats; defenders -> defensive_rolling_stats.
    Returns per-game (last N) + cumulative + rolling-window stats."""
    from sqlalchemy import text as _text

    player_name = args.get("player_name", "")
    year = args.get("season_year") or await _resolve_season_year(db)
    try:
        last_n = min(max(int(args.get("last_n", 5)), 1), 17)
    except (TypeError, ValueError):
        last_n = 5
    include_cum = bool(args.get("include_cumulative", True))
    player, warn = await _resolve_player_record(db, player_name)
    if not player:
        return {"error": warn}

    pos = (player.position or "").upper()
    # Choose rolling table + per-game columns by position family.
    if pos in ("QB",):
        table, gcols, ccols = "qb_rolling_stats", \
            ["pass_attempts", "pass_completions", "pass_yards", "pass_tds", "pass_int"], \
            []
        labels = {"pass_attempts": "pass_att", "pass_completions": "pass_comp",
                  "pass_yards": "pass_yds", "pass_tds": "pass_td", "pass_int": "int"}
        rolling = ["pass_yds_3", "pass_yds_5", "pass_yds_10",
                   "pass_td_3", "passer_rating_3", "ypa_3"]
    elif pos in ("K", "P"):
        table, gcols, ccols = "kicker_rolling_stats", \
            ["fg_made", "fg_attempted", "xp_made", "xp_attempted"], \
            ["cum_fg_made", "cum_fg_att", "cum_xp_made", "cum_xp_att"]
        labels = {"fg_made": "fg", "fg_attempted": "fga", "xp_made": "xp",
                  "xp_attempted": "xpa"}
        rolling = ["fg_made_3", "fg_made_5", "fg_made_10"]
    elif pos in ("CB", "DE", "DL", "DT", "LB", "S"):
        table, gcols, ccols = "defensive_rolling_stats", \
            ["tackles", "solo", "assist", "sacks", "tfl", "qb_hits", "passes_defended",
             "interceptions", "fumbles_forced", "fumbles_recovered"], \
            ["cum_sacks", "cum_tackles", "cum_interceptions", "cum_ff", "cum_fr", "cum_qb_hits", "cum_pd", "cum_tfl"]
        labels = {"tackles": "tack", "solo": "solo", "assist": "ast", "sacks": "sacks",
                  "tfl": "tfl", "qb_hits": "qb_hits", "passes_defended": "pd",
                  "interceptions": "int", "fumbles_forced": "ff", "fumbles_recovered": "fr"}
        rolling = ["sacks_3", "sacks_5", "sacks_10", "tackles_3", "tackles_5", "tackles_10"]
    else:
        # RB/WR/TE (and any default offensive skill)
        table, gcols, ccols = "skill_rolling_stats", \
            ["rush_attempts", "rush_yards", "rush_tds", "receptions", "receiving_yards",
             "receiving_tds", "targets", "fumbles"], \
            ["cum_rush_yds", "cum_rush_td", "cum_rec", "cum_recv_yds", "cum_recv_td"]
        labels = {"rush_attempts": "rush_att", "rush_yards": "rush_yds", "rush_tds": "rush_td",
                  "receptions": "rec", "receiving_yards": "rec_yds", "receiving_tds": "rec_td",
                  "targets": "tar", "fumbles": "fum"}
        rolling = ["rush_yds_3", "rush_yds_5", "rush_yds_10",
                   "recv_yds_3", "recv_yds_5", "recv_yds_10"]

    # Pull the player's rows for that season from the chosen table.
    # SELECT also pulls cumulative + rolling columns so trend extraction works.
    meta = ["game_id", "week", "team_abbr", "opponent_abbr", "game_date", "game_type"]
    select_cols = list(dict.fromkeys(meta + gcols + ccols + rolling))
    sql = _text(f"""
        SELECT {', '.join(select_cols)}
        FROM nfl.{table}
        WHERE player_id = :pid AND season = :year
        ORDER BY game_date, game_id
    """)
    r = await db.execute(sql, {"pid": player.id, "year": year})
    rows = r.mappings().all()
    if not rows:
        return {"player": player.name, "position": pos, "season": year,
                "error": f"No trend data for {year} (table {table})"}

    last_rows = rows[-last_n:]
    games = []
    for row in last_rows:
        entry = {"week": row.week, "team": row.team_abbr, "opponent": row.opponent_abbr,
                 "game_date": str(row.game_date), "game_type": row.game_type}
        for gc in gcols:
            entry[labels.get(gc, gc)] = getattr(row, gc)
        games.append(entry)

    trend = {}
    if include_cum and rows:
        lastrow = rows[-1]
        for cc in ccols:
            if hasattr(lastrow, cc):
                trend[cc] = getattr(lastrow, cc)
        # rolling windows from the last row
        for rc in rolling:
            if hasattr(lastrow, rc):
                trend[rc] = getattr(lastrow, rc)

    return {
        "player": player.name,
        "position": pos,
        "season": year,
        "stat_category": "defense" if pos in ("CB", "DE", "DL", "DT", "LB", "S") else
                          ("kicking" if pos in ("K", "P") else "offense"),
        "last_n": len(games),
        "trend": trend if include_cum else None,
        "game_logs": games,
    }


async def _get_player_splits(db: AsyncSession, args: dict) -> dict:
    """Situational/career splits from nfl.player_splits (home/away, weather,
    dome, surface, division, primetime). season_year optional -> single season."""
    player_name = args.get("player_name", "")
    split_type = (args.get("split_type") or "").strip().lower()
    year = args.get("season_year")

    # Resolve player (conservative: prefers espn_id-linked row, never guesses)
    player, warn = await _resolve_player_record(db, player_name)
    if not player:
        return {"error": warn}

    season_scope = None
    if year:
        sid = await _resolve_season_id(db, year)
        season_scope = sid

    where = "player_id = :pid"
    params = {"pid": player.id}
    if season_scope:
        where += " AND season_id = :sid"
        params["sid"] = season_scope
    else:
        where += " AND season_id IS NULL"  # career aggregate
    if split_type:
        where += " AND split_type = :st"
        params["st"] = split_type

    sql = text(f"""
        SELECT split_type, games_played, pass_attempts, pass_completions, pass_yards,
               pass_tds, pass_int, rush_attempts, rush_yards, rush_tds,
               targets, receptions, receiving_yards, receiving_tds, fumbles,
               ypc, ypr
        FROM nfl.player_splits
        WHERE {where}
        ORDER BY split_type
    """)
    rows = (await db.execute(sql, params)).mappings().all()
    if not rows:
        return {"error": f"No splits found for {player.name}"}

    def _fmt(row) -> dict:
        rate = None
        att = row.pass_attempts
        if att and att > 0:
            rate = _passer_rating(
                att, row.pass_completions or 0, row.pass_yards or 0,
                row.pass_tds or 0, row.pass_int or 0,
            )
        return {
            "split": row.split_type,
            "games": row.games_played,
            "passing": {"yards": row.pass_yards, "tds": row.pass_tds, "ints": row.pass_int,
                        "rating": rate, "cmppct": round((row.pass_completions or 0) / att * 100, 1) if att else None},
            "rushing": {"attempts": row.rush_attempts, "yards": row.rush_yards, "tds": row.rush_tds,
                        "ypc": row.ypc},
            "receiving": {"targets": row.targets, "receptions": row.receptions,
                          "yards": row.receiving_yards, "tds": row.receiving_tds,
                          "ypr": row.ypr},
            "fumbles": row.fumbles,
        }

    splits = {r.split_type: _fmt(r) for r in rows}
    scope = "career" if not season_scope else f"{year}"
    return {"player": player.name, "position": player.position, "scope": scope, "splits": splits}


def _passer_rating(att, comp, yds, td, intc):
    """Standard NFL passer rating (0-158.3)."""
    if not att:
        return None
    a = ((comp / att) - 0.30) * 5.0
    b = ((yds / att) - 3.0) * 0.25
    c = (td / att) * 20.0
    d = 2.375 - ((intc / att) * 25.0)
    for v in (a, b, c, d):
        if v < 0:
            v = 0
        elif v > 2.375:
            v = 2.375
    return round(((a + b + c + d) / 6.0) * 100.0, 1)


async def _get_team_trends(db: AsyncSession, args: dict) -> dict:
    """Recent performance trends from nfl.team_rolling_stats."""
    abbr = await _resolve_team_abbr(db, args.get("team_name", ""))
    if not abbr:
        return {"error": f"Team not found: {args.get('team_name', '')}"}
    season = args.get("season_year") or await _resolve_data_season_year(db)
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
    season = args.get("season_year") or await _resolve_data_season_year(db)

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
    """Team season futures odds from nfl.team_props.

    If a team_name is given, returns that team's full futures. If omitted,
    returns all teams ranked by championship odds: the team with the SMALLEST
    number (best/most negative odds) is the favorite and sorts first, up to
    the LARGEST number (most positive / biggest underdog) last.
    """
    season = args.get("season_year") or await _resolve_props_season(db)

    # ----- ranked list of all teams (no team_name) -----
    if not args.get("team_name", "").strip():
        sql = text(
            """SELECT m.name AS team_name, p.bookmaker, p.championship_odds,
                      p.win_total, p.win_total_over_odds, p.win_total_under_odds,
                      p.make_playoffs_odds
               FROM nfl.team_props p
               JOIN nfl.teams m ON m.id = p.team_id
               WHERE p.season_year = :season AND p.championship_odds IS NOT NULL
               ORDER BY p.championship_odds ASC  -- smallest = favorite first
               """
        )
        r = await db.execute(sql, {"season": season})
        rows = r.mappings().all()
        if not rows:
            return {"error": f"No season futures found for season {season}"}

        # Collapse to best (lowest) championship odds per team, keeping the book.
        best: dict[str, dict] = {}
        for row in rows:
            team = row.team_name
            if team not in best or row.championship_odds < best[team]["championship_odds"]:
                best[team] = {
                    "team_name": team,
                    "bookmaker": row.bookmaker,
                    "championship_odds": round(float(row.championship_odds), 1),
                    "win_total": round(float(row.win_total), 1) if row.win_total is not None else None,
                    "make_playoffs_odds": round(float(row.make_playoffs_odds), 1) if row.make_playoffs_odds is not None else None,
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

    # ----- single-team lookup (legacy path) -----
    tid = await _resolve_team_id(db, args.get("team_name", ""))
    if not tid:
        return {"error": f"Team not found: {args.get('team_name', '')}"}

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

# Alias map: model-invoked name variants -> canonical registered tool name.
_TOOL_ALIASES = {
    "get_team_futures": "get_team_season_futures",
    "get_team_future": "get_team_season_futures",
    "get_team_season_future": "get_team_season_futures",
    "get_player_props": "get_player_season_props",
    "get_player_season_prop": "get_player_season_props",
    "get_defense_rankings": "get_team_defense_rankings",
    "get_defense_ranking": "get_team_defense_rankings",
    "get_player_split_stats": "get_player_splits",
    "get_player_situational": "get_player_splits",
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
        FROM nfl.game_writeups w
        JOIN nfl.games g ON g.id = w.game_id
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at2 ON at2.id = g.away_team_id
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

async def _get_player_query(db: AsyncSession, args: dict) -> dict:
    """query_player_stats dispatcher — lazily imports the nfl_query engine to
    avoid a circular import (nfl_query imports _resolve_* helpers from here)."""
    from . import nfl_query
    return await nfl_query._run_query_player_stats(db, args)


async def _get_team_query(db: AsyncSession, args: dict) -> dict:
    """query_team_stats dispatcher — same lazy-import pattern as _get_player_query."""
    from . import nfl_query
    return await nfl_query._run_query_team_stats(db, args)


_TOOL_HANDLERS = {
    "get_team_info": _get_team_info,
    "get_team_stats": _get_team_stats,
    "get_standings": _get_standings,
    "get_todays_games": _get_todays_games,
    "get_week_games": _get_week_games,
    "get_game_info": _get_game_info,
    "get_game_writeup": _get_game_writeup,
    "get_head_to_head": _get_head_to_head,
    "get_injuries": _get_injuries,
    "get_depth_chart": _get_depth_chart,
    "get_player_stats": _get_player_stats,
    "get_player_weekly_log": _get_player_weekly_log,
    "get_player_trends": _get_player_trends,
    "get_player_splits": _get_player_splits,
    "query_player_stats": _get_player_query,
    "query_team_stats": _get_team_query,
    "get_game_prediction": _get_game_prediction,
    "search_articles": _search_articles,
    "get_team_schedule": _get_team_schedule,
    "get_team_game_log": _get_team_game_log,
    "get_team_splits": _get_team_splits,
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
    func_name = _normalize_tool_name(tool_call.function.name)
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
