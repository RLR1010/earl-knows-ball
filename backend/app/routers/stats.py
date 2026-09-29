"""Stats endpoints — sortable player & team stats by season."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.database import get_db

router = APIRouter()

# ── Allowed sort columns (whitelist to prevent SQL injection) ──────────

PLAYER_SORT_COLS = {
    # Passing
    "pass_attempts", "pass_completions", "pass_yards", "pass_tds", "pass_int",
    "comp_pct", "yards_per_att", "passer_rating",
    # Rushing
    "rush_attempts", "rush_yards", "rush_tds", "yards_per_carry",
    # Receiving
    "targets", "receptions", "receiving_yards", "receiving_tds", "yards_per_rec",
    # Defense
    "tackles_combined", "sacks", "tackles_for_loss", "qb_hits",
    "fumbles_forced", "interceptions", "passes_defended",
    # Misc
    "fumbles", "fumbles_lost", "games_played", "snaps_offense",
    "games",
}

TEAM_SORT_COLS = {
    "wins", "losses", "ties", "games",
    "points_for", "points_against", "point_diff",
    "yds_for", "yds_against", "yds_diff",
    "rush_yds_for", "rush_yds_against",
    "pass_yds_for", "pass_yds_against",
    "to_takeaways", "to_giveaways", "to_margin",
}

# ── Player Stats ──────────────────────────────────────────────────────


@router.get("/stats/players")
async def player_stats(
    year: int = Query(...),
    position: str = Query("ALL"),
    sort: str = Query("pass_yards"),
    order: str = Query("desc"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_games: int = Query(1, ge=0),
    db: AsyncSession = Depends(get_db),
):
    if sort not in PLAYER_SORT_COLS:
        sort = "pass_yards"
    direction = "DESC" if order == "desc" else "ASC"
    pos_filter = ""
    if position.upper() != "ALL":
        pos_filter = "AND p.position = :position"

    sql = f"""
    SELECT
        p.id AS player_id,
        p.name AS player_name,
        p.position,
        (ARRAY_AGG(t.abbreviation ORDER BY pws.week DESC))[1] AS team_abbr,
        COUNT(pws.id)::int AS games,
        SUM(pws.pass_attempts)::int AS pass_attempts,
        SUM(pws.pass_completions)::int AS pass_completions,
        SUM(pws.pass_yards)::int AS pass_yards,
        SUM(pws.pass_tds)::int AS pass_tds,
        SUM(pws.pass_int)::int AS pass_int,
        CASE WHEN SUM(pws.pass_attempts) > 0
            THEN ROUND(SUM(pws.pass_completions)::numeric / SUM(pws.pass_attempts) * 100, 1)
            ELSE 0 END AS comp_pct,
        CASE WHEN SUM(pws.pass_attempts) > 0
            THEN ROUND(SUM(pws.pass_yards)::numeric / SUM(pws.pass_attempts), 1)
            ELSE 0 END AS yards_per_att,
        -- NFL passer rating: each component clamped [0, 2.375], sum/6 * 100
        CASE WHEN SUM(pws.pass_attempts) > 0 THEN
            ROUND(
                (
                    LEAST(GREATEST((SUM(pws.pass_completions)::numeric / SUM(pws.pass_attempts) - 0.3) * 5, 0), 2.375)
                    + LEAST(GREATEST((SUM(pws.pass_yards)::numeric / SUM(pws.pass_attempts) - 3) * 0.25, 0), 2.375)
                    + LEAST(GREATEST(SUM(pws.pass_tds)::numeric / SUM(pws.pass_attempts) * 20, 0), 2.375)
                    + LEAST(GREATEST(2.375 - SUM(pws.pass_int)::numeric / SUM(pws.pass_attempts) * 25, 0), 2.375)
                ) / 6 * 100
            , 1) ELSE NULL END AS passer_rating,
        SUM(pws.rush_attempts)::int AS rush_attempts,
        SUM(pws.rush_yards)::int AS rush_yards,
        SUM(pws.rush_tds)::int AS rush_tds,
        CASE WHEN SUM(pws.rush_attempts) > 0
            THEN ROUND(SUM(pws.rush_yards)::numeric / SUM(pws.rush_attempts), 1)
            ELSE 0 END AS yards_per_carry,
        SUM(pws.targets)::int AS targets,
        SUM(pws.receptions)::int AS receptions,
        SUM(pws.receiving_yards)::int AS receiving_yards,
        SUM(pws.receiving_tds)::int AS receiving_tds,
        CASE WHEN SUM(pws.receptions) > 0
            THEN ROUND(SUM(pws.receiving_yards)::numeric / SUM(pws.receptions), 1)
            ELSE 0 END AS yards_per_rec,
        SUM(pws.fumbles)::int AS fumbles,
        SUM(pws.fumbles_lost)::int AS fumbles_lost,
        COALESCE(SUM(pws.snaps_offense), 0)::int AS snaps_offense,
        COALESCE(SUM(pws.tackles_combined), 0)::int AS tackles_combined,
        COALESCE(SUM(pws.sacks), 0)::numeric AS sacks,
        COALESCE(SUM(pws.tackles_for_loss), 0)::int AS tackles_for_loss,
        COALESCE(SUM(pws.qb_hits), 0)::int AS qb_hits,
        COALESCE(SUM(pws.fumbles_forced), 0)::int AS fumbles_forced,
        COALESCE(SUM(pws.interceptions), 0)::int AS interceptions,
        COALESCE(SUM(pws.passes_defended), 0)::int AS passes_defended
    FROM player_weekly_stats pws
    JOIN seasons s ON s.id = pws.season_id
    JOIN players p ON p.id = pws.player_id
    JOIN teams t ON t.id = pws.team_id
    WHERE s.year = :year AND pws.game_type = 'REG' {pos_filter}
    GROUP BY p.id, p.name, p.position
    HAVING COUNT(pws.id) >= :min_games
    ORDER BY {sort} {direction} NULLS LAST
    LIMIT :limit OFFSET :offset
    """

    params = {"year": year, "limit": limit, "offset": offset, "min_games": min_games}
    if position.upper() != "ALL":
        params["position"] = position.upper()

    result = await db.execute(text(sql), params)
    rows = [dict(r._mapping) for r in result.fetchall()]

    # ── Count total matching (for pagination) ──
    count_sql = f"""
    SELECT COUNT(*) FROM (
        SELECT 1 FROM player_weekly_stats pws
        JOIN seasons s ON s.id = pws.season_id
        JOIN players p ON p.id = pws.player_id
        WHERE s.year = :year AND pws.game_type = 'REG' {pos_filter}
        GROUP BY p.id
        HAVING COUNT(pws.id) >= :min_games
    ) sub
    """
    count_result = await db.execute(text(count_sql), params)
    total = count_result.scalar()

    return {
        "data": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "order": order,
    }


# ── Team Stats ────────────────────────────────────────────────────────


@router.get("/stats/teams")
async def team_stats(
    year: int = Query(...),
    sort: str = Query("wins"),
    order: str = Query("desc"),
    limit: int = Query(32, ge=1, le=50),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    if sort not in TEAM_SORT_COLS:
        sort = "wins"
    direction = "DESC" if order == "desc" else "ASC"

    # For each team, compute aggregate stats across home and away games
    sql = f"""
    WITH team_games AS (
        SELECT
            g.home_team_id AS team_id,
            g.away_team_id AS opp_id,
            g.home_score AS pf,
            g.away_score AS pa,
            CASE
                WHEN g.home_score > g.away_score THEN 'W'
                WHEN g.home_score < g.away_score THEN 'L'
                ELSE 'T'
            END AS result,
            g.id AS game_id
        FROM games g
        JOIN seasons s ON s.id = g.season_id
        WHERE s.year = :year AND g.game_type = 'REG' AND g.home_score IS NOT NULL

        UNION ALL

        SELECT
            g.away_team_id AS team_id,
            g.home_team_id AS opp_id,
            g.away_score AS pf,
            g.home_score AS pa,
            CASE
                WHEN g.away_score > g.home_score THEN 'W'
                WHEN g.away_score < g.home_score THEN 'L'
                ELSE 'T'
            END AS result,
            g.id AS game_id
        FROM games g
        JOIN seasons s ON s.id = g.season_id
        WHERE s.year = :year AND g.game_type = 'REG' AND g.home_score IS NOT NULL
    ),
    game_yds AS (
        SELECT game_id, team_id,
               COALESCE(SUM(pass_yards), 0)::int AS pass_yds,
               COALESCE(SUM(rush_yards), 0)::int AS rush_yds,
               COALESCE(SUM(receiving_yards), 0)::int AS rec_yds,
               COALESCE(SUM(pass_int + fumbles_lost), 0)::int AS giveaways
        FROM player_weekly_stats
        WHERE game_type = 'REG'
        GROUP BY game_id, team_id
    )
    SELECT
        t.id AS team_id,
        t.name AS team_name,
        t.abbreviation AS team_abbr,
        t.conference,
        t.division,
        tg.games,
        COALESCE(tg.wins, 0)::int AS wins,
        COALESCE(tg.losses, 0)::int AS losses,
        COALESCE(tg.ties, 0)::int AS ties,
        tg.points_for::int AS points_for,
        tg.points_against::int AS points_against,
        (tg.points_for - tg.points_against)::int AS point_diff,
        ROUND(COALESCE(ty.yds_for, 0)::numeric / NULLIF(tg.games, 0), 1) AS yds_for,
        ROUND(COALESCE(ty.yds_against, 0)::numeric / NULLIF(tg.games, 0), 1) AS yds_against,
        ROUND((COALESCE(ty.yds_for, 0) - COALESCE(ty.yds_against, 0))::numeric / NULLIF(tg.games, 0), 1) AS yds_diff,
        ROUND(COALESCE(ty.rush_yds_for, 0)::numeric / NULLIF(tg.games, 0), 1) AS rush_yds_for,
        ROUND(COALESCE(ty.rush_yds_against, 0)::numeric / NULLIF(tg.games, 0), 1) AS rush_yds_against,
        ROUND(COALESCE(ty.pass_yds_for, 0)::numeric / NULLIF(tg.games, 0), 1) AS pass_yds_for,
        ROUND(COALESCE(ty.pass_yds_against, 0)::numeric / NULLIF(tg.games, 0), 1) AS pass_yds_against,
        COALESCE(ty.to_takeaways, 0)::int AS to_takeaways,
        COALESCE(ty.to_giveaways, 0)::int AS to_giveaways,
        COALESCE(ty.to_takeaways - ty.to_giveaways, 0)::int AS to_margin
    FROM teams t
    JOIN (
        SELECT
            team_id,
            COUNT(*)::int AS games,
            SUM(CASE WHEN result = 'W' THEN 1 ELSE 0 END)::int AS wins,
            SUM(CASE WHEN result = 'L' THEN 1 ELSE 0 END)::int AS losses,
            SUM(CASE WHEN result = 'T' THEN 1 ELSE 0 END)::int AS ties,
            SUM(pf)::int AS points_for,
            SUM(pa)::int AS points_against
        FROM team_games
        GROUP BY team_id
    ) tg ON tg.team_id = t.id
    LEFT JOIN (
        SELECT
            tg.team_id,
            SUM(my.pass_yds + my.rush_yds)::int AS yds_for,
            SUM(oy.pass_yds + oy.rush_yds)::int AS yds_against,
            SUM(my.rush_yds)::int AS rush_yds_for,
            SUM(oy.rush_yds)::int AS rush_yds_against,
            SUM(my.pass_yds)::int AS pass_yds_for,
            SUM(oy.pass_yds)::int AS pass_yds_against,
            SUM(oy.giveaways)::int AS to_takeaways,
            SUM(my.giveaways)::int AS to_giveaways
        FROM team_games tg
        LEFT JOIN game_yds my ON my.game_id = tg.game_id AND my.team_id = tg.team_id
        LEFT JOIN game_yds oy ON oy.game_id = tg.game_id AND oy.team_id = tg.opp_id
        GROUP BY tg.team_id
    ) ty ON ty.team_id = t.id
    ORDER BY {sort} {direction}
    LIMIT :limit OFFSET :offset
    """

    params = {"year": year, "limit": limit, "offset": offset}

    result = await db.execute(text(sql), params)
    rows = [dict(r._mapping) for r in result.fetchall()]

    # Total teams count
    total = 32  # NFL always has 32

    return {
        "data": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort,
        "order": order,
    }


# ── Seasons (available years) ─────────────────────────────────────────


@router.get("/stats/seasons")
async def stat_seasons(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text(
            """
            SELECT DISTINCT s.year
            FROM seasons s
            JOIN player_weekly_stats pws ON pws.season_id = s.id
            ORDER BY s.year DESC
            """
        )
    )
    return {"years": [r[0] for r in result.fetchall()]}


# ── Season Leaders (Yahoo-style category cards) ───────────────────────

_RATING_EXPR = (
    "(LEAST(GREATEST((SUM(pws.pass_completions)::numeric / SUM(pws.pass_attempts) - 0.3) * 5, 0), 2.375)"
    " + LEAST(GREATEST((SUM(pws.pass_yards)::numeric / SUM(pws.pass_attempts) - 3) * 0.25, 0), 2.375)"
    " + LEAST(GREATEST(SUM(pws.pass_tds)::numeric / SUM(pws.pass_attempts) * 20, 0), 2.375)"
    " + LEAST(GREATEST(2.375 - SUM(pws.pass_int)::numeric / SUM(pws.pass_attempts) * 25, 0), 2.375)"
    ") / 6 * 100"
)

# group -> [(key, title, value_expr, having, unit)]
_LEADER_GROUPS = [
    ("Passing", [
        ("pass_yards", "Pass Yds", "SUM(pws.pass_yards)::int", "SUM(pws.pass_attempts) >= 50", "int"),
        ("pass_tds", "Pass TD", "SUM(pws.pass_tds)::int", "SUM(pws.pass_attempts) >= 50", "int"),
        ("passer_rating", "Passer Rating", _RATING_EXPR, "SUM(pws.pass_attempts) >= 50", "rating"),
    ]),
    ("Rushing", [
        ("rush_yards", "Rush Yds", "SUM(pws.rush_yards)::int", "SUM(pws.rush_attempts) >= 25", "int"),
        ("rush_tds", "Rush TD", "SUM(pws.rush_tds)::int", "SUM(pws.rush_attempts) >= 25", "int"),
        ("yards_per_carry", "Yds/Carry", "ROUND(SUM(pws.rush_yards)::numeric / NULLIF(SUM(pws.rush_attempts), 0), 1)", "SUM(pws.rush_attempts) >= 25", "one"),
    ]),
    ("Receiving", [
        ("receiving_yards", "Rec Yds", "SUM(pws.receiving_yards)::int", "SUM(pws.targets) >= 10", "int"),
        ("receptions", "Rec", "SUM(pws.receptions)::int", "SUM(pws.targets) >= 10", "int"),
        ("receiving_tds", "Rec TD", "SUM(pws.receiving_tds)::int", "SUM(pws.targets) >= 10", "int"),
    ]),
    ("Defense", [
        ("tackles_combined", "Tackles", "COALESCE(SUM(pws.tackles_combined), 0)::int", "COALESCE(SUM(pws.tackles_combined), 0) > 0", "int"),
        ("sacks", "Sacks", "COALESCE(SUM(pws.sacks), 0)::numeric", "COALESCE(SUM(pws.sacks), 0) > 0", "one"),
        ("interceptions", "INT", "COALESCE(SUM(pws.interceptions), 0)::int", "COALESCE(SUM(pws.interceptions), 0) > 0", "int"),
        ("passes_defended", "PD", "COALESCE(SUM(pws.passes_defended), 0)::int", "COALESCE(SUM(pws.passes_defended), 0) > 0", "int"),
    ]),
]


async def _leader_rows(db, year: int, expr: str, having: str, limit: int, team: str | None = None):
    team_clause = " AND t.abbreviation = :team" if team else ""
    sql = f"""
        SELECT p.id AS player_id,
               p.name AS player_name,
               p.position,
               (ARRAY_AGG(t.abbreviation ORDER BY pws.week DESC))[1] AS team_abbr,
               ({expr}) AS value
        FROM player_weekly_stats pws
        JOIN seasons s ON s.id = pws.season_id
        JOIN players p ON p.id = pws.player_id
        JOIN teams t ON t.id = pws.team_id
        WHERE s.year = :year AND pws.game_type = 'REG'{team_clause}
        GROUP BY p.id, p.name, p.position
        HAVING {having}
        ORDER BY value DESC NULLS LAST
        LIMIT :limit
    """
    result = await db.execute(text(sql), {"year": year, "limit": limit, "team": team})
    return [dict(r._mapping) for r in result.fetchall()]


@router.get("/stats/leaders")
async def stat_leaders(
    year: int = Query(...),
    limit: int = Query(5, ge=1, le=25),
    group: str = Query("all"),
    team: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    wanted = None if group.lower() in ("all", "") else group.lower()
    groups = []
    for gtitle, cards in _LEADER_GROUPS:
        if wanted and gtitle.lower() != wanted:
            continue
        out_cards = []
        for key, title, expr, having, unit in cards:
            rows = await _leader_rows(db, year, expr, having, limit, team)
            out_cards.append({
                "key": key,
                "title": title,
                "unit": unit,
                "rows": [{"rank": i + 1, **r} for i, r in enumerate(rows)],
            })
        groups.append({"title": gtitle, "cards": out_cards})
    return {"year": year, "limit": limit, "groups": groups}


# ── Team season stats (Yahoo-style team page section) ──────────────────

_NFL_TEAM_METRICS = [
    ("Offense", [
        ("Points/G", "ppg", True),
        ("Total Yds/G", "tot_ypg", True),
        ("Pass Yds/G", "pass_ypg", True),
        ("Rush Yds/G", "rush_ypg", True),
        ("Giveaways", "giveaways", False),
    ]),
    ("Defense", [
        ("Points Allowed/G", "papg", False),
        ("Sacks", "sacks", True),
        ("Takeaways", "takeaways", True),
        ("Interceptions", "def_int", True),
    ]),
]


@router.get("/stats/team/{abbr}")
async def stat_team(abbr: str, year: int = Query(...), db: AsyncSession = Depends(get_db)):
    abbr = abbr.upper()
    tw = [dict(r) for r in (await db.execute(text("""
        SELECT tw.team AS abbr, COUNT(*) AS g,
               COALESCE(SUM(tw.passing_yards), 0) AS pass_yds,
               COALESCE(SUM(tw.rushing_yards), 0) AS rush_yds,
               COALESCE(SUM(tw.passing_interceptions), 0) AS pass_int,
               COALESCE(SUM(tw.fumbles_lost_total), 0) AS fum_lost,
               COALESCE(SUM(tw.def_sacks), 0) AS sacks,
               COALESCE(SUM(tw.def_interceptions), 0) AS def_int,
               COALESCE(SUM(tw.fumble_recovery_opp), 0) AS fum_rec_opp
        FROM nfl.stats_team_week tw
        WHERE tw.season = :year AND tw.season_type = 'REG'
        GROUP BY tw.team
    """), {"year": year})).mappings().all()]
    sc = [dict(r) for r in (await db.execute(text("""
        SELECT t.abbreviation AS abbr, COUNT(*) AS g,
               SUM(CASE WHEN g.home_team_id = t.id THEN g.home_score ELSE g.away_score END) AS pf,
               SUM(CASE WHEN g.home_team_id = t.id THEN g.away_score ELSE g.home_score END) AS pa,
               SUM(CASE WHEN (g.home_team_id = t.id AND g.home_score > g.away_score)
                          OR (g.away_team_id = t.id AND g.away_score > g.home_score) THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN (g.home_team_id = t.id AND g.home_score < g.away_score)
                          OR (g.away_team_id = t.id AND g.away_score < g.home_score) THEN 1 ELSE 0 END) AS losses,
               SUM(CASE WHEN g.home_score = g.away_score THEN 1 ELSE 0 END) AS ties
        FROM nfl.games g
        JOIN nfl.teams t ON t.id = g.home_team_id OR t.id = g.away_team_id
        WHERE g.season_id = (SELECT id FROM nfl.seasons WHERE year = :year)
          AND g.status::text = 'FINAL' AND g.week <= 18
        GROUP BY t.abbreviation
    """), {"year": year})).mappings().all()]

    by = {r["abbr"]: dict(r) for r in tw}
    for r in sc:
        if r["abbr"] in by:
            by[r["abbr"]]["g_tw"] = by[r["abbr"]].get("g")
            by[r["abbr"]].update(r)
        else:
            by[r["abbr"]] = dict(r)

    teams = []
    for a, d in by.items():
        g_sc = d.get("g") or 0
        g_tw = d.get("g_tw") or g_sc
        if not g_sc:
            continue
        pf = d.get("pf") or 0
        pa = d.get("pa") or 0
        pass_yds = d.get("pass_yds") or 0
        rush_yds = d.get("rush_yds") or 0
        d["ppg"] = round(pf / g_sc, 1)
        d["papg"] = round(pa / g_sc, 1)
        d["pass_ypg"] = round(pass_yds / g_tw, 1)
        d["rush_ypg"] = round(rush_yds / g_tw, 1)
        d["tot_ypg"] = round((pass_yds + rush_yds) / g_tw, 1)
        d["giveaways"] = int((d.get("pass_int") or 0) + (d.get("fum_lost") or 0))
        d["takeaways"] = int((d.get("def_int") or 0) + (d.get("fum_rec_opp") or 0))
        d["sacks"] = round(float(d.get("sacks") or 0), 1)
        d["def_int"] = int(d.get("def_int") or 0)
        d.setdefault("ranks", {})
        teams.append(d)

    for _title, metrics in _NFL_TEAM_METRICS:
        for _label, key, higher in metrics:
            order = sorted(range(len(teams)), key=lambda i: (teams[i].get(key) is None,
                                                             -(teams[i].get(key) or 0) if higher else (teams[i].get(key) or 0)))
            for rank, i in enumerate(order, 1):
                teams[i]["ranks"][key] = rank

    tgt = next((t for t in teams if t["abbr"] == abbr), None)
    if not tgt:
        return {"year": year, "found": False, "team": {"abbr": abbr}}

    sections = []
    for title, metrics in _NFL_TEAM_METRICS:
        rows = []
        for label, key, _h in metrics:
            v = tgt.get(key)
            rows.append({"label": label, "value": v, "rank": tgt["ranks"].get(key),
                         "unit": "one" if isinstance(v, float) else "int"})
        sections.append({"title": title, "rows": rows})

    leader_groups = []
    for gtitle, cards in _LEADER_GROUPS:
        out_cards = []
        for key, title, expr, having, unit in cards:
            rows = await _leader_rows(db, year, expr, having, 5, abbr)
            out_cards.append({"key": key, "title": title, "unit": unit,
                              "rows": [{"rank": i + 1, **r} for i, r in enumerate(rows)]})
        leader_groups.append({"title": gtitle, "cards": out_cards})

    return {
        "year": year,
        "found": True,
        "team": {"abbr": abbr},
        "record": {"wins": tgt.get("wins") or 0, "losses": tgt.get("losses") or 0,
                   "ties": tgt.get("ties") or 0, "games": tgt.get("g") or 0},
        "sections": sections,
        "leader_groups": leader_groups,
    }
