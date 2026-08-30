"""
NBA query engines — allowlisted player/team stat queries, mirroring NFL's nfl_query.py.

The model supplies a TYPED spec; the engine validates against strict allowlists and
builds parameterized SQL. NEVER raw model SQL. Season semantics: NBA season_year =
START year (2022 = 2022-23), seasons span calendar years, so games scoped via
season_id not EXTRACT(YEAR).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ._query_guard import apply_limit, count_note, async_count

from .nba import (
    _resolve_player_split,
    _resolve_team_id,
    _resolve_season_year,
    _resolve_season_id,
)

AGG_WHITELIST = {"sum", "avg", "max", "count"}

# NBA player per-game stat columns (from player_game_stats).
NBA_PLAYER_STATS = {
    "points": "points", "minutes": "minutes",
    "field_goals_made": "field_goals_made", "field_goals_attempted": "field_goals_attempted",
    "three_pointers_made": "three_pointers_made", "three_pointers_attempted": "three_pointers_attempted",
    "free_throws_made": "free_throws_made", "free_throws_attempted": "free_throws_attempted",
    "rebounds_offensive": "rebounds_offensive", "rebounds_defensive": "rebounds_defensive",
    "rebounds_total": "rebounds_total", "assists": "assists", "steals": "steals",
    "blocks": "blocks", "turnovers": "turnovers", "fouls_personal": "fouls_personal",
    "plus_minus": "plus_minus", "starts": "is_starter",
}
# pct columns: AVG, never SUM
NBA_PLAYER_PCT = {
    "field_goal_pct": "field_goal_pct", "three_pointer_pct": "three_pointer_pct",
    "free_throw_pct": "free_throw_pct",
}
NBA_PLAYER_COLS = {**NBA_PLAYER_STATS, **NBA_PLAYER_PCT}
NBA_FILTERS = {"season_year", "week", "min_week", "max_week", "team", "opponent", "home_or_away", "game_type"}

_GAME_JOIN = "JOIN nba.games g ON g.id = pgs.game_id"


def _validate_nba_player_spec(args) -> list[str] | None:
    allowed = {"stats", "stat", "group_by", "filters", "aggregate", "top", "order", "player_name", "position", "team"}
    errors = []
    for k in args:
        if k not in allowed:
            errors.append(f"unknown spec key '{k}'")
    stats = args.get("stats") or ([args.get("stat")] if args.get("stat") else None)
    if not stats:
        errors.append("must specify at least one 'stat'")
    elif isinstance(stats, list):
        for s in stats:
            if s not in NBA_PLAYER_COLS:
                errors.append(f"stat '{s}' not supported")
    else:
        errors.append("'stats' must be a list")
    agg = args.get("aggregate", "sum")
    if agg not in AGG_WHITELIST:
        errors.append(f"aggregate '{agg}' not allowed")
    gb = args.get("group_by")
    if gb is not None and not isinstance(gb, list):
        errors.append("'group_by' must be a list")
    filt = args.get("filters")
    if filt is not None:
        if not isinstance(filt, dict):
            errors.append("'filters' must be a dict")
        else:
            for k in filt:
                if k not in NBA_FILTERS:
                    errors.append(f"filter '{k}' not supported")
    return errors or None


async def _run_query_player_stats(db: AsyncSession, args: dict) -> dict:
    errs = _validate_nba_player_spec(args)
    if errs:
        return {"error": "Invalid query spec", "details": errs}
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

    pid = None
    if player_name:
        try:
            resolved = await _resolve_player_split(db, player_name)
        except ValueError as e:
            return {"error": str(e)}
        pid = resolved["id"]

    year = filt.get("season_year") or await _resolve_season_year(db)
    sid = await _resolve_season_id(db, year)

    conds, params = ["g.season_id = :sid"], {"sid": sid}
    if pid:
        conds.append("pgs.player_id = :pid"); params["pid"] = pid
    team = filt.get("team")
    if team:
        tid = await _resolve_team_id(db, team)
        if not tid:
            return {"error": f"Unknown team '{team}'"}
        conds.append("pgs.team_id = :tid"); params["tid"] = tid
    ha = filt.get("home_or_away")
    if ha in ("home", "away"):
        col = "home_team_id" if ha == "home" else "away_team_id"
        conds.append(f"g.{col} = pgs.team_id")
    opp = filt.get("opponent")
    if opp:
        oid = await _resolve_team_id(db, opp)
        if not oid:
            return {"error": f"Unknown opponent '{opp}'"}
        params["oid"] = oid
        conds.append("(g.home_team_id = pgs.team_id AND g.away_team_id = :oid) OR (g.away_team_id = pgs.team_id AND g.home_team_id = :oid)")
    gt = filt.get("game_type")
    # DEFAULT to regular-season only: nba.player_game_stats holds POST + PLAYIN rows,
    # and NBA users asking for 'season stats' mean the 82-game regular season. An
    # explicit game_type filter ('POST'/'PLAYIN'/'PRE') overrides this.
    if not gt:
        gt = "REG"
    conds.append("g.game_type = :gt"); params["gt"] = gt

    # stats: sum counts, avg pcts
    sel = []
    for s in stats:
        c = NBA_PLAYER_COLS[s]
        if s in NBA_PLAYER_PCT:
            sel.append(f"AVG(pgs.{c}) AS \"{s}\"")
        elif agg == "count":
            sel.append(f"COUNT(CASE WHEN NOT pgs.{c} IS NULL THEN 1 END) AS \"{s}\"")
        elif agg == "avg":
            sel.append(f"AVG(pgs.{c}) AS \"{s}\"")
        elif agg == "max":
            sel.append(f"MAX(pgs.{c}) AS \"{s}\"")
        else:
            sel.append(f"COALESCE(SUM(pgs.{c}),0) AS \"{s}\"")

    group_cols, group_exprs = [], []
    if "player" in gb:
        group_cols += ["p.name", "p.position"]; group_exprs += ["p.name", "p.position"]
    if "week" in gb:
        group_cols += ["pgs.game_date"]; group_exprs += ["pgs.game_date"]

    sql = f"SELECT {', '.join(group_cols + sel)} FROM nba.player_game_stats pgs {_GAME_JOIN} {('JOIN nba.players p ON p.id = pgs.player_id' if 'player' in gb else '')} WHERE {' AND '.join(conds)}"
    if group_exprs:
        sql += " GROUP BY " + ", ".join(group_exprs)
        sql += f" ORDER BY \"{stats[0]}\" {order.upper()} NULLS LAST"
    sql, limit = apply_limit(sql, top)
    if sql is None:
        return {"error": limit}
    r = await db.execute(text(sql), params)
    rows = [dict(x) for x in r.mappings().all()]
    out = {"result": rows, "aggregate": agg, "season": year, "stat_names": stats}
    if group_exprs:
        true_total = await async_count(db, sql, params)
        cut = count_note(limit, len(rows), true_total)
        if cut:
            out["note"] = cut
    if not rows:
        out["note"] = "No rows"
    return out


# --- TEAM engine: nba.games (home/away team stats baked in) + team_rolling_stats ---
NBA_TEAM_FILTERS = {"season_year", "min_week", "max_week", "team", "opponent", "home_or_away"}
# team stat name -> (source, sql). :tcol resolves to home_/away_ prefix at build time.
NBA_TEAM_STATS_SOURCES = {
    # computed records from games
    "wins": "games", "losses": "games", "ties": "games",
    "points_for": "games", "points_against": "games", "point_margin": "games", "win_pct": "games",
    # home/away-baked team perf from nba.games
    "field_goals_made": "games", "field_goals_attempted": "games", "three_pointers_made": "games",
    "three_pointers_attempted": "games", "free_throws_made": "games", "free_throws_attempted": "games",
    "rebounds": "games", "assists": "games", "steals": "games", "blocks": "games",
    "turnovers": "games", "fouls": "games", "offensive_rebounds": "games", "defensive_rebounds": "games",
    "points_in_paint": "games",
    # rolling windows
    "win_pct_3": "rolling", "win_pct_5": "rolling", "off_pts_5": "rolling", "def_pts_5": "rolling",
    "cover_pct_5": "rolling", "ou_over_pct_5": "rolling",
}


async def _run_query_team_stats(db: AsyncSession, args: dict) -> dict:
    from .nba import _resolve_team_id as _rtid
    stats = args.get("stats") or ([args.get("stat")] if args.get("stat") else None)
    if not isinstance(stats, list) or not stats:
        return {"error": "Invalid query spec", "details": ["must specify 'stats' list"]}
    filt = args.get("filters") or {}
    for s in stats:
        if s not in NBA_TEAM_STATS_SOURCES:
            return {"error": f"stat '{s}' not supported"}
    sources = {NBA_TEAM_STATS_SOURCES[s] for s in stats}
    if len(sources) > 1:
        return {"error": "Mixing stats from different tables isn't allowed", "details": [f"sources {sorted(sources)}"]}
    src = sources.pop()
    team = filt.get("team")
    tid = await _rtid(db, team) if team else None
    if team and not tid:
        return {"error": f"Unknown team '{team}'"}
    params = {}
    conds = []
    gb = args.get("group_by") or []
    gb_cols, gb_exprs = [], []

    if src == "games":
        base = "FROM nba.games g WHERE 1=1"
        # DEFAULT to regular-season only (consistent with player path). nba.games holds
        # PRE/POST/PLAYIN rows; an explicit game_type filter ('POST'/'PLAYIN'/'PRE')
        # overrides. 'team season record/points' conventionally means the 82-game REG.
        gt = filt.get("game_type") or "REG"
        params["gt"] = gt
        conds.append("g.game_type = :gt")
        if tid:
            params["tid"] = tid
            # scope to team's games (either home or away)
            conds.append("(g.home_team_id = :tid OR g.away_team_id = :tid)")
        if filt.get("season_year"):
            sid = await _resolve_season_id(db, int(filt["season_year"]))
            conds.append("g.season_id = :sid"); params["sid"] = sid
        opp = filt.get("opponent")
        if opp:
            oid = await _rtid(db, opp)
            if not oid:
                return {"error": f"Unknown opponent '{opp}'"}
            params["opp"] = oid
            if tid:
                conds.append("((g.home_team_id = :tid AND g.away_team_id = :opp) OR (g.home_team_id = :opp AND g.away_team_id = :tid))")
            else:
                conds.append("(g.home_team_id = :opp OR g.away_team_id = :opp)")
        # build stat exprs with home/away prefix resolved
        # if tid + home_or_away, restrict side
        side = None
        if filt.get("home_or_away") in ("home", "away") and tid:
            col = "home_team_id" if filt["home_or_away"] == "home" else "away_team_id"
            conds.append(f"g.{col} = :tid")
            side = filt["home_or_away"]
        # if no tid but home_or_away -> restrict to that side league-wide
        elif filt.get("home_or_away") and not tid:
            col = "home_team_id" if filt["home_or_away"] == "home" else "away_team_id"
            conds.append(f"g.{col} IS NOT NULL")
            side = filt["home_or_away"]
        pre = "home_" if side == "home" else ("away_" if side == "away" else None)

        def _games_expr(name):
            if name in ("wins", "losses", "win_pct"):
                # compute wins/losses for the side (or either)
                # with tid+optional side: count that team's wins
                if tid:
                    if side == "home":
                        if name == "wins": return "SUM(CASE WHEN g.home_score > g.away_score THEN 1 ELSE 0 END)"
                        if name == "losses": return "SUM(CASE WHEN g.home_score < g.away_score THEN 1 ELSE 0 END)"
                        if name == "win_pct": return "ROUND(100.0 * SUM(CASE WHEN g.home_score > g.away_score THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1)"
                    elif side == "away":
                        if name == "wins": return "SUM(CASE WHEN g.away_score > g.home_score THEN 1 ELSE 0 END)"
                        if name == "losses": return "SUM(CASE WHEN g.away_score < g.home_score THEN 1 ELSE 0 END)"
                        if name == "win_pct": return "ROUND(100.0 * SUM(CASE WHEN g.away_score > g.home_score THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1)"
                    else:
                        # any side
                        if name == "wins": return "SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score > g.away_score) OR (g.away_team_id = :tid AND g.away_score > g.home_score) THEN 1 ELSE 0 END)"
                        if name == "losses": return "SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score < g.away_score) OR (g.away_team_id = :tid AND g.away_score < g.home_score) THEN 1 ELSE 0 END)"
                        if name == "win_pct": return "ROUND(100.0 * SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score > g.away_score) OR (g.away_team_id = :tid AND g.away_score > g.home_score) THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1)"
                else:
                    # league-wide - not meaningful for wins without a team; return count-based dummy
                    return "COUNT(*)"
            if name in ("points_for", "points_against", "point_margin"):
                if tid is None:
                    return f"SUM({pre or ''}score)" if name == "points_for" else ("COUNT(*)" if name == "points_against" else "0")
                # team points: sum the team's score
                if name == "points_for":
                    return "SUM(CASE WHEN g.home_team_id = :tid THEN g.home_score ELSE g.away_score END)"
                if name == "points_against":
                    return "SUM(CASE WHEN g.home_team_id = :tid THEN g.away_score ELSE g.home_score END)"
                if name == "point_margin":
                    return "SUM(CASE WHEN g.home_team_id = :tid THEN g.home_score - g.away_score ELSE g.away_score - g.home_score END)"
            # perf stat: choose home/away prefix column by side; if mixed side (no tid no side), sum both sides
            base_col = {
                "field_goals_made": "field_goals_made", "field_goals_attempted": "field_goals_attempted",
                "three_pointers_made": "three_points_made", "three_pointers_attempted": "three_points_attempted",
                "free_throws_made": "free_throws_made", "free_throws_attempted": "free_throws_attempted",
                "rebounds": "rebounds", "assists": "assists", "steals": "steals", "blocks": "blocks",
                "turnovers": "turnovers", "fouls": "fouls",
                "offensive_rebounds": "offensive_rebounds", "defensive_rebounds": "defensive_rebounds",
                "points_in_paint": "points_in_paint",
            }[name]
            # if team-scoped with a home/away preference OR mixed, we must average both home+away
            # Simplification: when tid, combine home+away columns weighted by side occurs only if side set;
            # otherwise we can't attribute easily -> use side when set, else sum both columns /2-ish not accurate.
            # Correct approach: for a single team, its per-game perf = home_col when it was home, away_col when away.
            if tid:
                return (f"SUM(CASE WHEN g.home_team_id = :tid THEN g.home_{base_col} ELSE g.away_{base_col} END)")
            # league-wide: sum both home and away columns
            return f"SUM(g.home_{base_col} + g.away_{base_col})"

        sel = ", ".join(f"{_games_expr(s)} AS \"{s}\"" for s in stats)
        sql = f"SELECT {sel} {base} AND {' AND '.join(conds) if conds else '1=1'}"
    else:  # rolling
        if tid:
            t = await db.execute(text("SELECT abbreviation FROM nba.teams WHERE id=:id"), {"id": tid})
            tr = t.first()
            if not tr:
                return {"error": f"Unknown team '{team}'"}
            params["abbr"] = tr[0]
        base = "FROM nba.team_rolling_stats trs WHERE 1=1"
        if params.get("abbr"):
            conds.append("trs.team_abbr = :abbr")
        if filt.get("season_year"):
            conds.append("trs.season = :syear"); params["syear"] = int(filt["season_year"])
        roll_map = {"win_pct_3": "win_pct_r3", "win_pct_5": "win_pct_r5", "off_pts_5": "off_pts_r5",
                    "def_pts_5": "def_pts_r5", "cover_pct_5": "cover_pct_r5", "ou_over_pct_5": "ou_over_pct_r5"}
        sel = ", ".join(f"AVG(trs.{roll_map[s]}) AS \"{s}\"" for s in stats)
        sql = f"SELECT {sel} {base} AND {' AND '.join(conds) if conds else '1=1'}"

    if gb and "team" in gb and (src == "games" or src == "rolling"):
        return {"error": "group_by team requires team-scoped detail; use filters.team for a single team"}

    r = await db.execute(text(sql), params)
    rows = [dict(x) for x in r.mappings().all()]
    return {"result": rows, "season": filt.get("season_year"), "source": src, "stat_names": stats}
