"""NBA stats endpoints — player stats, team standings, game schedules."""

from fastapi import APIRouter, Depends, Query
import datetime
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from sqlalchemy.orm import joinedload
from sqlalchemy import func
from app.models.nba import NBAGame, NBAPlayer, NBAPlayerSeasonStats, NBASeason, NBATeam

router = APIRouter()

SORT_COLS = {
    "points", "points_per_game", "assists", "assists_per_game",
    "rebounds", "rebounds_per_game", "steals", "blocks",
    "field_goal_pct", "three_point_pct", "free_throw_pct",
    "games_played", "minutes_played", "turnovers",
    "fantasy_points", "efficiency",
}


@router.get("/nba/stats/players")
async def nba_player_stats(
    year: int = Query(...),
    sort: str = Query("points_per_game"),
    order: str = Query("desc"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_games: int = Query(1, ge=0),
    db: AsyncSession = Depends(get_db),
):
    if sort not in SORT_COLS:
        sort = "points_per_game"
    direction = "DESC" if order == "desc" else "ASC"

    sql = f"""
    SELECT
        p.id AS player_id,
        p.name AS player_name,
        p.position,
        t.abbreviation AS team_abbr,
        ps.games_played,
        ps.games_started,
        ps.minutes_played,
        ps.points,
        ps.points_per_game,
        ps.field_goals_made,
        ps.field_goals_attempted,
        ps.field_goal_pct,
        ps.three_points_made,
        ps.three_points_attempted,
        ps.three_point_pct,
        ps.free_throws_made,
        ps.free_throws_attempted,
        ps.free_throw_pct,
        ps.rebounds,
        ps.offensive_rebounds,
        ps.defensive_rebounds,
        ps.rebounds_per_game,
        ps.assists,
        ps.assists_per_game,
        ps.turnovers,
        ps.steals,
        ps.blocks,
        ps.personal_fouls,
        ps.plus_minus,
        ps.fantasy_points,
        ps.efficiency
    FROM nba.player_season_stats ps
    JOIN nba.players p ON p.id = ps.player_id
    LEFT JOIN nba.teams t ON t.id = ps.team_id
    JOIN nba.seasons s ON s.id = ps.season_id
    WHERE s.year = :year AND ps.games_played >= :min_games
    ORDER BY ps.{sort} {direction} NULLS LAST
    LIMIT :limit OFFSET :offset
    """
    result = await db.execute(
        text(sql), {"year": year, "limit": limit, "offset": offset, "min_games": min_games}
    )
    rows = result.mappings().all()

    count_sql = """
    SELECT COUNT(*) FROM nba.player_season_stats ps
    JOIN nba.seasons s ON s.id = ps.season_id
    WHERE s.year = :year AND ps.games_played >= :min_games
    """
    total = (await db.execute(text(count_sql), {"year": year, "min_games": min_games})).scalar()

    return {"data": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset, "sort": sort, "order": order}


@router.get("/nba/stats/teams")
async def nba_team_stats(
    year: int = Query(...),
    sort: str = Query("wins"),
    order: str = Query("desc"),
    db: AsyncSession = Depends(get_db),
):
    direction = "DESC" if order == "desc" else "ASC"
    sql = f"""
    SELECT
        t.id AS team_id,
        t.name AS team_name,
        t.abbreviation AS team_abbr,
        t.conference,
        t.division,
        COUNT(g.id) AS games,
        SUM(CASE WHEN g.status::text = 'final' AND (
            (g.home_team_id = t.id AND g.home_score > g.away_score)
            OR (g.away_team_id = t.id AND g.away_score > g.home_score)
        ) THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN g.status::text = 'final' AND (
            (g.home_team_id = t.id AND g.home_score < g.away_score)
            OR (g.away_team_id = t.id AND g.away_score < g.home_score)
        ) THEN 1 ELSE 0 END) AS losses
    FROM nba.teams t
    LEFT JOIN nba.games g ON (g.home_team_id = t.id OR g.away_team_id = t.id)
        AND g.season_id = (SELECT id FROM nba.seasons WHERE year = :year)
        AND g.status::text = 'final'
    GROUP BY t.id, t.name, t.abbreviation, t.conference, t.division
    ORDER BY wins {direction} NULLS LAST
    """
    result = await db.execute(text(sql), {"year": year})
    return {"data": [dict(r) for r in result.mappings().all()]}


@router.get("/nba/players")
async def nba_players(
    position: str = Query(None),
    search: str = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    conditions = ["1=1"]
    params = {"limit": limit}
    if position:
        conditions.append("p.position = :position")
        params["position"] = position.upper()
    if search:
        conditions.append("p.name ILIKE :search")
        params["search"] = f"%{search}%"
    where = " AND ".join(conditions)
    sql = f"""
    SELECT p.id, p.name, p.position, p.nba_id, p.jersey_number,
           p.height, p.weight, p.college, p.years_exp, p.status,
           p.headshot_url, t.abbreviation AS team_abbr, t.name AS team_name
    FROM nba.players p
    LEFT JOIN nba.teams t ON t.id = p.team_id
    WHERE {where}
    ORDER BY p.name LIMIT :limit
    """
    result = await db.execute(text(sql), params)
    return [dict(r) for r in result.mappings().all()]


@router.get("/nba/games/{game_id}/boxscore")
async def nba_game_boxscore(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return detailed NBA game boxscore with home/away stats."""
    result = await db.execute(
        select(NBAGame)
        .options(joinedload(NBAGame.home_team), joinedload(NBAGame.away_team))
        .where(NBAGame.id == game_id)
    )
    game = result.unique().scalar_one_or_none()
    if not game:
        return {"error": "Game not found"}

    from datetime import datetime

    def _get_val(val):
        if val is not None:
            return float(val)
        return None

    home_stats = {
        "team": game.home_team.abbreviation if game.home_team else None,
        "team_id": game.home_team.id if game.home_team else None,
        "score": _get_val(game.home_score),
        "field_goals_made": _get_val(game.home_field_goals_made),
        "field_goals_attempted": _get_val(game.home_field_goals_attempted),
        "three_points_made": _get_val(game.home_three_points_made),
        "three_points_attempted": _get_val(game.home_three_points_attempted),
        "free_throws_made": _get_val(game.home_free_throws_made),
        "free_throws_attempted": _get_val(game.home_free_throws_attempted),
        "rebounds": _get_val(game.home_rebounds),
        "assists": _get_val(game.home_assists),
    }
    away_stats = {
        "team": game.away_team.abbreviation if game.away_team else None,
        "team_id": game.away_team.id if game.away_team else None,
        "score": _get_val(game.away_score),
        "field_goals_made": _get_val(game.away_field_goals_made),
        "field_goals_attempted": _get_val(game.away_field_goals_attempted),
        "three_points_made": _get_val(game.away_three_points_made),
        "three_points_attempted": _get_val(game.away_three_points_attempted),
        "free_throws_made": _get_val(game.away_free_throws_made),
        "free_throws_attempted": _get_val(game.away_free_throws_attempted),
        "rebounds": _get_val(game.away_rebounds),
        "assists": _get_val(game.away_assists),
    }

    # Compute percentages
    for side in (home_stats, away_stats):
        fga = side.get("field_goals_attempted")
        fgm = side.get("field_goals_made")
        side["field_goal_pct"] = round(fgm / fga, 3) if fga and fga > 0 else None

        tpa = side.get("three_points_attempted")
        tpm = side.get("three_points_made")
        side["three_point_pct"] = round(tpm / tpa, 3) if tpa and tpa > 0 else None

        fta = side.get("free_throws_attempted")
        ftm = side.get("free_throws_made")
        side["free_throw_pct"] = round(ftm / fta, 3) if fta and fta > 0 else None

    # Include player stats if available
    from app.models.nba.player_game_stats import NBAPlayerGameStats
    player_rows = await db.execute(
        select(NBAPlayerGameStats)
        .where(NBAPlayerGameStats.game_id == game_id)
        .order_by(NBAPlayerGameStats.points.desc().nullslast())
    )
    player_stats_list = []
    for ps in player_rows.scalars().all():
        # Fetch player name separately
        p_row = await db.execute(select(NBAPlayer.name).where(NBAPlayer.id == ps.player_id))
        p_name = p_row.scalar_one_or_none()
        player_stats_list.append({
            "player_id": ps.player_id,
            "name": p_name,
            "team_id": ps.team_id,
            "minutes": ps.minutes,
            "field_goals_made": ps.field_goals_made,
            "field_goals_attempted": ps.field_goals_attempted,
            "field_goal_pct": ps.field_goal_pct,
            "three_pointers_made": ps.three_pointers_made,
            "three_pointers_attempted": ps.three_pointers_attempted,
            "three_pointer_pct": ps.three_pointer_pct,
            "free_throws_made": ps.free_throws_made,
            "free_throws_attempted": ps.free_throws_attempted,
            "free_throw_pct": ps.free_throw_pct,
            "rebounds_offensive": ps.rebounds_offensive,
            "rebounds_defensive": ps.rebounds_defensive,
            "rebounds_total": ps.rebounds_total,
            "assists": ps.assists,
            "steals": ps.steals,
            "blocks": ps.blocks,
            "turnovers": ps.turnovers,
            "fouls_personal": ps.fouls_personal,
            "points": ps.points,
            "plus_minus": ps.plus_minus,
        })

    # Fetch betting lines
    betting_lines = None
    try:
        from sqlalchemy import text as sa_text
        bl_result = await db.execute(
            sa_text("""
                SELECT
                    opening_spread, opening_ou,
                    closing_spread, closing_ou,
                    closing_home_ml, closing_away_ml,
                    closing_spread_home_odds, closing_spread_away_odds,
                    closing_over_odds, closing_under_odds,
                    closing_home_implied_probability, closing_away_implied_probability
                FROM nba.betting_lines_consolidated
                WHERE game_id = :game_id
            """),
            {"game_id": game_id},
        )
        row = bl_result.one_or_none()
        if row:
            betting_lines = {
                "opening_spread": _get_val(row.opening_spread),
                "opening_ou": _get_val(row.opening_ou),
                "closing_spread": _get_val(row.closing_spread),
                "closing_ou": _get_val(row.closing_ou),
                "closing_home_ml": _get_val(row.closing_home_ml),
                "closing_away_ml": _get_val(row.closing_away_ml),
                "closing_spread_home_odds": _get_val(row.closing_spread_home_odds),
                "closing_spread_away_odds": _get_val(row.closing_spread_away_odds),
                "closing_over_odds": _get_val(row.closing_over_odds),
                "closing_under_odds": _get_val(row.closing_under_odds),
                "closing_home_implied_probability": _get_val(row.closing_home_implied_probability),
                "closing_away_implied_probability": _get_val(row.closing_away_implied_probability),
            }
    except Exception:
        pass

    return {
        "game_id": game.id,
        "nba_game_id": game.nba_game_id,
        "date": game.date.isoformat() if game.date else None,
        "status": game.status.value if game.status else "scheduled",
        "game_type": game.game_type,
        "venue": game.venue,
        "attendance": game.attendance,
        "home": home_stats,
        "away": away_stats,
        "players": player_stats_list,
        "betting_lines": betting_lines,
    }


@router.get("/nba/games")
async def nba_games(
    year: int = Query(...),
    date: str = Query(None),
    team_abbr: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    filters = ["s.year = :year"]
    params = {"year": year}

    if date:
        filters.append("(g.date AT TIME ZONE 'America/Chicago')::date = :date")
        params["date"] = datetime.date.fromisoformat(date)

    if team_abbr:
        filters.append("(ht.abbreviation = :team_abbr OR at.abbreviation = :team_abbr)")
        params["team_abbr"] = team_abbr.upper()

    where_clause = " AND ".join(filters)

    sql = f"""
    SELECT g.id, g.nba_game_id, g.date, g.game_type, g.home_score, g.away_score,
           g.status::text, g.venue, g.attendance,
           ht.abbreviation AS home_team, at.abbreviation AS away_team,
           blc.closing_spread AS spread, blc.closing_ou AS over_under,
           blc.closing_home_ml AS home_moneyline, blc.closing_away_ml AS away_moneyline
    FROM nba.games g
    JOIN nba.teams ht ON ht.id = g.home_team_id
    JOIN nba.teams at ON at.id = g.away_team_id
    JOIN nba.seasons s ON s.id = g.season_id
    LEFT JOIN nba.betting_lines_consolidated blc ON blc.game_id = g.id
    WHERE {where_clause}
    ORDER BY g.date ASC
    """
    result = await db.execute(text(sql), params)
    return [dict(r) for r in result.mappings().all()]


@router.get("/nba/games/nearest-date")
async def nba_nearest_date(
    year: int = Query(...),
    date: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    given_date = datetime.date.fromisoformat(date)

    # Try forward first
    forward_sql = """
    SELECT DISTINCT (g.date AT TIME ZONE 'America/Chicago')::date AS game_date
    FROM nba.games g
    JOIN nba.seasons s ON s.id = g.season_id
    WHERE s.year = :year
      AND (g.date AT TIME ZONE 'America/Chicago')::date > :date
      AND g.game_type IN ('REG', 'POST')
    ORDER BY game_date ASC
    LIMIT 1
    """
    result = await db.execute(text(forward_sql), {"year": year, "date": given_date})
    row = result.fetchone()
    if row:
        return {"date": row[0].isoformat(), "year": year}

    # Nothing forward, try backward (most recent past date)
    backward_sql = """
    SELECT DISTINCT (g.date AT TIME ZONE 'America/Chicago')::date AS game_date
    FROM nba.games g
    JOIN nba.seasons s ON s.id = g.season_id
    WHERE s.year = :year
      AND (g.date AT TIME ZONE 'America/Chicago')::date < :date
      AND g.game_type IN ('REG', 'POST')
    ORDER BY game_date DESC
    LIMIT 1
    """
    result = await db.execute(text(backward_sql), {"year": year, "date": given_date})
    row = result.fetchone()
    if row:
        return {"date": row[0].isoformat(), "year": year}

    return {"date": None, "year": None}


@router.get("/nba/players/{player_id}/profile")
async def nba_player_profile(player_id: int, db: AsyncSession = Depends(get_db)):
    """Return full NBA player profile: bio + season stats."""
    from datetime import datetime

    result = await db.execute(
        select(NBAPlayer).options(joinedload(NBAPlayer.team)).where(NBAPlayer.id == player_id)
    )
    player = result.unique().scalar_one_or_none()
    if not player:
        return None

    # Season stats
    r = await db.execute(
        select(NBAPlayerSeasonStats).where(
            NBAPlayerSeasonStats.player_id == player.id
        ).order_by(NBAPlayerSeasonStats.season_id.desc())
    )
    season_stats = r.scalars().all()

    recent_seasons = []
    for ss in season_stats:
        season_r = await db.execute(select(NBASeason).where(NBASeason.id == ss.season_id))
        season = season_r.scalar_one_or_none()
        year = season.year if season else 0
        team_abbr = ""
        if ss.team_id:
            tr = await db.execute(select(NBAPlayerSeasonStats.team_id).where(NBAPlayerSeasonStats.id == ss.id))
            t_result = await db.execute(
                select(NBAPlayerSeasonStats).where(NBAPlayerSeasonStats.id == ss.id)
            )
            from app.models.nba import NBATeam
            if ss.team_id:
                tr2 = await db.execute(select(NBATeam).where(NBATeam.id == ss.team_id))
                team = tr2.scalar_one_or_none()
                if team:
                    team_abbr = team.abbreviation

        recent_seasons.append({
            "year": year,
            "team_abbr": team_abbr,
            "games": ss.games_played,
            "games_started": ss.games_started,
            "minutes_played": ss.minutes_played,
            "points": ss.points,
            "points_per_game": ss.points_per_game,
            "field_goals_made": ss.field_goals_made,
            "field_goals_attempted": ss.field_goals_attempted,
            "field_goal_pct": ss.field_goal_pct,
            "three_points_made": ss.three_points_made,
            "three_points_attempted": ss.three_points_attempted,
            "three_point_pct": ss.three_point_pct,
            "free_throws_made": ss.free_throws_made,
            "free_throws_attempted": ss.free_throws_attempted,
            "free_throw_pct": ss.free_throw_pct,
            "rebounds": ss.rebounds,
            "offensive_rebounds": ss.offensive_rebounds,
            "defensive_rebounds": ss.defensive_rebounds,
            "rebounds_per_game": ss.rebounds_per_game,
            "assists": ss.assists,
            "assists_per_game": ss.assists_per_game,
            "steals": ss.steals,
            "blocks": ss.blocks,
            "turnovers": ss.turnovers,
            "personal_fouls": ss.personal_fouls,
            "fantasy_points": ss.fantasy_points,
        })

    # Career totals
    career = {
        "games": sum(ss.games_played for ss in season_stats if ss.games_played),
        "points": sum(ss.points for ss in season_stats if ss.points),
        "rebounds": sum(ss.rebounds for ss in season_stats if ss.rebounds),
        "assists": sum(ss.assists for ss in season_stats if ss.assists),
        "steals": sum(ss.steals for ss in season_stats if ss.steals),
        "blocks": sum(ss.blocks for ss in season_stats if ss.blocks),
        "first_year": min((s.year for s in season_stats if s.season_id), default=None) if False else None,
        "last_year": max((s.year for s in season_stats if s.season_id), default=None) if False else None,
    }
    if season_stats:
        years = []
        for ss in season_stats:
            sr = await db.execute(select(NBASeason).where(NBASeason.id == ss.season_id))
            s = sr.scalar_one_or_none()
            if s:
                years.append(s.year)
        if years:
            career["first_year"] = min(years)
            career["last_year"] = max(years)

    return {
        "id": player.id,
        "name": player.name,
        "position": player.position,
        "team_abbr": player.team.abbreviation if player.team else None,
        "team_name": player.team.name if player.team else None,
        "college": player.college,
        "height": player.height,
        "weight": player.weight,
        "birth_date": str(player.birth_date) if player.birth_date else None,
        "years_exp": player.years_exp,
        "status": player.status,
        "jersey_number": player.jersey_number,
        "headshot_url": player.headshot_url,
        "draft": None,
        "depth_chart": None,
        "stats": career,
        "recent_seasons": recent_seasons,
        "injuries": [],
        "transactions": [],
        "writeup": None,
    }
