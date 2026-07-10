"""MLB-specific tool definitions and executors for the tool-calling chat engine.

Exports:
    TOOL_DEFINITIONS: List of OpenAI function-calling schemas.
    execute_mlb_tool: Async dispatcher that runs the right DB query.
"""

import json
import logging
from datetime import date, datetime, timedelta, timezone as dt_timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mlb import (
    MLBTeam,
    MLBPlayer,
    MLBSeason,
    MLBGamePrediction,
    MLBTeamSplit,
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
            "description": "Search for relevant news articles using semantic search. Returns article titles and summaries.",
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
                },
                "required": ["query"],
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
    """Search for articles via pgvector."""
    from app.ingestion.pgvector_search import search_articles

    query = args.get("query", "")
    limit = min(args.get("limit", 8), 15)

    articles = await search_articles(db, query, top_k=limit, sport="mlb")
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

_TOOL_MAP = {
    "search_teams": _search_teams,
    "get_team_stats": _get_team_stats,
    "get_team_batting_stats": _get_team_batting_stats,
    "get_team_pitching_stats": _get_team_pitching_stats,
    "get_standings": _get_standings,
    "get_todays_games": _get_todays_games,
    "get_game_info": _get_game_info,
    "get_head_to_head": _get_head_to_head,
    "get_injuries": _get_injuries,
    "get_player_stats": _get_player_stats,
    "get_game_prediction": _get_game_prediction,
    "get_team_splits": _get_team_splits,
    "search_articles": _search_articles_tool,
}


async def execute_mlb_tool(db: AsyncSession, tool_call) -> str:
    """Execute an MLB tool call and return a JSON result string.

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

    if func_name not in _TOOL_MAP:
        logger.warning("Unknown MLB tool called: %s", func_name)
        return json.dumps({"error": f"Unknown MLB tool: {func_name}"})

    logger.info("Executing MLB tool: %s args=%s", func_name, args)
    result = await _TOOL_MAP[func_name](db, args)
    return json.dumps(result, default=str, ensure_ascii=False)
