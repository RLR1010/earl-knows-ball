"""
Handicapping API endpoints: pick cards, matchup analysis, team stats.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import Season, Game, Team, BettingLine, GamePrediction
from app.handicapping.nfl.engine import Handicapper, TeamStatsBuilder, CURRENT_SEASON, backtest_season
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
    handicapper = Handicapper(db)
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
    week: int = Query(1, description="Week number"),
    num_games: int = Query(5, description="Number of recent games to analyze"),
    db: AsyncSession = Depends(get_db),
):
    """
    Analyze a specific matchup (home team @ away team) for a given week.

    Used by Earl's chat to provide quick matchup analysis.
    """
    # Validate teams
    home_r = await db.execute(select(Team).where(Team.abbreviation == home.upper()))
    away_r = await db.execute(select(Team).where(Team.abbreviation == away.upper()))
    if not home_r.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Team '{home}' not found")
    if not away_r.scalar_one_or_none():
        raise HTTPException(status_code=404, detail=f"Team '{away}' not found")

    handicapper = Handicapper(db)
    card = await handicapper.analyze_matchup(
        home_team_abbr=home.upper(),
        away_team_abbr=away.upper(),
        year=year,
        week=week,
        num_games_analysis=num_games,
    )
    if not card:
        raise HTTPException(
            status_code=404,
            detail=f"No matchup found for {away} @ {home} in {year} week {week}",
        )
    return card


@router.get("/handicapping/team-stats/{year}/{team}")
async def get_team_stats(
    year: int,
    team: str,
    num_games: Optional[int] = Query(None, description="Recent N games (None = all)"),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed team statistics for handicapping analysis."""
    team_r = await db.execute(select(Team).where(Team.abbreviation == team.upper()))
    t = team_r.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail=f"Team '{team}' not found")

    builder = TeamStatsBuilder(db)
    all_stats = await builder.build(year, num_games=num_games)
    stats = all_stats.get(team.upper())
    if not stats:
        raise HTTPException(
            status_code=404,
            detail=f"No stats for {team} in {year}",
        )
    return stats.to_dict()


@router.get("/handicapping/ats-standings/{year}")
async def get_ats_standings(
    year: int,
    num_games: Optional[int] = Query(None, description="Recent N games"),
    min_games: int = Query(4, description="Minimum games for ATS qualification"),
    db: AsyncSession = Depends(get_db),
):
    """
    Get ATS standings for all teams in a season.

    Shows which teams are covering the spread, hitting overs/unders,
    and their straight-up records.
    """
    builder = TeamStatsBuilder(db)
    all_stats = await builder.build(year, num_games=num_games)

    standings = []
    for abbr, stats in all_stats.items():
        if stats.games >= min_games:
            standings.append(stats.to_dict())

    standings.sort(key=lambda x: (x.get("ats_pct") or 0), reverse=True)

    return {
        "year": year,
        "num_games": num_games or "all",
        "standings": standings,
    }


@router.get("/handicapping/situational/game/{game_id}")
async def get_game_situation(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get situational factors for a single game (rest, travel, division, venue)."""
    analyzer = SituationalAnalyzer(db)
    ctx = await analyzer.analyze_game(game_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=f"Game {game_id} not found")
    return ctx.to_dict()


@router.get("/handicapping/situational/week/{year}/{week}")
async def get_week_situations(
    year: int,
    week: int,
    db: AsyncSession = Depends(get_db),
):
    """Get situational factors for all games in a week."""
    analyzer = SituationalAnalyzer(db)
    results = await analyzer.analyze_week(year, week)
    return {"season": year, "week": week, "games": [r.to_dict() for r in results]}


@router.get("/handicapping/splits/game/{game_id}")
async def get_game_splits(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get betting splits for a single game (line movement + implied public %)."""
    analyzer = SplitAnalyzer(db)
    split = await analyzer.analyze_game(game_id)
    if not split:
        raise HTTPException(status_code=404, detail=f"No betting data for game {game_id}")
    return split.to_dict()


@router.get("/handicapping/splits/week/{year}/{week}")
async def get_week_splits(
    year: int,
    week: int,
    db: AsyncSession = Depends(get_db),
):
    """Get betting splits for all games in a week."""
    analyzer = SplitAnalyzer(db)
    results = await analyzer.analyze_week(year, week)
    return {"season": year, "week": week, "games": [r.to_dict() for r in results]}


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


@router.get("/handicapping/backtest/{year}")
async def run_backtest(
    year: int,
    start_week: int = Query(1, description="First week to test (default: 1, includes all weeks)"),
    end_week: Optional[int] = Query(None, description="Last week to test (default: season's last)"),
    num_games: Optional[int] = Query(3, description="Number of recent games to use per team for stats"),
    db: AsyncSession = Depends(get_db),
):
    """
    Run a week-by-week backtest of the handicapping engine.

    For each week N, builds team stats ONLY from games before week N
    (simulating what you'd know before kickoff), then compares predictions
    against actual results.

    Returns ATS, O/U, and moneyline accuracy across the season.
    """
    result = await backtest_season(
        db=db,
        year=year,
        end_week=end_week,
        num_games=num_games,
    )
    return result


@router.get("/handicapping/mlb/backtest/{year}")
async def run_mlb_backtest(
    year: int,
    resume: bool = Query(True, description="Skip games already in game_predictions"),
):
    """
    Run MLB season backtest in the background.
    Trains all three XGBoost models on a 5-year lookback, then evaluates
    predictions against every completed game. When resume=True, skips games
    already in game_predictions so you can pick up where you left off.
    """
    import asyncio
    from app.database import async_session
    from app.handicapping.mlb.mlb_engine import backtest_season

    async def _run():
        async with async_session() as db:
            result = await backtest_season(db, year=year, resume=resume, num_games=10)
            logger = __import__('logging').getLogger('earl.mlb_backtest')
            logger.info(f"MLB backtest {year} complete: RL={result.get('run_line',{}).get('pct')}, OU={result.get('over_under',{}).get('pct')}, ML={result.get('moneyline',{}).get('pct')}")
            return result

    asyncio.create_task(_run())
    return {"status": "started", "message": f"MLB backtest {year} running in background (resume={resume}). Check API logs for progress."}


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
        return round(win / max(total, 1) * 100, 1) if total > 0 else 0

    async def _count(col):
        q = (
            select(col, f.count())
            .select_from(GamePrediction)
            .join(Game, Game.id == GamePrediction.game_id)
            .where(where)
            .group_by(col)
        )
        return {r[0]: r[1] for r in (await db.execute(q)).fetchall() if r[0]}

    ats = await _count(GamePrediction.ats_result)
    ou = await _count(GamePrediction.ou_result)
    ml = await _count(GamePrediction.ml_result)

    ats_w = ats.get("Win", 0); ats_l = ats.get("Loss", 0); ats_p = ats.get("Push", 0)
    ou_w = ou.get("Win", 0); ou_l = ou.get("Loss", 0); ou_p = ou.get("Push", 0)
    ml_w = ml.get("Win", 0); ml_l = ml.get("Loss", 0)

    return {
        "ats": {"wins": ats_w, "losses": ats_l, "pushes": ats_p, "pct": _pct(ats_w, ats_w + ats_l)},
        "ou":  {"wins": ou_w,  "losses": ou_l,  "pushes": ou_p,  "pct": _pct(ou_w,  ou_w + ou_l)},
        "ml":  {"wins": ml_w,  "losses": ml_l,  "pct": _pct(ml_w,  ml_w + ml_l)},
    }


@router.get("/handicapping/predictions/{game_id}")
async def get_game_prediction(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a single game prediction (public, no auth)."""
    from sqlalchemy import text as _t
    r = await db.execute(_t("""
        SELECT gp.predicted_home_score, gp.predicted_away_score, gp.predicted_total,
               gp.predicted_margin, gp.actual_home_score, gp.actual_away_score,
               gp.actual_total, gp.actual_margin, gp.ats_result, gp.ou_result, gp.ml_result,
               gp.margin_conf, gp.ou_conf,
               g.week, g.date, ht.abbreviation as ha, at.abbreviation as aa, s.year as season,
               bl.spread, bl.over_under
        FROM nfl.game_predictions gp
        JOIN nfl.games g ON g.id = gp.game_id
        JOIN nfl.seasons s ON s.id = g.season_id
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at ON at.id = g.away_team_id
        LEFT JOIN LATERAL (
            SELECT bl2.spread, bl2.over_under
            FROM nfl.betting_lines bl2
            WHERE bl2.game_id = g.id
            ORDER BY
                CASE bl2.source
                    WHEN 'nflverse' THEN 1
                    WHEN 'sbr_closing' THEN 2
                    WHEN 'the_odds_api' THEN 3
                    WHEN 'the_odds_api_opening' THEN 4
                    ELSE 5
                END,
                bl2.recorded_at DESC
            LIMIT 1
        ) bl ON TRUE
        WHERE gp.game_id = :gid AND gp.source = 'api'
        LIMIT 1
    """), {"gid": game_id})
    row = r.fetchone()
    if not row:
        return {"error": "No prediction found", "game_id": game_id}

    # Calibrate confidence — maps raw stored value to empirical accuracy
    try:
        from app.handicapping.calibrate_confidence import calibrate as _cal
        raw_mc = float(row.margin_conf) if row.margin_conf else None
        raw_ou = float(row.ou_conf) if row.ou_conf else None
        cal_overall = _cal(raw_mc, "overall") if raw_mc else None
        cal_ats = _cal(raw_mc, "ats") if raw_mc else None
        cal_ou = _cal(raw_ou or raw_mc, "ou") if (raw_ou or raw_mc) else None
        cal_ml = _cal(raw_mc, "ml") if raw_mc else None
    except ImportError:
        cal_overall = float(row.margin_conf) if row.margin_conf else None
        cal_ats = None
        cal_ou = float(row.ou_conf) if row.ou_conf else None
        cal_ml = None

    return {
        "game_id": game_id,
        "season": row.season, "week": row.week,
        "home_team": row.ha, "away_team": row.aa,
        "date": str(row.date) if row.date else None,
        "predicted": {"home_score": row.predicted_home_score, "away_score": row.predicted_away_score, "total": row.predicted_total, "margin": row.predicted_margin},
        "actual": {"home_score": row.actual_home_score, "away_score": row.actual_away_score, "total": row.actual_total, "margin": row.actual_margin},
        "results": {"ats": row.ats_result, "ou": row.ou_result, "ml": row.ml_result},
        "confidence": {
            "overall": cal_overall,
            "ats": cal_ats,
            "ou": cal_ou,
            "ml": cal_ml,
        },
        "line": {"spread": float(row.spread) if row.spread else None, "over_under": float(row.over_under) if row.over_under else None},
    }
