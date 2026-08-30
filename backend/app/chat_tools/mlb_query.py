"""
MLB query engines — allowlisted player (batting + pitching) and team stat queries,
mirroring NFL/NBA. Source auto-detected per stat. NEVER raw model SQL.
"""
from __future__ import annotations

import math

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .mlb import _resolve_team, _resolve_hitter, _resolve_season, MLBSeason
from ._query_guard import apply_limit, count_note, async_count

AGG_WHITELIST = {"sum", "avg", "max", "count"}

# batting counts (SUM) + rate stats (AVG)
MLB_BAT_COUNTS = {
    "plate_appearances": "plate_appearances", "at_bats": "at_bats", "runs": "runs",
    "hits": "hits", "doubles": "doubles", "triples": "triples", "home_runs": "home_runs",
    "runs_batted_in": "runs_batted_in", "base_on_balls": "base_on_balls",
    "intentional_walks": "intentional_walks", "strikeouts": "strikeouts",
    "stolen_bases": "stolen_bases", "caught_stealing": "caught_stealing",
    "hit_by_pitch": "hit_by_pitch", "sacrifice_flies": "sacrifice_flies",
    "total_bases": "total_bases", "left_on_base": "left_on_base",
    "ground_into_double_play": "ground_into_double_play", "pickoffs": "pickoffs",
}
MLB_BAT_RATE = {"avg": "avg", "obp": "obp", "slg": "slg", "ops": "ops"}
MLB_BAT_COLS = {**MLB_BAT_COUNTS, **MLB_BAT_RATE}

# pitching — SEASON aggregate via mlb.pitching_stats (counts AND accurate rates).
# Names shared with batting (strikeouts, hits, home_runs, base_on_balls, hit_by_pitch)
# are treated as neutral by _detect_source and resolved by the non-neutral stats.
MLB_PIT_STATS = {
    # counts
    "wins": "wins", "losses": "losses", "saves": "saves", "holds": "holds",
    "blown_saves": "blown_saves", "games_played": "games_played", "games_started": "games_started",
    "shutouts": "shutouts", "complete_games": "complete_games",
    "innings_pitched": "innings_pitched", "earned_runs": "earned_runs", "outs": "outs",
    "runs_allowed": "runs", "hits_allowed": "hits", "home_runs_allowed": "home_runs",
    "walks_allowed": "base_on_balls", "intentional_walks": "intentional_walks",
    "strikeouts": "strikeouts", "batters_faced": "batters_faced", "hit_by_pitch": "hit_by_pitch",
    "pitches_thrown": "pitches_thrown", "strikes": "strikes", "wild_pitches": "wild_pitches",
    "balks": "balks", "pickoffs": "pickoffs", "ground_into_double_play": "ground_into_double_play",
    # rates
    "era": "era", "whip": "whip", "avg_against": "avg", "obp_against": "obp",
    "slg_against": "slg", "ops_against": "ops", "hits_per_9": "hits_per_9",
    "home_runs_per_9": "home_runs_per_9", "strikeouts_per_9": "strikeouts_per_9",
    "walks_per_9": "walks_per_9", "strikeout_walk_ratio": "strikeout_walk_ratio",
    "win_percentage": "win_percentage", "strike_percentage": "strike_percentage",
    "pitches_per_inning": "pitches_per_inning",
}
MLB_PIT_RATE = {k: v for k, v in MLB_PIT_STATS.items()
                 if k in ("era", "whip", "avg_against", "obp_against", "slg_against", "ops_against",
                          "hits_per_9", "home_runs_per_9", "strikeouts_per_9", "walks_per_9",
                          "strikeout_walk_ratio", "win_percentage", "strike_percentage", "pitches_per_inning")}
MLB_PIT_COUNTS = {k: v for k, v in MLB_PIT_STATS.items() if k not in MLB_PIT_RATE}
MLB_PIT_COLS = MLB_PIT_STATS

MLB_FILTERS = {"season_year", "month", "home_or_away", "team", "opponent"}


def _detect_source(stats):
    """Return 'batting'|'pitching'|None. Treat names present in BOTH allowlists as
    neutral (e.g. 'strikeouts' exists in both batting and pitching); the source is
    pinned by any non-neutral stat. All-neutral -> batting default."""
    from_bat = {s for s in stats if s in MLB_BAT_COLS and not (s in MLB_BAT_COLS and s in MLB_PIT_COLS)}
    from_pit = {s for s in stats if s in MLB_PIT_COLS and not (s in MLB_BAT_COLS and s in MLB_PIT_COLS)}
    both = {s for s in stats if s in MLB_BAT_COLS and s in MLB_PIT_COLS}
    unknown = [s for s in stats if s not in MLB_BAT_COLS and s not in MLB_PIT_COLS]
    if unknown:
        return None, [f"stat '{u}' not supported" for u in unknown]
    # neutral names are usable in either source
    if from_bat and from_pit:
        return None, ["can't mix batting and pitching stats in one query"]
    if from_bat:
        return "batting", None
    if from_pit:
        return "pitching", None
    # all neutral -> default batting (rate/count names like 'strikeouts' or 'hits')
    return "batting", None


async def _run_query_player_stats(db: AsyncSession, args: dict) -> dict:
    allowed = {"stats", "stat", "aggregate", "group_by", "filters", "top", "order", "player_name"}
    for k in args:
        if k not in allowed:
            return {"error": "Invalid query spec", "details": [f"unknown spec key '{k}'"]}
    stats = args.get("stats") or ([args.get("stat")] if args.get("stat") else None)
    if not isinstance(stats, list) or not stats:
        return {"error": "Invalid query spec", "details": ["must specify 'stats' list"]}
    agg = args.get("aggregate", "sum")
    if agg not in AGG_WHITELIST:
        return {"error": f"aggregate '{agg}' not allowed"}
    filt = args.get("filters") or {}
    # Defensive guard: a player reference inside 'filters' is silently IGNORED by this
    # engine (single-player must use the TOP-LEVEL 'player_name' arg). Fail loudly
    # instead of returning inflated whole-league data that looks correct.
    for misplaced in ("player", "player_name", "player_id"):
        if misplaced in filt:
            return {"error": "Invalid query spec", "details": [f"'{misplaced}' inside 'filters' is ignored; pass the player name via the TOP-LEVEL 'player_name' argument instead"]}
    src, err = _detect_source(stats)
    if err:
        return {"error": "Invalid query spec", "details": err}
    gb = args.get("group_by") or []
    order = (args.get("order") or "desc").lower()
    top = args.get("top")

    season, _ = await _resolve_season(db, {"season": filt.get("season_year")})
    sid = season.id if season else None

    params = {}
    conds = []

    if src == "batting":
        tbl = "mlb.batting_game_stats bgs"
        join_g = _g_join("bgs")
        player_col = "pgs2.player_id"
        if sid:
            conds.append("g.season_id = :sid"); params["sid"] = sid
        # resolves hitter -> player id
        if args.get("player_name"):
            hitter = await _resolve_hitter(db, args["player_name"])
            if not hitter:
                return {"error": f"Unknown player '{args['player_name']}'"}
            conds.append("bgs.player_id = :pid"); params["pid"] = hitter.id
        team_side = None
        if filt.get("home_or_away") in ("home", "away"):
            # batting_game_stats.team_side stores the full words 'home'/'away' and
            # means the player's own team's side (i.e. 'home' == player's team played at home)
            team_side = filt["home_or_away"]
        if team_side:
            conds.append("bgs.team_side = :side"); params["side"] = team_side
        if filt.get("home_or_away") not in ("home", "away") and filt.get("home_or_away"):
            return {"error": "Invalid query spec", "details": ["home_or_away must be 'home' or 'away'"]}
        if filt.get("team"):
            # batting table has no team_id (only team_side H/A). A team's batters appear as
            # both H and A across games, so a raw 'team' filter is not meaningful here. Return
            # a clear error instead of guessing.
            return {"error": "Invalid query spec", "details": ["'team' filter isn't supported for batting stats (no team_id on batting rows); use home_or_away or query_team_stats instead"]}
        counts, rates = {}, {}
        for s in stats:
            if s in MLB_BAT_RATE:
                rates[s] = MLB_BAT_RATE[s]
            else:
                counts[s] = MLB_BAT_COUNTS[s]
        sel = _agg_select(counts, "bgs", rates=rates, agg=agg)

    else:  # pitching — read mlb.pitching_stats (SEASON aggregate: counts + accurate rates)
        # One row per pitcher-season, so no SUM/AVG across games — columns are season totals/rates.
        tbl = "mlb.pitching_stats pss"
        join_g = "JOIN mlb.seasons pseason ON pseason.id = pss.season_id"
        if sid:
            conds.append("pss.season_id = :sid"); params["sid"] = sid
        if args.get("player_name"):
            hitter = await _resolve_hitter(db, args["player_name"])
            if not hitter:
                return {"error": f"Unknown player '{args['player_name']}'"}
            conds.append("pss.player_id = :pid"); params["pid"] = hitter.id
        if filt.get("team"):
            team = await _resolve_team(db, filt["team"])
            if not team:
                return {"error": f"Unknown team '{filt['team']}'"}
            conds.append("pss.team_id = :tid"); params["tid"] = team.id
        if filt.get("home_or_away"):
            return {"error": "home_or_away isn't supported for season-level pitching stats"}
        if filt.get("opponent"):
            return {"error": "opponent isn't supported for season-level pitching stats (per-game splits aren't available)"}
        # season rows are per (player, season, team). On a per-player leaderboard, a pitcher
        # who played for 2 teams in one season yields 2 rows; aggregate to one line: rates MIN
        # (best team-line), counts SUM. Without group_by, return the raw season rows.
        min_ip = filt.get("min_innings")
        if min_ip is not None:
            conds.append("pss.innings_pitched >= :minip"); params["minip"] = float(min_ip)
        if "player" in gb:
            parts = []
            for s in stats:
                col = MLB_PIT_STATS[s]
                if s in MLB_PIT_RATE:
                    parts.append(f"MIN(pss.{col}) AS \"{s}\"")
                else:
                    parts.append(f"COALESCE(SUM(pss.{col}),0) AS \"{s}\"")
            sel = ", ".join(parts)
        else:
            sel = ", ".join(f"pss.{MLB_PIT_STATS[s]} AS \"{s}\"" for s in stats)

    # group by player
    group_cols, group_exprs = [], []
    if "player" in gb:
        if src == "batting":
            group_cols += ["pl.name AS name"]; group_exprs += ["pl.name"]
            group_cols += ["pl.position AS position"]; group_exprs += ["pl.position"]
        else:
            group_cols += ["pt.name AS name"]; group_exprs += ["pt.name"]

    from_sql = f"FROM {tbl} {join_g}"
    if "player" in gb and src == "batting":
        from_sql += " JOIN mlb.players pl ON pl.id = bgs.player_id"
    elif "player" in gb and src == "pitching":
        from_sql += " JOIN mlb.players pt ON pt.id = pss.player_id"
    sql = f"SELECT {', '.join(group_cols + [sel])} {from_sql} WHERE {' AND '.join(conds) if conds else '1=1'}"
    having = []
    if group_exprs:  # per-player leaderboard
        # batting qualification: min season AB (e.g. a leaderboard shouldn't rank
        # 2-AB call-ups above everyday players)
        min_ab = filt.get("min_at_bats")
        if src == "batting" and min_ab is not None:
            try:
                mi = int(min_ab)
            except (TypeError, ValueError):
                return {"error": f"min_at_bats must be an integer, got '{min_ab}'"}
            having.append("SUM(bgs.at_bats) >= :minab"); params["minab"] = mi
    if group_exprs:
        sql += " GROUP BY " + ", ".join(group_exprs)
        if having:
            sql += " HAVING " + " AND ".join(having)
        sql += f" ORDER BY \"{stats[0]}\" {order.upper()} NULLS LAST"
    sql, limit = apply_limit(sql, top)
    if sql is None:
        return {"error": limit}
    r = await db.execute(text(sql), params)
    rows = [dict(x) for x in r.mappings().all()]
    out = {"result": rows, "aggregate": agg, "season": season.year if season else None,
           "source": src, "stat_names": stats}
    # accurate truncation note ONLY for leaderboards (group_by present); a single-player
    # lookup is inherently one row and a note would mislead the model into thinking rows
    # were cut off. Skip the parallel COUNT for narrow lookups too.
    if group_exprs:
        true_total = await async_count(db, sql, params)
        cut = count_note(limit, len(rows), true_total)
        if cut:
            out["note"] = cut
    if not rows:
        out["note"] = "No rows"
    return out


def _g_join(alias, by_game=False):
    # Restrict to REGULAR-SEASON games: batting_game_stats/pitcher per-game tables carry
    # NO game_type column, but mlb.games does. Without this, postseason games (D/L/W/F)
    # get summed into season totals — e.g. Altuve 2022 OPS .920 (REG only) vs .884 (with
    # 12 playoff games). Season lines must be regular season only.
    return f"JOIN mlb.games g ON g.id = {alias}.game_id AND g.game_type = 'R'"



# Per-game batting rates stored on batting_game_stats must NOT be AVG()-ed — that
# produces the average of per-game rates (unweighted by AB), which drifts badly from
# the true season rate (e.g. Christian Walker 2018 SLG: true .388 vs AVG-of-games .424;
# OPS is crushed even harder). Instead SUM the underlying count columns and derive the
# true rate from the summed counts.
# ── Batting rate derivation ────────────────────────────────────────────────────
# Per-game batting rates on batting_game_stats (avg/obp/slg/ops) must NEVER be
# AVG()-ed — that yields the average of per-game rates (unweighted by AB), which
# drifts badly from the true season rate (e.g. Christian Walker 2018 SLG: true .388
# vs AVG-of-games .424; OPS crushed even harder .718 vs 2.86). Instead SUM the
# underlying count columns and derive the true rate from the summed counts, inline
# in SQL so the column is still named by the stat (keeps GROUP BY / ORDER BY valid).
# Requires the agg=sum path (counts), which is the default for batting aggregates.
_MLB_RATE_EXPR = {
    "avg": "ROUND(SUM(bgs.hits)::decimal / NULLIF(SUM(bgs.at_bats), 0), 3)",
    "slg": "ROUND(SUM(bgs.total_bases)::decimal / NULLIF(SUM(bgs.at_bats), 0), 3)",
    "obp": (
        "ROUND((SUM(bgs.hits) + SUM(bgs.base_on_balls) + SUM(bgs.hit_by_pitch))::decimal "
        "/ NULLIF(SUM(bgs.at_bats) + SUM(bgs.base_on_balls) + SUM(bgs.hit_by_pitch) "
        "+ SUM(bgs.sacrifice_flies), 0), 3)"
    ),
}


def _agg_select(counts, alias, rates=None, agg="sum"):
    parts = []
    for s, col in counts.items():
        if agg == "count":
            parts.append(f"COUNT(CASE WHEN NOT {alias}.{col} IS NULL THEN 1 END) AS \"{s}\"")
        elif agg == "max":
            parts.append(f"MAX({alias}.{col}) AS \"{s}\"")
        elif agg == "avg":
            parts.append(f"AVG({alias}.{col}) AS \"{s}\"")
        else:
            parts.append(f"COALESCE(SUM({alias}.{col}),0) AS \"{s}\"")
    # rate stats: derive from summed counts (never AVG the stored per-game rate).
    # Only valid for the sum aggregation path.
    if agg in ("sum", None, ""):
        for s in (rates or {}):
            expr = _MLB_RATE_EXPR.get(s)
            if expr:
                parts.append(f"{expr} AS \"{s}\"")
        if "ops" in (rates or {}):
            # ops = obp + slg, both summed-derivations. Emit the derived ops column.
            slg = "ROUND(SUM(bgs.total_bases)::decimal / NULLIF(SUM(bgs.at_bats), 0), 3)"
            obp = _MLB_RATE_EXPR["obp"]
            parts.append(f"({obp} + {slg}) AS \"ops\"")
    elif rates:
        # non-sum aggregate: keep a column present so SELECT/ORDER BY don't break.
        # Rates under count/max/avg aren't meaningful; surface the raw per-game rate
        # (best-effort) rather than erroring.
        for s, col in (rates or {}).items():
            parts.append(f"MAX({alias}.{col}) AS \"{s}\"")
    return ", ".join(parts) if parts else "1"



# ── MLB team engine ────────────────────────────────────────────────────────────
MLB_TEAM_STATS_SOURCES = {
    "wins": "games", "losses": "games", "win_pct": "games",
    "runs_scored": "games", "runs_allowed": "games", "run_margin": "games",
    # pitching — per-game staff aggregate via mlb.pitcher_game_stats joined to games
    "era": "pitching", "innings_pitched": "pitching", "earned_runs": "pitching",
    "pitchers_runs_allowed": "pitching", "hits_allowed": "pitching",
    "walks_allowed": "pitching", "strikeouts": "pitching", "home_runs_allowed": "pitching",
    # rolling
    "win_pct_5": "rolling", "win_pct_10": "rolling", "wins_last_10": "rolling",
    "avg_runs_scored": "rolling", "avg_runs_allowed": "rolling",
    "avg_ops_5": "rolling", "avg_ops_10": "rolling", "era_5": "rolling", "era_10": "rolling",
    "over_pct_5": "rolling", "spread_pct_5": "rolling",
}

# Team pitching stat -> SUM(column) on mlb.pitcher_game_stats (per-game staff totals).
# ip is stored as fractional thirds (1.3 = 1 1/3). ERA derives by converting summed
# IP to outs (outs = FLOOR(ip)*3 + ROUND((ip % 1)*10)) then ER * 27 / outs.
MLB_PITCH_TEAM_AGG = {
    "innings_pitched": "SUM(pgs.ip) AS _ip",
    "earned_runs": "SUM(pgs.er) AS _er",
    "pitchers_runs_allowed": "SUM(pgs.runs_allowed) AS _runs_allowed",
    "hits_allowed": "SUM(pgs.h) AS _h",
    "walks_allowed": "SUM(pgs.bb) AS _bb",
    "strikeouts": "SUM(pgs.k) AS _k",
    "home_runs_allowed": "SUM(pgs.hr) AS _hr",
}

# filtered team-pitching filter allowlist
MLB_PITCH_TEAM_FILTERS = {"team", "season_year", "month", "home_or_away", "opponent"}


async def _run_query_team_stats(db: AsyncSession, args: dict) -> dict:
    allowed = {"stats", "stat", "filters", "season", "top", "order"}
    for k in args:
        if k not in allowed:
            return {"error": "Invalid query spec", "details": [f"unknown spec key '{k}'"]}
    stats = args.get("stats") or ([args.get("stat")] if args.get("stat") else None)
    if not isinstance(stats, list) or not stats:
        return {"error": "Invalid query spec", "details": ["must specify 'stats' list"]}
    filt = args.get("filters") or {}
    for s in stats:
        if s not in MLB_TEAM_STATS_SOURCES:
            return {"error": f"stat '{s}' not supported"}
    sources = {MLB_TEAM_STATS_SOURCES[s] for s in stats}
    if len(sources) > 1:
        return {"error": "Mixing stats from different tables isn't allowed", "details": [f"sources {sorted(sources)}"]}
    src = sources.pop()
    team = filt.get("team")
    team_obj = await _resolve_team(db, team) if team else None
    if team and not team_obj:
        return {"error": f"Unknown team '{team}'"}
    season, _ = await _resolve_season(db, {"season": filt.get("season_year")})
    params, conds = {}, []

    if src == "games":
        if not team_obj:
            return {"error": "records stats require a 'team' filter"}
        base = "FROM mlb.games g WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)"
        params["tid"] = team_obj.id
        if season:
            conds.append("g.season_id = :sid"); params["sid"] = season.id
        def _ge(name):
            if name == "wins":
                return "SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score > g.away_score) OR (g.away_team_id = :tid AND g.away_score > g.home_score) THEN 1 ELSE 0 END)"
            if name == "losses":
                return "SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score < g.away_score) OR (g.away_team_id = :tid AND g.away_score < g.home_score) THEN 1 ELSE 0 END)"
            if name == "win_pct":
                return "ROUND(100.0 * SUM(CASE WHEN (g.home_team_id = :tid AND g.home_score > g.away_score) OR (g.away_team_id = :tid AND g.away_score > g.home_score) THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 1)"
            if name == "runs_scored":
                return "SUM(CASE WHEN g.home_team_id = :tid THEN g.home_score ELSE g.away_score END)"
            if name == "runs_allowed":
                return "SUM(CASE WHEN g.home_team_id = :tid THEN g.away_score ELSE g.home_score END)"
            if name == "run_margin":
                return "SUM(CASE WHEN g.home_team_id = :tid THEN g.home_score - g.away_score ELSE g.away_score - g.home_score END)"
            return "COUNT(*)"
        sel = ", ".join(f"{_ge(s)} AS \"{s}\"" for s in stats)
        sql = f"SELECT {sel} {base} AND {' AND '.join(conds) if conds else '1=1'}"
    elif src == "pitching":
        # pgs -> games join (games carries season/date/home-away). pgs.team_abbr
        # gives which side pitched, so home_or_away/opponent are derivable.
        if not team_obj:
            return {"error": "pitching stats require a 'team' filter"}
        for k in filt:
            if k not in MLB_PITCH_TEAM_FILTERS:
                return {"error": f"unsupported filter '{k}' for pitching stats"}
        base = "FROM mlb.pitcher_game_stats pgs JOIN mlb.games g ON g.id = pgs.game_id AND g.game_type = 'R'"
        # team's games: pitcher's team must be one of the two contestants
        team_abbr = (
            await db.execute(text("SELECT abbreviation FROM mlb.teams WHERE id = :tid"),
                             {"tid": team_obj.id})
        ).scalar()
        if not team_abbr:
            return {"error": "Unknown team '{team}'"}
        conds.append("pgs.team_abbr = :tabbr"); params["tabbr"] = team_abbr
        if season:
            conds.append("g.season_id = :sid"); params["sid"] = season.id
        hoa = filt.get("home_or_away")
        if hoa:
            h = hoa.strip().lower()
            if h not in ("home", "away", "road"):
                return {"error": f"home_or_away must be 'home' or 'away', got '{hoa}'"}
            home_side = ("pgs.team_abbr = (SELECT t.abbreviation FROM mlb.teams t WHERE t.id = g.home_team_id)")
            if h == "home":
                conds.append(home_side)
            else:
                conds.append(f"NOT ({home_side})")
        month = filt.get("month")
        if month is not None:
            try:
                m = int(month)
            except (TypeError, ValueError):
                return {"error": f"month must be an integer 1-12, got '{month}'"}
            if m < 1 or m > 12:
                return {"error": f"month must be 1-12, got {m}"}
            conds.append("EXTRACT(MONTH FROM g.date) = :month"); params["month"] = m
        opp = filt.get("opponent")
        if opp:
            o = await _resolve_team(db, opp)
            if not o:
                return {"error": f"Unknown opponent '{opp}'"}
            conds.append(
                "(g.home_team_id = :oid OR g.away_team_id = :oid)"); params["oid"] = o.id
        # Build aggregate: sum raw pitching contributions, then fold ip/er into ERA.
        agg_bits = []
        for s in stats:
            if s in MLB_PITCH_TEAM_AGG:
                agg_bits.append(MLB_PITCH_TEAM_AGG[s])
        # `era` is derived, not summed directly: always include ip + er internally.
        if "era" in stats:
            agg_bits.append("SUM(pgs.ip) AS _ip")
            agg_bits.append("SUM(pgs.er) AS _era_er")
        sel_agg = ", ".join(dict.fromkeys(agg_bits))
        sql = f"SELECT {sel_agg} {base} WHERE {' AND '.join(conds) if conds else '1=1'}"
        r = await db.execute(text(sql), params)
        row = r.mappings().first()
        if not row:
            return {"result": [], "source": src, "stat_names": stats,
                    "season": season.year if season else None}
        # derive final values
        out = {}
        ip = float(row.get("_ip") or 0)
        tmp = {}
        for s in stats:
            if s not in MLB_PITCH_TEAM_AGG:
                continue  # era is derived below, not summed
            col = MLB_PITCH_TEAM_AGG[s]
            colname = col.split(" AS ")[-1].strip()
            tmp[s] = row[colname]
        outs = math.floor(ip) * 3 + round((ip - math.floor(ip)) * 10)
        for s in stats:
            if s == "era":
                er = float(row["_era_er"] or 0)
                out[s] = round((er * 27.0) / outs, 2) if outs else None
            elif s == "innings_pitched":
                out[s] = float(tmp[s])
            elif s == "pitchers_runs_allowed":
                out[s] = int(tmp[s] or 0)
            else:
                out[s] = int(tmp[s] or 0)
        rows = [out]
        # no top/order for a single-team aggregate
        return {"result": rows, "source": src, "stat_names": stats,
                "season": season.year if season else None}
    else:  # rolling
        t = await db.execute(text("SELECT id FROM mlb.teams WHERE abbreviation = :abbr OR name ILIKE :n"),
                             {"abbr": (team or ""), "n": f"%{team or ''}%"})
        tr = t.first()
        if not tr and not team_obj:
            return {"error": f"Unknown team '{team}'"}
        base = "FROM mlb.team_rolling_stats trs WHERE 1=1"
        if team_obj:
            conds.append("trs.team_id = :tid"); params["tid"] = team_obj.id
        if season:
            conds.append("trs.season_id = :sid"); params["sid"] = season.id
        roll_map = {
            "win_pct_5": "win_pct5", "win_pct_10": "win_pct10", "wins_last_10": "wins_l10",
            "avg_runs_scored": "rf_avg", "avg_runs_allowed": "ra_avg",
            "avg_ops_5": "ops5", "avg_ops_10": "ops10", "era_5": "era5", "era_10": "era10",
            "over_pct_5": "over_pct5", "spread_pct_5": "spread_pct5",
        }
        sel = ", ".join(f"AVG(trs.{roll_map[s]}) AS \"{s}\"" for s in stats)
        sql = f"SELECT {sel} {base} AND {' AND '.join(conds) if conds else '1=1'}"

    top = args.get("top")
    order = (args.get("order") or "desc").lower()
    r = await db.execute(text(sql), params)
    rows = [dict(x) for x in r.mappings().all()]
    return {"result": rows, "source": src, "stat_names": stats, "season": season.year if season else None}
