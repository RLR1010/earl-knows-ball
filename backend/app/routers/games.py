from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, text
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import Game, Season, Team, PlayerWeeklyStats, Player, BettingLine
from pydantic import BaseModel

router = APIRouter()


class GameOut(BaseModel):
    id: int
    week: int
    game_type: str
    status: str
    date: str
    venue: str | None = None
    roof_type: str | None = None
    surface: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    home_score: int | None = None
    away_score: int | None = None
    home_team_id: int | None = None
    away_team_id: int | None = None
    spread: float | None = None          # from home team perspective (+ = home underdog, - = home favorite)
    over_under: float | None = None

    model_config = {"from_attributes": True}


class BoxScorePlayer(BaseModel):
    player_id: int
    player_name: str
    position: str
    team_abbr: str | None = None
    # Passing
    pass_attempts: int = 0
    pass_completions: int = 0
    pass_yards: float = 0.0
    pass_tds: int = 0
    pass_int: int = 0
    # Rushing
    rush_attempts: int = 0
    rush_yards: float = 0.0
    rush_tds: int = 0
    # Receiving
    targets: int = 0
    receptions: int = 0
    receiving_yards: float = 0.0
    receiving_tds: int = 0
    # Kicking
    field_goals_made: int = 0
    field_goals_attempted: int = 0
    extra_points_made: int = 0
    # Defense
    tackles: float = 0.0
    sacks: float = 0.0
    interceptions: int = 0
    fumbles_recovered: int = 0
    defensive_tds: int = 0


class BoxScoreStats(BaseModel):
    total_yards: float = 0.0
    pass_yards: float = 0.0
    rush_yards: float = 0.0
    turnovers: int = 0
    first_downs: int = 0
    third_down_pct: float | None = None
    time_of_possession: str | None = None
    penalties: int = 0
    penalty_yards: int = 0
    top_players: list[BoxScorePlayer] = []


class BoxScoreOut(BaseModel):
    game: GameOut
    home_stats: BoxScoreStats
    away_stats: BoxScoreStats


async def _game_to_out(game: Game, spread: float | None = None, over_under: float | None = None) -> GameOut:
    return GameOut(
        id=game.id,
        week=game.week,
        game_type=game.game_type,
        status=game.status.value if game.status else "scheduled",
        date=game.date.isoformat() if game.date else "",
        venue=game.venue,
        roof_type=game.roof_type,
        surface=game.surface,
        home_team=game.home_team.abbreviation if game.home_team else None,
        away_team=game.away_team.abbreviation if game.away_team else None,
        home_score=game.home_score,
        away_score=game.away_score,
        home_team_id=game.home_team_id,
        away_team_id=game.away_team_id,
        spread=spread,
        over_under=over_under,
    )


@router.get("/seasons")
async def list_seasons(db: AsyncSession = Depends(get_db)):
    """Return years that have game data in the database."""
    result = await db.execute(
        select(Season.year)
        .where(Season.id.in_(select(Game.season_id).distinct()))
        .order_by(Season.year.desc())
    )
    years = [row[0] for row in result.all()]
    return years


@router.get("/games")
async def list_games(
    season_year: int | None = Query(None),
    week: int | None = Query(None),
    team_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(Game)
        .options(joinedload(Game.home_team), joinedload(Game.away_team))
    )

    if season_year:
        season_sub = select(Season.id).where(Season.year == season_year).scalar_subquery()
        query = query.where(Game.season_id.in_(season_sub))
    if week:
        query = query.where(Game.week == week)
    if team_id:
        query = query.where(
            (Game.home_team_id == team_id) | (Game.away_team_id == team_id)
        )

    query = query.order_by(Game.date)
    result = await db.execute(query)
    games = result.unique().scalars().all()

    # Bulk fetch consolidated betting lines (one row per game)
    if games:
        game_ids = [g.id for g in games]
        sql = text("""
            SELECT game_id, closing_spread, closing_ou
            FROM nfl.betting_lines_consolidated
            WHERE game_id = ANY(:game_ids)
        """)
        line_result = await db.execute(sql, {"game_ids": game_ids})
        latest_lines = {}
        for row in line_result:
            latest_lines[row.game_id] = {
                "spread": float(row.closing_spread) if row.closing_spread is not None else None,
                "over_under": float(row.closing_ou) if row.closing_ou is not None else None,
            }
    else:
        latest_lines = {}

    return [
        await _game_to_out(g, latest_lines.get(g.id, {}).get("spread"), latest_lines.get(g.id, {}).get("over_under"))
        for g in games
    ]


async def _build_team_box_stats(
    db: AsyncSession,
    game: Game,
    team_id: int,
    team_abbr: str,
) -> BoxScoreStats:
    """Aggregate team-level stats and get top players for a game."""
    # Get player stats for this team in this game
    stats_result = await db.execute(
        select(PlayerWeeklyStats)
        .options(joinedload(PlayerWeeklyStats.player))
        .where(
            PlayerWeeklyStats.game_id == game.id,
            PlayerWeeklyStats.team_id == team_id,
        )
    )
    rows = stats_result.unique().scalars().all()

    if not rows:
        return BoxScoreStats()

    # Aggregate team totals
    total_yards = sum((r.pass_yards or 0) + (r.rush_yards or 0) for r in rows)
    pass_yards = sum(r.pass_yards or 0 for r in rows)
    rush_yards = sum(r.rush_yards or 0 for r in rows)
    turnovers = sum((r.pass_int or 0) + (r.fumbles_lost or 0) for r in rows)

    # Build top players list (group by player, different positions contribute different stats)
    player_map: dict[int, dict] = {}
    for r in rows:
        pid = r.player_id
        if pid not in player_map:
            pname = r.player.name if r.player else "Unknown"
            ppos = r.player.position if r.player else ""
            player_map[pid] = {
                "player_id": pid,
                "player_name": pname,
                "position": ppos,
                "team_abbr": team_abbr,
                "pass_attempts": 0,
                "pass_completions": 0,
                "pass_yards": 0.0,
                "pass_tds": 0,
                "pass_int": 0,
                "rush_attempts": 0,
                "rush_yards": 0.0,
                "rush_tds": 0,
                "targets": 0,
                "receptions": 0,
                "receiving_yards": 0.0,
                "receiving_tds": 0,
                "field_goals_made": 0,
                "field_goals_attempted": 0,
                "extra_points_made": 0,
                "tackles": 0.0,
                "sacks": 0.0,
                "interceptions": 0,
                "fumbles_recovered": 0,
                "defensive_tds": 0,
            }
        p = player_map[pid]
        p["pass_attempts"] += r.pass_attempts or 0
        p["pass_completions"] += r.pass_completions or 0
        p["pass_yards"] += r.pass_yards or 0
        p["pass_tds"] += r.pass_tds or 0
        p["pass_int"] += r.pass_int or 0
        p["rush_attempts"] += r.rush_attempts or 0
        p["rush_yards"] += r.rush_yards or 0
        p["rush_tds"] += r.rush_tds or 0
        p["targets"] += r.targets or 0
        p["receptions"] += r.receptions or 0
        p["receiving_yards"] += r.receiving_yards or 0
        p["receiving_tds"] += r.receiving_tds or 0
        p["field_goals_made"] += r.field_goals_made or 0
        p["field_goals_attempted"] += r.field_goals_attempted or 0
        p["extra_points_made"] += r.extra_points_made or 0
        p["sacks"] += r.sacks or 0
        p["interceptions"] += r.interceptions or 0
        p["fumbles_recovered"] += r.fumbles_recovered or 0
        p["defensive_tds"] += r.defensive_tds or 0

    # Sort top players by total yards + TDs contribution
    all_players = list(player_map.values())
    all_players.sort(
        key=lambda p: (p["pass_yards"] + p["rush_yards"] + p["receiving_yards"]) + (p["pass_tds"] + p["rush_tds"] + p["receiving_tds"]) * 10,
        reverse=True,
    )

    return BoxScoreStats(
        total_yards=round(total_yards, 1),
        pass_yards=round(pass_yards, 1),
        rush_yards=round(rush_yards, 1),
        turnovers=turnovers,
        top_players=[BoxScorePlayer(**p) for p in all_players[:8]],
    )


@router.get("/games/{game_id}/box-score")
async def get_game_box_score(game_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Game)
        .options(joinedload(Game.home_team), joinedload(Game.away_team))
        .where(Game.id == game_id)
    )
    game = result.unique().scalar_one_or_none()
    if not game:
        return None

    game_out = await _game_to_out(game)

    home_stats = await _build_team_box_stats(
        db, game, game.home_team_id, game_out.home_team or ""
    )
    away_stats = await _build_team_box_stats(
        db, game, game.away_team_id, game_out.away_team or ""
    )

    return BoxScoreOut(game=game_out, home_stats=home_stats, away_stats=away_stats)


@router.get("/games/{game_id}")
async def get_game(game_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Game)
        .options(joinedload(Game.home_team), joinedload(Game.away_team))
        .where(Game.id == game_id)
    )
    game = result.unique().scalar_one_or_none()
    if not game:
        return None
    # Get best betting line (priority: nflverse > sbr_closing > the_odds_api > the_odds_api_opening > sbr_opening)
    line_result = await db.execute(
        text("""
            SELECT spread, over_under
            FROM nfl.betting_lines
            WHERE game_id = :game_id
            ORDER BY
                CASE source
                    WHEN 'nflverse' THEN 1
                    WHEN 'sbr_closing' THEN 2
                    WHEN 'the_odds_api' THEN 3
                    WHEN 'the_odds_api_opening' THEN 4
                    ELSE 5
                END,
                recorded_at DESC
            LIMIT 1
        """),
        {"game_id": game_id},
    )
    bl = line_result.fetchone()
    return await _game_to_out(game, float(bl.spread) if bl and bl.spread is not None else None, float(bl.over_under) if bl and bl.over_under is not None else None)
