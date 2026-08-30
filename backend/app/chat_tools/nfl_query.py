"""
query_player_stats — a controlled, allowlisted query engine for NFL player stats.

The pitch (Rich): a "one tool to express arbitrary stat questions" that closes the
class of gap where a stat question needs a column/table an existing bespoke tool
doesn't expose. Instead of writing a new tool per question, the model expresses a
structured, TYPED filter spec, and this engine turns it into a parameterized query
against a strictly allowlisted set of columns/filters/aggregates.

SAFETY & ACCURACY RULES (non-negotiable):
  * NEVER raw SQL from the model. The model supplies only *field names + values* in
    a structured dict. All SQL is built by this module.
  * Strict allowlist for stat columns, filters, and aggregate functions. Anything
    not on the allowlist -> tool returns an explicit error ("unsupported"), never
    injected.
  * Season semantics are encoded here: season_year is the NFL season START year
    (a season spans calendar years). Month/week filters are scoped via season_id,
    NOT EXTRACT(YEAR), so "April"/"week 3" can't leak across seasons.
  * Aggregates SUM/AVG/MAX/COUNT only. No arbitrary expressions. GROUP BY only the
    requested stat columns (usually none -> season total per player).

The model sees the SCHEMA (tool parameters) and this module's docstring surfaces as
the tool description so the LLM knows: which stat groups exist, which filters are
supported, and that an unsupported field returns an error rather than guessing.

This is the FIRST sport (NFL). If the pattern lands cleanly it ports to NBA/MLB
(their player_weekly_stats / batting-game-stats have analogous column sets).
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ._query_guard import apply_limit, count_note, async_count

from .nfl import (
    _resolve_player_record,
    _resolve_season_id,
    _resolve_team_id,
    _resolve_team_name,
    _resolve_season_year,
)

logger = logging.getLogger("nfl_query")

# ---------------------------------------------------------------------------
# ALLOWLIST: stat columns the model may SELECT/aggregate.
# key = the friendly name the model uses; sql = the real column; group = for docs.
# Only these may appear in "stats" / "group_by".
# ---------------------------------------------------------------------------
NFL_COLUMNS: dict[str, dict] = {
    # passing
    "pass_yards": {"sql": "pass_yards", "group": "passing", "type": "float"},
    "pass_td": {"sql": "pass_tds", "group": "passing", "type": "int"},
    "pass_int": {"sql": "pass_int", "group": "passing", "type": "int"},
    "pass_attempts": {"sql": "pass_attempts", "group": "passing", "type": "int"},
    "pass_completions": {"sql": "pass_completions", "group": "passing", "type": "int"},
    "passer_rating": {"sql": "passer_rating", "group": "passing", "type": "float"},
    "pass_sacks": {"sql": "pass_sacks", "group": "passing", "type": "int"},
    "pass_sack_yards": {"sql": "pass_sack_yards", "group": "passing", "type": "int"},
    # rushing
    "rush_yards": {"sql": "rush_yards", "group": "rushing", "type": "float"},
    "rush_td": {"sql": "rush_tds", "group": "rushing", "type": "int"},
    "rush_attempts": {"sql": "rush_attempts", "group": "rushing", "type": "int"},
    "rush_long": {"sql": "rush_long", "group": "rushing", "type": "int"},
    # receiving
    "receptions": {"sql": "receptions", "group": "receiving", "type": "int"},
    "targets": {"sql": "targets", "group": "receiving", "type": "int"},
    "receiving_yards": {"sql": "receiving_yards", "group": "receiving", "type": "float"},
    "receiving_td": {"sql": "receiving_tds", "group": "receiving", "type": "int"},
    "receiving_long": {"sql": "receiving_long", "group": "receiving", "type": "int"},
    # defensive (the NEW columns)
    "tackles_combined": {"sql": "tackles_combined", "group": "defensive", "type": "int"},
    "tackles_solo": {"sql": "tackles_solo", "group": "defensive", "type": "int"},
    "tackles_assist": {"sql": "tackles_assist", "group": "defensive", "type": "int"},
    "tfl": {"sql": "tackles_for_loss", "group": "defensive", "type": "int"},
    "sacks": {"sql": "sacks", "group": "defensive", "type": "float"},
    "qb_hits": {"sql": "qb_hits", "group": "defensive", "type": "int"},
    "hurries": {"sql": "hurries", "group": "defensive", "type": "int"},
    "stuffs": {"sql": "stuffs", "group": "defensive", "type": "int"},
    "passes_defended": {"sql": "passes_defended", "group": "defensive", "type": "int"},
    "def_int": {"sql": "interceptions", "group": "defensive", "type": "int"},
    "def_int_yards": {"sql": "interception_yards", "group": "defensive", "type": "int"},
    "def_int_td": {"sql": "interception_tds", "group": "defensive", "type": "int"},
    "fumbles_forced": {"sql": "fumbles_forced", "group": "defensive", "type": "int"},
    "fumbles_recovered": {"sql": "fumbles_recovered", "group": "defensive", "type": "int"},
    "defensive_td": {"sql": "defensive_tds", "group": "defensive", "type": "int"},
    "safeties": {"sql": "safeties", "group": "defensive", "type": "int"},
    "stuffs_qb": {"sql": "passes_batted_down", "group": "defensive", "type": "int"},
    # special teams
    "kick_returns": {"sql": "kick_returns", "group": "special_teams", "type": "int"},
    "kick_return_yards": {"sql": "kick_return_yards", "group": "special_teams", "type": "int"},
    "kick_return_td": {"sql": "kick_return_tds", "group": "special_teams", "type": "int"},
    "punt_returns": {"sql": "punt_returns", "group": "special_teams", "type": "int"},
    "punt_return_yards": {"sql": "punt_return_yards", "group": "special_teams", "type": "int"},
    "punt_return_td": {"sql": "punt_return_tds", "group": "special_teams", "type": "int"},
    "punts": {"sql": "punts", "group": "special_teams", "type": "int"},
    "punt_yards": {"sql": "punt_yards", "group": "special_teams", "type": "int"},
    "punts_inside_20": {"sql": "punts_inside_20", "group": "special_teams", "type": "int"},
    "field_goals_made": {"sql": "field_goals_made", "group": "special_teams", "type": "int"},
    "field_goals_attempted": {"sql": "field_goals_attempted", "group": "special_teams", "type": "int"},
    # other
    "fumbles": {"sql": "fumbles", "group": "misc", "type": "int"},
    "fumbles_lost": {"sql": "fumbles_lost", "group": "misc", "type": "int"},
    "snaps_defense": {"sql": "snaps_defense", "group": "snaps", "type": "int"},
    "snaps_offense": {"sql": "snaps_offense", "group": "snaps", "type": "int"},
}

# ---------------------------------------------------------------------------
# ALLOWLIST: filters the model may apply. Each maps to a parameterized predicate.
# key = filter name the model uses.
# ---------------------------------------------------------------------------
SUPPORTED_FILTERS = {
    "season_year", "week", "min_week", "max_week",
    "team", "opponent", "home_or_away", "game_type",
}

AGG_WHITELIST = {"sum", "avg", "max", "count"}

# ---------------------------------------------------------------------------
# TEAM-STAT QUERY ENGINE (games + game_stats + team_rolling_stats)
# Each stat maps to a source table + SQL expression. All stats in one query must
# share a source (validated). "computed" source stats (record/points/win_pct)
# derive from nfl.games scores — no redundant `won` column is ever used.
# ---------------------------------------------------------------------------
GAMES_BASE = """
FROM nfl.games g
WHERE ((g.home_team_id = :tid) OR (g.away_team_id = :tid))
"""

# stat -> (source, sql_expr). :tid/:opp bound by engine.
TEAM_STATS = {
    # --- computed from nfl.games scores (source games) ---
    "wins": ("games", "SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score > g.away_score) OR (g.away_team_id = :tid AND g.away_score > g.home_score) THEN 1 ELSE 0 END)"),
    "losses": ("games", "SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score < g.away_score) OR (g.away_team_id = :tid AND g.away_score < g.home_score) THEN 1 ELSE 0 END)"),
    "ties": ("games", "SUM(CASE WHEN g.home_score = g.away_score THEN 1 ELSE 0 END)"),
    "games_played": ("games", "COUNT(*)"),
    "points_for": ("games", "SUM(CASE WHEN g.home_team_id = :tid THEN g.home_score ELSE g.away_score END)"),
    "points_against": ("games", "SUM(CASE WHEN g.home_team_id = :tid THEN g.away_score ELSE g.home_score END)"),
    "point_margin": ("games", "SUM(CASE WHEN g.home_team_id = :tid THEN g.home_score - g.away_score ELSE g.away_score - g.home_score END)"),
    "avg_points_for": ("games", "AVG(CASE WHEN g.home_team_id = :tid THEN g.home_score ELSE g.away_score END)"),
    "avg_points_against": ("games", "AVG(CASE WHEN g.home_team_id = :tid THEN g.away_score ELSE g.home_score END)"),
    "avg_point_margin": ("games", "AVG(CASE WHEN g.home_team_id = :tid THEN g.home_score - g.away_score ELSE g.away_score - g.home_score END)"),
    "win_pct": ("games", "ROUND(100.0 * SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score > g.away_score) OR (g.away_team_id = :tid AND g.away_score > g.home_score) THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1)"),
    # --- nfl.game_stats columns (source game_stats) ---
    "total_yards": ("game_stats", "SUM(gs.total_yards)"),
    "avg_total_yards": ("game_stats", "AVG(gs.total_yards)"),
    "pass_yards": ("game_stats", "SUM(gs.pass_yards)"),
    "rush_yards": ("game_stats", "SUM(gs.rush_yards)"),
    "def_yards_allowed": ("game_stats", "SUM(gs.def_yards_allowed)"),
    "avg_def_yards_allowed": ("game_stats", "AVG(gs.def_yards_allowed)"),
    "turnovers": ("game_stats", "SUM(gs.turnovers)"),
    "takeaways": ("game_stats", "SUM(gs.takeaways)"),
    "turnover_diff": ("game_stats", "SUM(gs.turnover_diff)"),
    "sacks": ("game_stats", "SUM(gs.def_sacks)"),
    "sacks_suffered": ("game_stats", "SUM(gs.sacks_suffered)"),
    "pass_interceptions": ("game_stats", "SUM(gs.pass_interceptions)"),
    "first_downs": ("game_stats", "SUM(gs.first_downs)"),
    "penalty_yards": ("game_stats", "SUM(gs.penalty_yards)"),
    "avg_total_yards_per_game": ("game_stats", "AVG(gs.total_yards)"),
    "avg_rush_yards_per_game": ("game_stats", "AVG(gs.rush_yards)"),
    "avg_pass_yards_per_game": ("game_stats", "AVG(gs.pass_yards)"),
    "avg_turnover_margin": ("game_stats", "AVG(gs.turnover_diff)"),
    "epa_per_play": ("game_stats", "AVG(gs.total_yards) / NULLIF(AVG(gs.total_yards),0)"),
    "third_down_pct": ("game_stats", "ROUND(100.0 * SUM(gs.third_down_conversions) / NULLIF(SUM(gs.third_down_attempts),0), 1)"),
    "fourth_down_pct": ("game_stats", "ROUND(100.0 * SUM(gs.fourth_down_conversions) / NULLIF(SUM(gs.fourth_down_attempts),0), 1)"),
    "red_zone_td_pct": ("game_stats", "ROUND(100.0 * SUM(gs.red_zone_tds) / NULLIF(SUM(gs.red_zone_trips),0), 1)"),
    # --- nfl.game_stats expanded columns (2026-08-29) ---
    # passing / rushing efficiency + volume
    "pass_ypa": ("game_stats", "ROUND((SUM(gs.pass_yards)::numeric) / NULLIF(SUM(gs.pass_attempts),0), 1)"),
    "rush_ypa": ("game_stats", "ROUND((SUM(gs.rush_yards)::numeric) / NULLIF(SUM(gs.rush_attempts),0), 1)"),
    "yards_per_play": ("game_stats", "ROUND((SUM(gs.total_yards)::numeric) / NULLIF(SUM(gs.pass_attempts + gs.rush_attempts),0), 1)"),
    "passing_epa": ("game_stats", "SUM(gs.passing_epa)"),
    "rushing_epa": ("game_stats", "SUM(gs.rushing_epa)"),
    "receiving_epa": ("game_stats", "SUM(gs.receiving_epa)"),
    "passing_cpoe": ("game_stats", "AVG(gs.passing_cpoe)"),
    # air yards / YAC
    "passing_air_yards": ("game_stats", "SUM(gs.passing_air_yards)"),
    "passing_yards_after_catch": ("game_stats", "SUM(gs.passing_yards_after_catch)"),
    "receiving_air_yards": ("game_stats", "SUM(gs.receiving_air_yards)"),
    "receiving_yards_after_catch": ("game_stats", "SUM(gs.receiving_yards_after_catch)"),
    # depth / down / situation
    "explosive_plays": ("game_stats", "SUM(gs.explosive_plays)"),
    "three_and_outs": ("game_stats", "SUM(gs.three_and_outs)"),
    "passing_first_downs": ("game_stats", "SUM(gs.passing_first_downs)"),
    "rushing_first_downs": ("game_stats", "SUM(gs.rushing_first_downs)"),
    "receiving_first_downs": ("game_stats", "SUM(gs.receiving_first_downs)"),
    "passing_2pt_conversions": ("game_stats", "SUM(gs.passing_2pt_conversions)"),
    "rushing_2pt_conversions": ("game_stats", "SUM(gs.rushing_2pt_conversions)"),
    "receiving_2pt_conversions": ("game_stats", "SUM(gs.receiving_2pt_conversions)"),
    "timeouts": ("game_stats", "SUM(gs.timeouts)"),
    "misc_yards": ("game_stats", "SUM(gs.misc_yards)"),
    "special_teams_tds": ("game_stats", "SUM(gs.special_teams_tds)"),
    "targets": ("game_stats", "SUM(gs.targets)"),
    "fumbles_total": ("game_stats", "SUM(gs.fumbles_total)"),
    "fumbles_lost": ("game_stats", "SUM(gs.fumbles_lost_total)"),
    # defense detail
    "def_tackles_solo": ("game_stats", "SUM(gs.def_tackles_solo)"),
    "def_tackles_assists": ("game_stats", "SUM(gs.def_tackle_assists)"),
    "def_tackles_for_loss": ("game_stats", "SUM(gs.def_tackles_for_loss)"),
    "def_tackles_for_loss_yards": ("game_stats", "SUM(gs.def_tackles_for_loss_yards)"),
    "def_fumbles_forced": ("game_stats", "SUM(gs.def_fumbles_forced)"),
    "def_sack_yards": ("game_stats", "SUM(gs.def_sack_yards)"),
    "def_qb_hits": ("game_stats", "SUM(gs.def_qb_hits)"),
    "def_interception_yards": ("game_stats", "SUM(gs.def_interception_yards)"),
    "def_pass_defended": ("game_stats", "SUM(gs.def_pass_defended)"),
    "def_tds": ("game_stats", "SUM(gs.def_tds)"),
    "def_safeties": ("game_stats", "SUM(gs.def_safeties)"),
    "def_2pt_conversions_allowed": ("game_stats", "SUM(gs.def_2pt_made)"),
    # kicking
    "fg_made": ("game_stats", "SUM(gs.fg_made)"),
    "fg_attempts": ("game_stats", "SUM(gs.fg_att)"),
    "fg_missed": ("game_stats", "SUM(gs.fg_missed)"),
    "fg_blocked": ("game_stats", "SUM(gs.fg_blocked)"),
    "fg_long": ("game_stats", "MAX(gs.fg_long)"),
    "fg_pct": ("game_stats", "ROUND(100.0 * SUM(gs.fg_made) / NULLIF(SUM(gs.fg_att),0), 1)"),
    "fg_made_50_59": ("game_stats", "SUM(gs.fg_made_50_59)"),
    "fg_made_60": ("game_stats", "SUM(gs.fg_made_60_)"),
    "game_winning_fg": ("game_stats", "SUM(gs.gwfg_made)"),
    "pat_made": ("game_stats", "SUM(gs.pat_made)"),
    "pat_attempts": ("game_stats", "SUM(gs.pat_att)"),
    "pat_pct": ("game_stats", "ROUND(100.0 * SUM(gs.pat_made) / NULLIF(SUM(gs.pat_att),0), 1)"),
    # punting
    "punts": ("game_stats", "SUM(gs.pt_att)"),
    "punt_yards": ("game_stats", "SUM(gs.pt_yards)"),
    "punt_long": ("game_stats", "MAX(gs.pt_long)"),
    "punts_inside_20": ("game_stats", "SUM(gs.pt_inside_20)"),
    "punt_touchbacks": ("game_stats", "SUM(gs.pt_touchback)"),
    "punt_fair_caught": ("game_stats", "SUM(gs.pt_fair_caught)"),
    "punt_net_yards": ("game_stats", "SUM(gs.pt_net_yards)"),
    "punt_avg": ("game_stats", "ROUND(1.0 * SUM(gs.pt_yards) / NULLIF(SUM(gs.pt_att),0), 1)"),
    "punt_net_avg": ("game_stats", "ROUND(1.0 * SUM(gs.pt_net_yards) / NULLIF(SUM(gs.pt_att),0), 1)"),
    # returns
    "kickoff_returns": ("game_stats", "SUM(gs.kickoff_returns)"),
    "kickoff_return_yards": ("game_stats", "SUM(gs.kickoff_return_yards)"),
    "punt_returns": ("game_stats", "SUM(gs.punt_returns)"),
    "punt_return_yards": ("game_stats", "SUM(gs.punt_return_yards)"),
    # time of possession (seconds summed; "avg_time_of_possession_min" presentable)
    "time_of_possession_secs": ("game_stats", "SUM(gs.time_of_possession_secs)"),
    "avg_time_of_possession_secs": ("game_stats", "AVG(gs.time_of_possession_secs)"),
    # --- nfl.team_rolling_stats windows (source rolling) ---
    "win_pct_5": ("rolling", "AVG(trs.win_pct_r5)"),
    "win_pct_3": ("rolling", "AVG(trs.win_pct_r3)"),
    "off_pts_per_game_5": ("rolling", "AVG(trs.off_pts_r5)"),
    "off_yds_per_game_5": ("rolling", "AVG(trs.off_yds_r5)"),
    "off_yds_per_game_10": ("rolling", "AVG(trs.off_yds_r10)"),
    "def_yds_per_game_5": ("rolling", "AVG(trs.def_yds_r5)"),
    "cover_pct_5": ("rolling", "AVG(trs.cover_pct_r5)"),
    "ou_over_pct_5": ("rolling", "AVG(trs.ou_over_pct_r5)"),
}

_TEAM_SRC_TABLE = {
    "games": "nfl.games g",
    "game_stats": "nfl.game_stats gs",
    "rolling": "nfl.team_rolling_stats trs",
}


def _validate_spec(args: dict) -> list[str] | None:
    """Validate a filter spec. Return list of errors, or None if OK."""
    errors = []
    if not isinstance(args, dict):
        return ["spec must be a dict"]
    # allowed keys
    allowed = {
        "stat", "stats", "group_by", "filters", "aggregate", "top", "order",
        "player_name", "position",
    }
    for k in args:
        if k not in allowed:
            errors.append(f"unknown spec key '{k}'")
    # aggregate
    agg = args.get("aggregate", "sum")
    if agg not in AGG_WHITELIST:
        errors.append(f"aggregate '{agg}' not allowed (use one of {sorted(AGG_WHITELIST)})")
    # stats
    stats = args.get("stats") or ([args.get("stat")] if args.get("stat") else None)
    if stats:
        if not isinstance(stats, list):
            errors.append("'stats' must be a list")
        else:
            for s in stats:
                if s not in NFL_COLUMNS:
                    errors.append(f"stat '{s}' not supported (see tool description for the allowed stat names)")
    else:
        errors.append("must specify at least one 'stat' (e.g. 'sacks', 'pass_yards')")
    # group_by
    gb = args.get("group_by")
    if gb is not None:
        if not isinstance(gb, list):
            errors.append("'group_by' must be a list")
        else:
            for g in gb:
                if g not in {"player", "week"}:
                    errors.append(f"group_by '{g}' not supported (use 'player' or 'week')")
    # filters
    filt = args.get("filters")
    if filt is not None:
        if not isinstance(filt, dict):
            errors.append("'filters' must be a dict")
        else:
            for k in filt:
                if k not in SUPPORTED_FILTERS:
                    errors.append(f"filter '{k}' not supported (use one of {sorted(SUPPORTED_FILTERS)})")
            if "home_or_away" in filt and filt["home_or_away"] not in ("home", "away"):
                errors.append("home_or_away must be 'home' or 'away'")
            if "game_type" in filt and filt["game_type"] not in ("REG", "PRE", "POST"):
                errors.append("game_type must be 'REG', 'PRE', or 'POST' (defaults to 'REG')")
    return errors or None


async def _run_query_player_stats(db: AsyncSession, args: dict) -> dict:
    """Execute an allowlisted player-stats query. Pure builder: model supplies names
    + values only; all SQL is constructed here against the allowlist."""
    errors = _validate_spec(args)
    if errors:
        return {"error": "Invalid query spec", "details": errors}

    stats = args.get("stats") or [args.get("stat")]
    agg = args.get("aggregate", "sum")
    gb = args.get("group_by") or []
    filt = args.get("filters") or {}
    # Defensive guard: a player reference inside 'filters' is silently IGNORED (single-player
    # must use the TOP-LEVEL 'player_name' arg). Fail loudly instead of returning inflated
    # whole-league data that looks correct.
    for misplaced in ("player", "player_name", "player_id"):
        if misplaced in filt:
            return {"error": "Invalid query spec", "details": [f"'{misplaced}' inside 'filters' is ignored; pass the player name via the TOP-LEVEL 'player_name' argument instead"]}
    order = (args.get("order") or "desc").lower()
    top = args.get("top")
    player_name = args.get("player_name")
    position = args.get("position")

    pid = None
    if player_name:
        player, warn = await _resolve_player_record(db, player_name)
        if not player:
            return {"error": warn}
        pid = player.id

    year = filt.get("season_year")
    if year is None:
        year = await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    conds, params = [], {"sid": sid}
    conds.append("pws.season_id = :sid")
    if pid:
        conds.append("pws.player_id = :pid"); params["pid"] = pid
    if position:
        conds.append("EXISTS (SELECT 1 FROM nfl.players p2 WHERE p2.id = pws.player_id AND UPPER(p2.position) = :position)")
        params["position"] = position.upper()
    week = filt.get("week")
    if week is not None:
        conds.append("pws.week = :week"); params["week"] = int(week)
    if filt.get("min_week") is not None:
        conds.append("pws.week >= :min_week"); params["min_week"] = int(filt["min_week"])
    if filt.get("max_week") is not None:
        conds.append("pws.week <= :max_week"); params["max_week"] = int(filt["max_week"])
    team = filt.get("team")
    if team:
        tid = await _resolve_team_id(db, team)
        if not tid:
            return {"error": f"Unknown team '{team}'"}
        conds.append("pws.team_id = :tid"); params["tid"] = tid
    opp = filt.get("opponent")
    if opp:
        oid = await _resolve_team_id(db, opp)
        if not oid:
            return {"error": f"Unknown opponent '{opp}'"}
        conds.append("pws.opponent_id = :oid"); params["oid"] = oid
    ha = filt.get("home_or_away")
    if ha:
        if ha == "home":
            conds.append("EXISTS (SELECT 1 FROM nfl.games g WHERE g.id = pws.game_id AND g.home_team_id = pws.team_id)")
        else:
            conds.append("EXISTS (SELECT 1 FROM nfl.games g WHERE g.id = pws.game_id AND g.away_team_id = pws.team_id)")
    # Default to REGULAR SEASON when game_type is omitted, so a plain stats query
    # never mixes regular + playoff games (playoff contamination). Explicit
    # game_type='POST' still selects postseason only.
    gt = filt.get("game_type") or "REG"
    conds.append("pws.game_type = :gt"); params["gt"] = gt

    col_sql = [NFL_COLUMNS[s]["sql"] for s in stats]
    sel_exprs = []
    for s, c in zip(stats, col_sql):
        if agg == "count":
            sel_exprs.append(f"COUNT(CASE WHEN NOT pws.{c} IS NULL THEN 1 END) AS {s}")
        elif agg == "avg":
            sel_exprs.append(f"AVG(pws.{c}) AS {s}")
        elif agg == "max":
            sel_exprs.append(f"MAX(pws.{c}) AS {s}")
        else:
            sel_exprs.append(f"COALESCE(SUM(pws.{c}),0) AS {s}")

    group_exprs, group_cols = [], []
    if "player" in gb:
        group_cols += ["p.name", "p.position"]
        group_exprs += ["p.name", "p.position"]
    if "week" in gb:
        group_cols += ["pws.week"]
        group_exprs += ["pws.week"]

    sql = f"""
        SELECT {', '.join(group_cols + sel_exprs)}
        FROM nfl.player_weekly_stats pws
        {'JOIN nfl.players p ON p.id = pws.player_id' if 'player' in gb else ''}
        WHERE {(' AND '.join(conds))}
    """
    if group_exprs:
        sql += " GROUP BY " + ", ".join(group_exprs)
    if group_cols:
        sql += f" ORDER BY {stats[0]} {order.upper()} NULLS LAST"
    sql, limit = apply_limit(sql, top)
    if sql is None:
        return {"error": limit}

    r = await db.execute(text(sql), params)
    rows = [dict(x) for x in r.mappings().all()]
    note = f"{len(rows)} row(s). Values are {agg} over the selected games." if len(rows) else f"No rows for season {year}"
    if group_cols:
        true_total = await async_count(db, sql, params)
        cut = count_note(limit, len(rows), true_total)
        if cut:
            note += f" [{cut}]"
    return {
        "result": rows,
        "aggregate": agg,
        "season": year,
        "stat_names": stats,
        "note": note,
    }


async def _run_query_team_stats(db: AsyncSession, args: dict) -> dict:
    """Allowlisted team/game query engine. stats from TEAM_STATS allowlist.
    If filters.team is set -> single-team result. Else -> league-wide aggregate,
    or grouped by team/opponent when group_by includes it.
    All sources validated; computed win/loss/margin derive from games scores."""
    from .nfl import _resolve_team_id

    errors = []
    stats = args.get("stats") or ([args.get("stat")] if args.get("stat") else None)
    if not stats:
        return {"error": "Invalid query spec", "details": ["must specify at least one 'stat'"]}
    if not isinstance(stats, list):
        return {"error": "Invalid query spec", "details": ["'stats' must be a list"]}
    for s in stats:
        if s not in TEAM_STATS:
            errors.append(f"stat '{s}' not supported")
    filt = args.get("filters") or {}
    gb = args.get("group_by")
    if gb is not None and not isinstance(gb, list):
        errors.append("'group_by' must be a list")
    if errors:
        return {"error": "Invalid query spec", "details": errors}

    team_name = filt.get("team")
    tid = None
    if team_name:
        tid = await _resolve_team_id(db, team_name)
        if not tid:
            return {"error": f"Unknown team '{team_name}'"}

    # source must be consistent across all requested stats
    sources = {TEAM_STATS[s][0] for s in stats}
    if len(sources) > 1:
        return {"error": "Mixing stats from different tables isn't allowed in one query",
                "details": [f"Chose: {sorted(sources)}"]}
    src = sources.pop()
    exprs = [TEAM_STATS[s][1] for s in stats]
    sel = ", ".join(f"{e} AS {s}" for s, e in zip(stats, exprs))

    params = {}
    conds = []
    gb_cols, gb_exprs = [], []

    if src == "games":
        if tid:
            base = GAMES_BASE
            params["tid"] = tid
        else:
            base = "FROM nfl.games g WHERE 1=1"
        year = filt.get("season_year")
        if year:
            sid = await _resolve_season_id(db, year)
            conds.append("g.season_id = :sid"); params["sid"] = sid
        if filt.get("min_week") is not None:
            conds.append("g.week >= :min_week"); params["min_week"] = int(filt["min_week"])
        if filt.get("max_week") is not None:
            conds.append("g.week <= :max_week"); params["max_week"] = int(filt["max_week"])
        if filt.get("home_or_away") in ("home", "away") and tid:
            no = "home_team_id" if filt["home_or_away"] == "home" else "away_team_id"
            conds.append(f"g.{no} = :tid")
        if filt.get("opponent"):
            oid = await _resolve_team_id(db, filt["opponent"])
            if not oid:
                return {"error": f"Unknown opponent '{filt['opponent']}'"}
            # opponent filter: the game must pair tid vs oid
            if tid:
                conds.append("((g.home_team_id = :tid AND g.away_team_id = :opp) OR (g.home_team_id = :opp AND g.away_team_id = :tid))")
                params["opp"] = oid
            else:
                conds.append("(g.home_team_id = :opp OR g.away_team_id = :opp)")
                params["opp"] = oid
    elif src == "game_stats":
        if tid:
            t = await db.execute(text("SELECT abbreviation FROM nfl.teams WHERE id=:id"), {"id": tid})
            tr = t.first()
            if not tr:
                return {"error": f"Unknown team '{team_name}'"}
            params["abbr"] = tr[0]
        base = "FROM nfl.game_stats gs WHERE 1=1"
        if params.get("abbr"):
            conds.append("gs.team_abbr = :abbr")
        if filt.get("season_year"):
            conds.append("gs.season = :syear"); params["syear"] = int(filt["season_year"])
        if filt.get("opponent"):
            oid = await _resolve_team_id(db, filt["opponent"])
            if not oid:
                return {"error": f"Unknown opponent '{filt['opponent']}'"}
            t2 = await db.execute(text("SELECT abbreviation FROM nfl.teams WHERE id=:id"), {"id": oid})
            opp_abbr = t2.first()[0]
            conds.append("gs.opponent_abbr = :oabbr"); params["oabbr"] = opp_abbr
    else:  # rolling
        if tid:
            t = await db.execute(text("SELECT abbreviation FROM nfl.teams WHERE id=:id"), {"id": tid})
            tr = t.first()
            if not tr:
                return {"error": f"Unknown team '{team_name}'"}
            params["abbr"] = tr[0]
        base = "FROM nfl.team_rolling_stats trs WHERE 1=1"
        if params.get("abbr"):
            conds.append("trs.team_abbr = :abbr")
        if filt.get("season_year"):
            conds.append("trs.season = :syear"); params["syear"] = int(filt["season_year"])

    if gb and ("team" in gb or "opponent" in gb):
        if not tid:
            if "team" in gb and src == "game_stats":
                gb_cols += ["gs.team_abbr AS team"]; gb_exprs += ["gs.team_abbr"]
            elif "team" in gb and src == "rolling":
                gb_cols += ["trs.team_abbr AS team"]; gb_exprs += ["trs.team_abbr"]
            elif "team" in gb and src == "games":
                return {"error": "group_by team requires a team filter for games-source stats"}
            if "opponent" in gb and src == "game_stats":
                gb_cols += ["gs.opponent_abbr AS opponent"]; gb_exprs += ["gs.opponent_abbr"]

    where_sql = (" AND ".join(conds)) if conds else "1=1"
    if gb_exprs:
        sql = f"SELECT {', '.join(gb_cols + sel.split(', '))} {base} AND {where_sql} GROUP BY {', '.join(gb_exprs)}"
    else:
        sql = f"SELECT {sel} {base} AND {where_sql}"

    # ordering + top for leaderboards
    order = (args.get("order") or "desc").lower()
    top = args.get("top")
    if gb_exprs:
        sql += f" ORDER BY {stats[0]} {order.upper()} NULLS LAST"
    sql, limit = apply_limit(sql, top)
    if sql is None:
        return {"error": limit}

    r = await db.execute(text(sql), params)
    rows = [dict(x) for x in r.mappings().all()]
    out = {
        "result": rows,
        "aggregate": args.get("aggregate", "sum"),
        "season": filt.get("season_year"),
        "source": src,
        "stat_names": stats,
    }
    if gb_exprs:
        true_total = await async_count(db, sql, params)
        cut = count_note(limit, len(rows), true_total)
        if cut:
            out["note"] = cut
    return out


    stats = args.get("stats") or [args.get("stat")]
    agg = args.get("aggregate", "sum")
    gb = args.get("group_by") or []
    filt = args.get("filters") or {}
    order = (args.get("order") or "desc").lower()
    top = args.get("top")
    player_name = args.get("player_name")
    position = args.get("position")

    # --- resolve player (supports whole-roster queries WITHOUT player_name) ---
    pid = None
    if player_name:
        player, warn = await _resolve_player_record(db, player_name)
        if not player:
            return {"error": warn}
        pid = player.id

    # --- resolve season ---
    year = filt.get("season_year")
    if year is None:
        year = await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    # --- build WHERE (parameterized; allowlisted predicates) ---
    conds, params = [], {"sid": sid}
    conds.append("pws.season_id = :sid")

    if pid:
        conds.append("pws.player_id = :pid")
        params["pid"] = pid

    if position:
        conds.append("EXISTS (SELECT 1 FROM nfl.players p2 WHERE p2.id = pws.player_id AND UPPER(p2.position) = :position)")
        params["position"] = position.upper()

    week = filt.get("week")
    if week is not None:
        conds.append("pws.week = :week"); params["week"] = int(week)
    if filt.get("min_week") is not None:
        conds.append("pws.week >= :min_week"); params["min_week"] = int(filt["min_week"])
    if filt.get("max_week") is not None:
        conds.append("pws.week <= :max_week"); params["max_week"] = int(filt["max_week"])

    team = filt.get("team")
    if team:
        tid = await _resolve_team_id(db, team)
        if not tid:
            return {"error": f"Unknown team '{team}'"}
        conds.append("pws.team_id = :tid"); params["tid"] = tid

    opp = filt.get("opponent")
    if opp:
        oid = await _resolve_team_id(db, opp)
        if not oid:
            return {"error": f"Unknown opponent '{opp}'"}
        conds.append("pws.opponent_id = :oid"); params["oid"] = oid

    ha = filt.get("home_or_away")
    if ha:
        if ha == "home":
            conds.append("EXISTS (SELECT 1 FROM nfl.games g WHERE g.id = pws.game_id AND g.home_team_id = pws.team_id)")
        else:
            conds.append("EXISTS (SELECT 1 FROM nfl.games g WHERE g.id = pws.game_id AND g.away_team_id = pws.team_id)")

    # Default to REGULAR SEASON when game_type is omitted (playoff-contamination safety).
    gt = filt.get("game_type") or "REG"
    conds.append("pws.game_type = :gt"); params["gt"] = gt

    # --- targets: aggregated stat expressions from allowlist ---
    col_sql = [NFL_COLUMNS[s]["sql"] for s in stats]
    sel_exprs = []
    for s, c in zip(stats, col_sql):
        if agg == "count":
            sel_exprs.append(f"COUNT(CASE WHEN NOT pws.{c} IS NULL THEN 1 END) AS {s}")
        elif agg == "avg":
            sel_exprs.append(f"AVG(pws.{c}) AS {s}")
        elif agg == "max":
            sel_exprs.append(f"MAX(pws.{c}) AS {s}")
        else:
            sel_exprs.append(f"COALESCE(SUM(pws.{c}),0) AS {s}")

    # --- group by ---
    group_exprs, group_cols = [], []
    if "player" in gb:
        group_cols += ["p.name", "p.position"]
        group_exprs += ["p.name", "p.position"]
    if "week" in gb:
        group_cols += ["pws.week"]
        group_exprs += ["pws.week"]

    sql = f"""
        SELECT {', '.join(group_cols + sel_exprs)}
        FROM nfl.player_weekly_stats pws
        {'JOIN nfl.players p ON p.id = pws.player_id' if 'player' in gb else ''}
        WHERE {(' AND '.join(conds))}
    """
    if group_exprs:
        sql += " GROUP BY " + ", ".join(group_exprs)

    order_sql = stats[0]
    # QUALIFY not supported on PG; use ORDER BY + LIMIT
    if group_cols:
        sql += f" ORDER BY {order_sql} {order.upper()} NULLS LAST"
    sql, limit = apply_limit(sql, top)
    if sql is None:
        return {"error": limit}

    r = await db.execute(text(sql), params)
    rows = [dict(x) for x in r.mappings().all()]
    if not rows:
        return {"result": [], "note": f"No rows for season {year}", "aggregate": agg}

    # every row needs a stable key
    out = []
    for row in rows:
        out.append(row)
    note = f"{len(rows)} row(s). Values are {agg} over the selected games."
    if group_cols:
        true_total = await async_count(db, sql, params)
        cut = count_note(limit, len(rows), true_total)
        if cut:
            note += f" [{cut}]"
    return {
        "result": out,
        "aggregate": agg,
        "season": year,
        "stat_names": stats,
        "note": note,
    }
