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
    # Fantasy
    "fantasy_points_ppr", "fantasy_points_std", "fantasy_points_half",
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
        STRING_AGG(DISTINCT t.abbreviation, '/' ORDER BY t.abbreviation) AS team_abbr,
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
        COALESCE(SUM(pws.fantasy_points_ppr), 0)::numeric(10,1) AS fantasy_points_ppr,
        COALESCE(SUM(pws.fantasy_points_std), 0)::numeric(10,1) AS fantasy_points_std,
        COALESCE(SUM(pws.fantasy_points_half), 0)::numeric(10,1) AS fantasy_points_half,
        COALESCE(SUM(pws.snaps_offense), 0)::int AS snaps_offense
    FROM player_weekly_stats pws
    JOIN seasons s ON s.id = pws.season_id
    JOIN players p ON p.id = pws.player_id
    JOIN teams t ON t.id = pws.team_id
    WHERE s.year = :year {pos_filter}
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
        WHERE s.year = :year {pos_filter}
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
