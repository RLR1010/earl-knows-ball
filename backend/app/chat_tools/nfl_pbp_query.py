"""nfl_pbp_query — allowlisted PLAY-BY-PLAY query engine for NFL chat/research.

WHY THIS EXISTS
---------------
`nfl.game_stats` exposes pre-aggregated per-game totals (yards, EPA, explosive
plays, etc.), but it cannot answer *situational* questions that only the raw
`nfl.play_by_play` table can: "How does Mahomes do on 3rd & long?", "How many
explosive runs has the Bills offense allowed on 1st down?", "Show me every play
in the final two minutes of the Chiefs-Broncos game", "Which teams convert the
most 4th downs in the red zone?". This engine gives the model safe, structured
access to that table.

SAFETY MODEL (same as nfl_query.py)
-----------------------------------
  * The model NEVER supplies SQL. It supplies only *metric names*, *filter
    values*, and *group_by field names* drawn from the allowlists below.
  * Every identifier emitted into SQL comes from a hardcoded Python dict; every
    value is a bound parameter.
  * Results are bounded (see _query_guard) so a wild query can't flood context.

DEFAULT SEASON
--------------
Play-by-play holds multiple seasons. If the model gives no season filter, the
query defaults to the CURRENT season (mirrors the other NFL tools) so "the
Bills on 3rd down" means *this* year unless the model says otherwise.

Public surface:
  * _run_query_play_stats(db, args)  -> aggregated stats over plays
  * _run_search_plays(db, args)      -> a list of individual matching plays
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ._query_guard import apply_limit, count_note, async_count
from .nfl import _resolve_season_year, _resolve_team_id

logger = logging.getLogger(__name__)

PBP_TABLE = "nfl.play_by_play"

# ---- allowlisted aggregate metrics (each is a complete SQL aggregate over alias p) ----
PLAY_STATS: dict[str, str] = {
    "plays": "COUNT(*)",
    "yards": "COALESCE(SUM(p.yards_gained), 0)",
    "yards_per_play": "AVG(p.yards_gained)",
    "pass_plays": "COALESCE(SUM(p.pass_attempt), 0)",
    "rush_plays": "COALESCE(SUM(p.rush_attempt), 0)",
    "completions": "COALESCE(SUM(p.complete_pass), 0)",
    "pass_yards": "COALESCE(SUM(CASE WHEN p.pass_attempt = 1 THEN p.yards_gained ELSE 0 END), 0)",
    "rush_yards": "COALESCE(SUM(CASE WHEN p.rush_attempt = 1 THEN p.yards_gained ELSE 0 END), 0)",
    "yards_per_pass": (
        "CASE WHEN SUM(p.pass_attempt) > 0 THEN "
        "SUM(CASE WHEN p.pass_attempt = 1 THEN p.yards_gained ELSE 0 END)::float / SUM(p.pass_attempt) "
        "ELSE NULL END"
    ),
    "yards_per_rush": (
        "CASE WHEN SUM(p.rush_attempt) > 0 THEN "
        "SUM(CASE WHEN p.rush_attempt = 1 THEN p.yards_gained ELSE 0 END)::float / SUM(p.rush_attempt) "
        "ELSE NULL END"
    ),
    "interceptions": "COALESCE(SUM(p.interception), 0)",
    "fumbles_lost": "COALESCE(SUM(p.fumble_lost), 0)",
    "turnovers": "COALESCE(SUM(p.interception), 0) + COALESCE(SUM(p.fumble_lost), 0)",
    "touchdowns": "COALESCE(SUM(p.touchdown), 0)",
    "scoring_plays": "COALESCE(SUM(p.scoring_play), 0)",
    "first_downs": "COALESCE(SUM(p.first_down), 0)",
    "third_down_attempts": "COALESCE(SUM(p.third_down_attempted), 0)",
    "third_down_conversions": "COALESCE(SUM(p.third_down_converted), 0)",
    "third_down_pct": (
        "CASE WHEN SUM(p.third_down_attempted) > 0 THEN "
        "100.0 * SUM(p.third_down_converted) / SUM(p.third_down_attempted) ELSE NULL END"
    ),
    "fourth_down_attempts": "COALESCE(SUM(p.fourth_down_attempted), 0)",
    "fourth_down_conversions": "COALESCE(SUM(p.fourth_down_converted), 0)",
    "fourth_down_pct": (
        "CASE WHEN SUM(p.fourth_down_attempted) > 0 THEN "
        "100.0 * SUM(p.fourth_down_converted) / SUM(p.fourth_down_attempted) ELSE NULL END"
    ),
    "explosive_plays": "COALESCE(SUM(CASE WHEN p.yards_gained >= :explosive_threshold THEN 1 ELSE 0 END), 0)",
    "goal_to_go_plays": "COALESCE(SUM(CASE WHEN p.yardline_100 <= 5 THEN 1 ELSE 0 END), 0)",
}

_TOP_LEVEL_METRIC_KEYS = ("stats", "stat", "metrics", "metric")

PLAY_TYPES = {"pass", "run", "punt", "field_goal", "kickoff", "extra_point", "no_play", "qb_kneel", "qb_spike"}
GAME_TYPES = {"REG": "REG", "POST": "POST", "PRE": "PRE", "REGULAR": "REG", "PLAYOFF": "POST",
              "PLAYOFFS": "POST", "POSTSEASON": "POST", "PRESEASON": "PRE", "PRE_SEASON": "PRE"}

# group_by field -> SQL expression (whitelisted identifiers only)
GROUP_COLS: dict[str, str] = {
    "team": "p.posteam",
    "opponent": "p.defteam",
    "week": "p.week",
    "game_type": "p.season_type",
    "play_type": "p.play_type",
    "down": "p.down",
    "qtr": "p.qtr",
    "home_or_away": "p.posteam_type",
}
GROUP_LABELS = {
    "team": "team_abbr", "opponent": "opponent_abbr", "week": "week",
    "game_type": "game_type", "play_type": "play_type", "down": "down",
    "qtr": "quarter", "home_or_away": "home_or_away",
}

_INTERNAL_NOTE = (
    "present this to the user as normal analysis; never quote raw SQL, table or "
    "column names, filters dicts, or tool internals"
)


async def _resolve_abbr(db: AsyncSession, name) -> str | None:
    """Resolve a team name/abbreviation to its canonical abbreviation (e.g. KC)."""
    if name is None:
        return None
    raw = str(name).strip()
    if not raw:
        return None
    tid = await _resolve_team_id(db, raw)
    if tid is None:
        return None
    row = (await db.execute(text("SELECT abbreviation FROM nfl.teams WHERE id = :i"), {"i": tid})).first()
    return row[0] if row else None


async def _build_filters(db: AsyncSession, filt: dict):
    """Translate a filters dict into (conditions, params, notes). Allowlist only."""
    conds: list[str] = []
    params: dict = {}
    notes: list[str] = []
    filt = filt or {}

    # season window
    sy = filt.get("season_year") or filt.get("season")
    if sy is not None:
        try:
            params["season_year"] = int(sy)
            conds.append("p.season = :season_year")
        except (TypeError, ValueError):
            notes.append(f"ignored invalid season_year={sy!r}")
    else:
        try:
            params["season_year"] = int(await _resolve_season_year(db))
            conds.append("p.season = :season_year")
            notes.append(f"no season given; defaulting to current season {params['season_year']}")
        except Exception:
            pass
    if filt.get("min_season") is not None:
        params["min_season"] = int(filt["min_season"])
        conds.append("p.season >= :min_season")
    if filt.get("max_season") is not None:
        params["max_season"] = int(filt["max_season"])
        conds.append("p.season <= :max_season")

    # weeks
    if filt.get("week") is not None:
        params["week"] = int(filt["week"])
        conds.append("p.week = :week")
    if filt.get("min_week") is not None:
        params["min_week"] = int(filt["min_week"])
        conds.append("p.week >= :min_week")
    if filt.get("max_week") is not None:
        params["max_week"] = int(filt["max_week"])
        conds.append("p.week <= :max_week")

    # game type
    gt = filt.get("game_type") or filt.get("season_type")
    if gt is not None:
        mapped = GAME_TYPES.get(str(gt).strip().upper())
        if mapped:
            params["game_type"] = mapped
            conds.append("p.season_type = :game_type")
        else:
            notes.append(f"ignored invalid game_type={gt!r}")

    # team / opponent (offense = posteam; opponent = defense)
    for key, col in (("team", "p.posteam"), ("offense", "p.posteam"), ("opponent", "p.defteam"), ("defense", "p.defteam")):
        if filt.get(key) is not None:
            abbr = await _resolve_abbr(db, filt[key])
            if abbr:
                pname = f"{key}_abbr"
                params[pname] = abbr
                conds.append(f"{col} = :{pname}")
            else:
                notes.append(f"could not resolve team {filt[key]!r}")

    # down / qtr
    if filt.get("down") is not None:
        params["down"] = int(filt["down"])
        conds.append("p.down = :down")
    if filt.get("qtr") is not None:
        params["qtr"] = int(filt["qtr"])
        conds.append("p.qtr = :qtr")

    # play type
    pt = filt.get("play_type")
    if pt is not None:
        ptl = str(pt).strip().lower().replace(" ", "_")
        if ptl in PLAY_TYPES:
            params["play_type"] = ptl
            conds.append("p.play_type = :play_type")
        else:
            notes.append(f"ignored invalid play_type={pt!r}")
    elif not filt.get("include_non_plays"):
        # Exclude non-plays by default (penalties, timeouts, and the dataset's
        # 'GAME' marker rows carry play_type=''). Keeps COUNT(*) meaning "plays".
        conds.append("p.play_type <> ''")

    # home/away (offense's perspective)
    ha = filt.get("home_or_away")
    if ha is not None:
        hal = str(ha).strip().lower()
        if hal in ("home", "away"):
            params["home_or_away"] = hal
            conds.append("p.posteam_type = :home_or_away")
        else:
            notes.append(f"ignored invalid home_or_away={ha!r}")

    # field position buckets
    if filt.get("red_zone"):
        conds.append("p.yardline_100 <= 20")
    if filt.get("goal_line"):
        conds.append("p.yardline_100 <= 5")
    if filt.get("min_yardline") is not None:  # own-yardline-ish constraint on yardline_100
        params["min_yardline"] = float(filt["min_yardline"])
        conds.append("p.yardline_100 >= :min_yardline")
    if filt.get("max_yardline") is not None:
        params["max_yardline"] = float(filt["max_yardline"])
        conds.append("p.yardline_100 <= :max_yardline")

    # yards gained window
    if filt.get("min_yards") is not None:
        params["min_yards"] = float(filt["min_yards"])
        conds.append("p.yards_gained >= :min_yards")
    if filt.get("max_yards") is not None:
        params["max_yards"] = float(filt["max_yards"])
        conds.append("p.yards_gained <= :max_yards")

    # outcomes (used mostly by search_plays)
    if filt.get("touchdown"):
        conds.append("p.touchdown = 1")
    if filt.get("turnover"):
        conds.append("(p.interception = 1 OR p.fumble_lost = 1)")
    if filt.get("text"):
        params["text"] = f"%{str(filt['text']).strip()}%"
        conds.append("p.desc_text ILIKE :text")

    return conds, params, notes


async def _run_query_play_stats(db: AsyncSession, args: dict) -> dict:
    """Aggregate allowlisted play-by-play metrics, optionally grouped."""
    args = args or {}
    # metrics: accept stats/stat/metrics/metric, or a bare string
    metrics = None
    for k in _TOP_LEVEL_METRIC_KEYS:
        if args.get(k):
            metrics = args[k]
            break
    if metrics is None:
        metrics = ["plays"]
    if isinstance(metrics, str):
        metrics = [metrics]
    bad = [m for m in metrics if m not in PLAY_STATS]
    if bad:
        return {
            "error": f"unknown stat(s): {', '.join(map(str, bad))}",
            "allowed_stats": sorted(PLAY_STATS),
            "note": _INTERNAL_NOTE,
        }

    group_by = args.get("group_by") or []
    if isinstance(group_by, str):
        group_by = [group_by]
    bad_g = [g for g in group_by if g not in GROUP_COLS]
    if bad_g:
        return {
            "error": f"unknown group_by field(s): {', '.join(map(str, bad_g))}",
            "allowed_group_by": sorted(GROUP_COLS),
            "note": _INTERNAL_NOTE,
        }

    try:
        explosive_threshold = int(args.get("explosive_threshold", 20))
    except (TypeError, ValueError):
        explosive_threshold = 20

    conds, params, notes = await _build_filters(db, args.get("filters") or {})
    params["explosive_threshold"] = explosive_threshold

    select_parts = []
    group_exprs = []
    for g in group_by:
        select_parts.append(f"{GROUP_COLS[g]} AS {GROUP_LABELS[g]}")
        group_exprs.append(GROUP_COLS[g])
    for m in metrics:
        select_parts.append(f"{PLAY_STATS[m]} AS {m}")

    where = f" WHERE {' AND '.join(conds)}" if conds else ""
    group_sql = f" GROUP BY {', '.join(group_exprs)}" if group_exprs else ""

    order = str(args.get("order", "desc")).lower()
    direction = "ASC" if order == "asc" else "DESC"
    order_sql = ""
    if group_by:
        order_sql = f" ORDER BY {metrics[0]} {direction} NULLS LAST"

    sql = f"SELECT {', '.join(select_parts)} FROM {PBP_TABLE} p{where}{group_sql}{order_sql}"
    sql, limit = apply_limit(sql, args.get("top"))
    if sql is None:
        return {"error": str(limit), "note": _INTERNAL_NOTE}

    try:
        result = await db.execute(text(sql), params)
        rows = [dict(r._mapping) for r in result.fetchall()]
    except Exception as exc:  # pragma: no cover - surfaced to model, not raised
        logger.warning("pbp query failed: %s", exc)
        return {"error": f"query failed: {exc}", "note": _INTERNAL_NOTE}

    note = count_note(limit, len(rows), await async_count(db, sql, params)) if group_by else None

    out = {
        "result": rows,
        "stats": metrics,
        "group_by": group_by,
        "filters": args.get("filters") or {},
        "explosive_threshold": explosive_threshold,
        "note": _INTERNAL_NOTE,
    }
    if note:
        out["truncation"] = note
    if notes:
        out["filter_notes"] = notes
    return out


async def _run_search_plays(db: AsyncSession, args: dict) -> dict:
    """Return a bounded list of individual plays matching the filters.

    Flat arguments (season_year, week, team, opponent, down, play_type, qtr,
    min_yards, max_yards, touchdown, turnover, text, game_type, red_zone,
    goal_line, order, top)."""
    args = args or {}
    # Allow nested filters too (mirrors query_play_stats).
    merged = dict(args.get("filters") or {})
    for k, v in args.items():
        if k not in ("filters", "top", "order", "descending"):
            merged.setdefault(k, v)

    conds, params, notes = await _build_filters(db, merged)
    where = f" WHERE {' AND '.join(conds)}" if conds else ""

    order = str(args.get("order", "desc")).lower()
    if order == "asc":
        order_sql = " ORDER BY p.season ASC, p.week ASC, p.old_game_id ASC, p.play_id ASC"
    elif order == "yards":
        order_sql = " ORDER BY p.yards_gained DESC NULLS LAST"
    else:
        order_sql = " ORDER BY p.season DESC, p.week DESC, p.old_game_id DESC, p.play_id ASC"

    cols = (
        "p.season AS season, p.week AS week, p.season_type AS game_type, "
        "p.old_game_id AS game_id, p.posteam AS team_abbr, p.defteam AS opponent_abbr, "
        "p.qtr AS quarter, p.down AS down, p.ydstogo AS yards_to_go, "
        "p.yardline_100 AS yardline_100, p.play_type AS play_type, "
        "p.yards_gained AS yards_gained, p.touchdown AS touchdown, "
        "p.interception AS interception, p.fumble_lost AS fumble_lost, "
        "p.desc_text AS description"
    )
    sql = f"SELECT {cols} FROM {PBP_TABLE} p{where}{order_sql}"
    # Individual plays are verbose; keep the list small by default and capped.
    try:
        req_top = int(args.get("top") or 25)
    except (TypeError, ValueError):
        req_top = 25
    req_top = max(1, min(req_top, 200))
    sql, limit = apply_limit(sql, req_top)
    if sql is None:
        return {"error": str(limit), "note": _INTERNAL_NOTE}

    try:
        result = await db.execute(text(sql), params)
        rows = [dict(r._mapping) for r in result.fetchall()]
    except Exception as exc:  # pragma: no cover
        logger.warning("pbp search failed: %s", exc)
        return {"error": f"query failed: {exc}", "note": _INTERNAL_NOTE}

    total = await async_count(db, sql, params)
    note = count_note(limit, len(rows), total)

    out = {"result": rows, "count_returned": len(rows), "note": _INTERNAL_NOTE}
    if note:
        out["truncation"] = note
    if notes:
        out["filter_notes"] = notes
    return out
