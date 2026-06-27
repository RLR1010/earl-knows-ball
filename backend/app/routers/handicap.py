"""
Handicapping API endpoints: pick cards, matchup analysis.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import Season, Game, Team, BettingLine, GamePrediction
from app.handicapping.nfl.engine import NFLHandicapper

# CURRENT_SEASON used to live in engine.py; define it here for now
CURRENT_SEASON = 2026
from app.handicapping.nfl.situational import SituationalAnalyzer
from app.handicapping.nfl.splits import SplitAnalyzer

logger = logging.getLogger("earl.router.handicap")
router = APIRouter()


@router.get("/handicapping/week/{year}/{week}")
async def get_week_picks(
    year: int,
    week: int,
    num_games: Optional[int] = Query(None, description="Number of recent games to analyze per team (None = all)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Produce pick cards for all games in a given week.

    Returns spread picks, over/under picks, and moneyline recommendations
    with confidence scores and reasoning for every game.
    """
    handicapper = NFLHandicapper(db)
    picks = await handicapper.handicap_week(
        year=year,
        week=week,
        num_games_analysis=num_games,
    )
    if not picks:
        # Check if the season or week exists
        season = await db.execute(select(Season).where(Season.year == year))
        if not season.scalar_one_or_none():
            raise HTTPException(status_code=404, detail=f"Season {year} not found")
        game = await db.execute(
            select(Game).join(Season).where(
                Season.year == year, Game.week == week
            ).limit(1)
        )
        if not game.scalar_one_or_none():
            raise HTTPException(status_code=404, detail=f"Week {week} of {year} not found")
        # Stats not available (e.g., future week with no scores yet)
        return {"season": year, "week": week, "picks": [], "note": "No game results available yet for analysis"}

    # Commit any saved predictions
    await db.commit()
    return {"season": year, "week": week, "picks": picks}


@router.get("/handicapping/matchup")
async def get_matchup_picks(
    home: str = Query(..., description="Home team abbreviation"),
    away: str = Query(..., description="Away team abbreviation"),
    year: int = Query(CURRENT_SEASON, description="Season year"),
    num_games: Optional[int] = Query(None, description="Number of recent games to analyze per team (None = all)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Produce a single pick card for a hypothetical matchup.

    Unlike the week endpoint, this works without a specific Game row —
    just two team abbreviations and the season year.
    """
    handicapper = NFLHandicapper(db)
    pick = await handicapper.handicap_matchup(
        home_abbr=home,
        away_abbr=away,
        year=year,
        num_games=num_games,
    )
    if not pick:
        raise HTTPException(status_code=404, detail="Could not analyze matchup — check team abbreviations")

    await db.commit()
    return pick


@router.get("/handicapping/situational/game/{game_id}")
async def get_situational_game(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get situational handicapping info for a specific game."""
    analyzer = SituationalAnalyzer(db)
    info = await analyzer.get_situational_context_for_game(game_id)
    return {"game_id": game_id, "situational": info}


@router.get("/handicapping/situational/week/{year}/{week}")
async def get_situational_week(
    year: int,
    week: int,
    db: AsyncSession = Depends(get_db),
):
    """Get situational handicapping info for all games in a week."""
    analyzer = SituationalAnalyzer(db)
    info = await analyzer.get_week_situational(year, week)
    return {"season": year, "week": week, "situational": info}


@router.get("/handicapping/splits/game/{game_id}")
async def get_splits_game(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get betting splits / trends for a specific game."""
    analyzer = SplitAnalyzer(db)
    splits = await analyzer.get_game_splits(game_id)
    return {"game_id": game_id, "splits": splits}


@router.get("/handicapping/splits/week/{year}/{week}")
async def get_splits_week(
    year: int,
    week: int,
    db: AsyncSession = Depends(get_db),
):
    """Get betting splits / trends for all games in a week."""
    analyzer = SplitAnalyzer(db)
    splits = await analyzer.get_week_splits(year, week)
    return {"season": year, "week": week, "splits": splits}


@router.get("/handicapping/predictions")
async def get_predictions(
    year: int = Query(None, description="Season year"),
    team: str = Query(None, description="Team abbreviation (home or away)"),
    week: int = Query(None, description="Week number"),
    limit: int = Query(50, description="Max results"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get historical game predictions from the database.

    Returns predicted vs actual scores, ATS/OU/ML results.
    """
    from sqlalchemy import and_
    from sqlalchemy.orm import joinedload

    # Pre-load teams lookup
    team_r = await db.execute(select(Team))
    teams_by_id = {t.id: t.abbreviation for t in team_r.scalars().all()}

    # Build query with joins
    q = (
        select(GamePrediction)
        .options(joinedload(GamePrediction.game))
        .join(Game, Game.id == GamePrediction.game_id)
        .join(Season, Season.id == Game.season_id)
    )

    where_clauses = []
    if year:
        season_r = await db.execute(select(Season).where(Season.year == year))
        season = season_r.scalar_one_or_none()
        if season:
            where_clauses.append(Game.season_id == season.id)

    if team:
        t_id = teams_by_id.get(team.upper())
        if t_id:
            where_clauses.append(
                (Game.home_team_id == t_id) | (Game.away_team_id == t_id)
            )

    if week:
        where_clauses.append(Game.week == week)

    if where_clauses:
        q = q.where(and_(*where_clauses))

    q = q.order_by(Game.date.desc()).limit(limit)

    r = await db.execute(q)
    preds = r.unique().scalars().all()

    results = []
    for p in preds:
        game = p.game
        results.append({
            "game_id": p.game_id,
            "season": year or game.season.year,
            "week": game.week,
            "home_team": teams_by_id.get(game.home_team_id, "?"),
            "away_team": teams_by_id.get(game.away_team_id, "?"),
            "date": game.date.isoformat() if game.date else None,
            "predicted": {
                "home_score": p.predicted_home_score,
                "away_score": p.predicted_away_score,
                "total": p.predicted_total,
                "margin": p.predicted_margin,
            },
            "actual": {
                "home_score": p.actual_home_score,
                "away_score": p.actual_away_score,
                "total": p.actual_total,
                "margin": p.actual_margin,
            },
            "results": {
                "ats": p.ats_result,
                "ou": p.ou_result,
                "ml": p.ml_result,
            },
        })

    return {"predictions": results, "count": len(results)}


@router.get("/handicapping/predictions/stats")
async def get_prediction_stats(
    year: int = Query(None, description="Season year"),
    db: AsyncSession = Depends(get_db),
):
    """Get aggregate prediction accuracy stats."""
    from sqlalchemy import and_, func as f

    conditions = []
    if year:
        season_r = await db.execute(select(Season).where(Season.year == year))
        season = season_r.scalar_one_or_none()
        if season:
            conditions.append(Game.season_id == season.id)

    where = and_(*conditions) if conditions else True

    def _pct(win, total):
        return round(win / total * 100, 1) if total else None

    # Stats from game_predictions
    pg = await db.execute(
        select(
            f.count(GamePrediction.id).label("total"),
            f.sum(f.cast(GamePrediction.ats_result == "win", f.Integer)).label("ats_wins"),
            f.sum(f.cast(GamePrediction.ou_result == "win", f.Integer)).label("ou_wins"),
            f.sum(f.cast(GamePrediction.ml_result == "win", f.Integer)).label("ml_wins"),
        )
        .select_from(GamePrediction)
        .join(Game)
        .join(Season)
        .where(where)
    )
    stats = pg.one()

    total = stats.total or 0
    ats_w = stats.ats_wins or 0
    ou_w = stats.ou_wins or 0
    ml_w = stats.ml_wins or 0

    return {
        "year": year or "all",
        "total_games": total,
        "ats": {"wins": ats_w, "total": total, "pct": _pct(ats_w, total)},
        "over_under": {"wins": ou_w, "total": total, "pct": _pct(ou_w, total)},
        "moneyline": {"wins": ml_w, "total": total, "pct": _pct(ml_w, total)},
    }


@router.get("/handicapping/predictions/{game_id}")
async def get_game_prediction(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a single game's prediction with line info and confidence."""
    # Load GamePrediction with its Game
    pred = await db.execute(
        select(GamePrediction)
        .where(GamePrediction.game_id == game_id)
        .options(
            GamePrediction.game.and_(
                select(Game).where(Game.id == game_id)
            )
        )
    )
    row = pred.scalar_one_or_none()
    if not row:
        # Not in game_predictions yet; check if game exists
        game_r = await db.execute(
            select(Game, Season, Team)
            .join(Season)
            .outerjoin(Team, Game.id.isnot(None))
            .where(Game.id == game_id)
            .limit(1)
        )
        game = game_r.one_or_none()
        if not game:
            raise HTTPException(status_code=404, detail="Game not found")
        return {"game_id": game_id, "prediction": None, "message": "No prediction run yet for this game"}

    # Build response
    from app.models import _is_nfl
    from app.handicapping.confidence import confidence_score as _cal

    # Load game for extra info
    g = await db.execute(
        select(Game).where(Game.id == game_id)
    )
    game = g.scalar_one_or_none()

    s = await db.execute(
        select(Season).where(Season.id == game.season_id)
    )
    season = s.scalar_one_or_none()

    home_team_r = await db.execute(
        select(Team).where(Team.id == game.home_team_id)
    )
    away_team_r = await db.execute(
        select(Team).where(Team.id == game.away_team_id)
    )
    ht = home_team_r.scalar_one_or_none()
    at = away_team_r.scalar_one_or_none()

    home_abbr = ht.abbreviation if ht else "?"
    away_abbr = at.abbreviation if at else "?"

    # Load betting line
    line_r = await db.execute(
        select(BettingLine)
        .where(BettingLine.game_id == game_id)
        .order_by(BettingLine.date.desc()).limit(1)
    )
    line = line_r.scalar_one_or_none()

    return {
        "game_id": game_id,
        "season": season.year if season else "?",
        "week": game.week,
        "home_team": home_abbr,
        "away_team": away_abbr,
        "date": game.date.isoformat() if game.date else None,
        "predicted": {
            "home_score": row.predicted_home_score,
            "away_score": row.predicted_away_score,
            "total": row.predicted_total,
            "margin": row.predicted_margin,
        },
        "actual": {
            "home_score": row.actual_home_score,
            "away_score": row.actual_away_score,
            "total": row.actual_total,
            "margin": row.actual_margin,
        },
        "results": {
            "ats": row.ats_result,
            "ou": row.ou_result,
            "ml": row.ml_result,
        },
        "line": {
            "spread": float(line.spread) if line and line.spread else None,
            "over_under": float(line.over_under) if line and line.over_under else None,
            "home_moneyline": line.home_moneyline if line else None,
            "away_moneyline": line.away_moneyline if line else None,
        },
    }
