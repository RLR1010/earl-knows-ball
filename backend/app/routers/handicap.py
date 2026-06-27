"""
Handicapping API endpoints: pick cards, matchup analysis.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models import Season, Game, Team, BettingLine, GamePrediction
from app.handicapping.nfl.engine import NFLHandicapper
from app.handicapping.nfl.situational import SituationalAnalyzer
from app.handicapping.nfl.splits import SplitAnalyzer

logger = logging.getLogger("earl.router.handicap")
router = APIRouter()

CURRENT_SEASON = datetime.now().year


async def _get_team_abbrevs(db: AsyncSession) -> dict[int, str]:
    """Return {team_id: abbreviation} lookup."""
    r = await db.execute(select(Team))
    return {t.id: t.abbreviation for t in r.scalars().all()}


@router.get("/handicapping/week/{year}/{week}")
async def get_week_picks(
    year: int,
    week: int,
    save_to_db: bool = Query(True, description="Save predictions to DB"),
    db: AsyncSession = Depends(get_db),
):
    """Produce pick cards for all games in a given week."""
    # Look up games for this week
    season_r = await db.execute(select(Season).where(Season.year == year))
    season = season_r.scalar_one_or_none()
    if not season:
        raise HTTPException(status_code=404, detail=f"Season {year} not found")

    games_r = await db.execute(
        select(Game).where(Game.season_id == season.id, Game.week == week).order_by(Game.date)
    )
    games = list(games_r.scalars().all())
    if not games:
        raise HTTPException(status_code=404, detail=f"Week {week} of {year} not found")

    team_abbrevs = await _get_team_abbrevs(db)
    game_ids = [g.id for g in games]

    handicapper = NFLHandicapper()
    results = await handicapper.handicap_games(
        game_ids=game_ids,
        year=year,
        save_to_db=save_to_db,
        source="api",
    )

    # Attach team abbreviations (+ enrich format for frontend)
    picks = []
    for r in results:
        if "error" in r:
            continue
        # Find the game for date/team info
        game = next((g for g in games if g.id == r.get("game_id")), None)
        if game:
            r["home_abbr"] = team_abbrevs.get(game.home_team_id, r.get("home_abbr", "?"))
            r["away_abbr"] = team_abbrevs.get(game.away_team_id, r.get("away_abbr", "?"))
            r["date"] = game.date.isoformat() if game.date else None
            r["week"] = game.week
            r["season"] = year
        picks.append(r)

    await db.commit()
    return {"season": year, "week": week, "picks": picks}


@router.get("/handicapping/matchup")
async def get_matchup_picks(
    home: str = Query(..., description="Home team abbreviation"),
    away: str = Query(..., description="Away team abbreviation"),
    year: int = Query(CURRENT_SEASON, description="Season year"),
    db: AsyncSession = Depends(get_db),
):
    """Produce a single pick card for a matchup between two teams."""
    team_abbrevs = await _get_team_abbrevs(db)

    home_id = next((tid for tid, a in team_abbrevs.items() if a.upper() == home.upper()), None)
    away_id = next((tid for tid, a in team_abbrevs.items() if a.upper() == away.upper()), None)
    if not home_id or not away_id:
        raise HTTPException(status_code=404, detail="Check team abbreviations")

    # Find the most recent game between these two teams
    season_r = await db.execute(select(Season).where(Season.year == year))
    season = season_r.scalar_one_or_none()
    if not season:
        raise HTTPException(status_code=404, detail=f"Season {year} not found")

    game_r = await db.execute(
        select(Game)
        .where(
            Game.season_id == season.id,
            ((Game.home_team_id == home_id) & (Game.away_team_id == away_id))
            | ((Game.home_team_id == away_id) & (Game.away_team_id == home_id)),
        )
        .order_by(Game.date.desc()).limit(1)
    )
    game = game_r.scalar_one_or_none()
    if not game:
        raise HTTPException(status_code=404, detail=f"No game found for {home} @ {away} in {year}")

    handicapper = NFLHandicapper()
    result = await handicapper.handicap_game(
        game_id=game.id,
        home_abbr=home.upper(),
        away_abbr=away.upper(),
        year=year,
        save_to_db=True,
        source="api",
    )

    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    result["home_abbr"] = home.upper()
    result["away_abbr"] = away.upper()
    result["date"] = game.date.isoformat() if game.date else None
    result["week"] = game.week
    result["season"] = year

    await db.commit()
    return result


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
    team: str = Query(None, description="Team abbreviation"),
    week: int = Query(None, description="Week number"),
    limit: int = Query(50, description="Max results"),
    db: AsyncSession = Depends(get_db),
):
    """Get historical game predictions from the database."""
    team_abbrevs = await _get_team_abbrevs(db)
    reverse_team = {a: i for i, a in team_abbrevs.items()}

    q = (
        select(GamePrediction)
        .options(joinedload(GamePrediction.game))
        .join(Game)
        .join(Season)
    )

    where_clauses = []
    if year:
        season_r = await db.execute(select(Season).where(Season.year == year))
        s = season_r.scalar_one_or_none()
        if s:
            where_clauses.append(Game.season_id == s.id)

    if team:
        tid = reverse_team.get(team.upper())
        if tid:
            where_clauses.append(
                (Game.home_team_id == tid) | (Game.away_team_id == tid)
            )

    if week:
        where_clauses.append(Game.week == week)

    if where_clauses:
        from sqlalchemy import and_
        q = q.where(and_(*where_clauses))

    q = q.order_by(Game.date.desc()).limit(limit)

    r = await db.execute(q)
    preds = r.unique().scalars().all()

    results = []
    for p in preds:
        game = p.game
        results.append({
            "game_id": p.game_id,
            "season": year or game.season.year if hasattr(game, 'season') else None,
            "week": game.week,
            "home_team": team_abbrevs.get(game.home_team_id, "?"),
            "away_team": team_abbrevs.get(game.away_team_id, "?"),
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
    conditions = []
    if year:
        season_r = await db.execute(select(Season).where(Season.year == year))
        s = season_r.scalar_one_or_none()
        if s:
            conditions.append(Game.season_id == s.id)

    from sqlalchemy import and_, func as f
    where = and_(*conditions) if conditions else True

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

    def _pct(win, tot):
        return round(win / tot * 100, 1) if tot else None

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
    pred_r = await db.execute(
        select(GamePrediction)
        .where(GamePrediction.game_id == game_id)
        .options(joinedload(GamePrediction.game))
    )
    row = pred_r.scalar_one_or_none()
    if not row:
        game_r = await db.execute(
            select(Game).where(Game.id == game_id)
        )
        game = game_r.scalar_one_or_none()
        if not game:
            raise HTTPException(status_code=404, detail="Game not found")
        return {"game_id": game_id, "prediction": None, "message": "No prediction run yet for this game"}

    game = row.game
    season_r = await db.execute(select(Season).where(Season.id == game.season_id))
    season = season_r.scalar_one_or_none()
    team_abbrevs = await _get_team_abbrevs(db)

    line_r = await db.execute(
        select(BettingLine)
        .where(BettingLine.game_id == game_id)
        .order_by(BettingLine.date.desc()).limit(1)
    )
    line = line_r.scalar_one_or_none()

    return {
        "game_id": game_id,
        "season": season.year if season else None,
        "week": game.week,
        "home_team": team_abbrevs.get(game.home_team_id, "?"),
        "away_team": team_abbrevs.get(game.away_team_id, "?"),
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
