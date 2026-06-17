from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import Player, Team, DepthChart, Injury, Transaction, Season, PlayerWeeklyStats
from pydantic import BaseModel
from sqlalchemy import func

router = APIRouter()


class PlayerOut(BaseModel):
    id: int
    name: str
    position: str
    team_abbr: str | None = None
    team_name: str | None = None
    status: str | None = None
    jersey_number: int | None = None
    height: int | None = None
    weight: int | None = None
    college: str | None = None
    years_exp: int | None = None
    headshot_url: str | None = None

    model_config = {"from_attributes": True}


async def _player_to_out(player: Player) -> PlayerOut:
    return PlayerOut(
        id=player.id,
        name=player.name,
        position=player.position,
        status=player.status,
        jersey_number=player.jersey_number,
        height=player.height,
        weight=player.weight,
        college=player.college,
        years_exp=player.years_exp,
        headshot_url=player.headshot_url,
        team_abbr=player.team.abbreviation if player.team else None,
        team_name=player.team.name if player.team else None,
    )


@router.get("/players")
async def list_players(
    position: str | None = Query(None),
    team_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    query = select(Player).options(joinedload(Player.team))
    if position:
        query = query.where(Player.position == position.upper())
    if team_id:
        query = query.where(Player.team_id == team_id)
    query = query.order_by(Player.name)
    result = await db.execute(query)
    players = result.unique().scalars().all()
    return [await _player_to_out(p) for p in players]


@router.get("/players/{player_id}")
async def get_player(player_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Player).options(joinedload(Player.team)).where(Player.id == player_id)
    )
    player = result.unique().scalar_one_or_none()
    if not player:
        return None
    return await _player_to_out(player)


@router.get("/players/{player_id}/profile")
async def get_player_profile(player_id: int, db: AsyncSession = Depends(get_db)):
    """Return full player profile data: stats, draft, injuries, transactions, depth chart."""
    from datetime import datetime

    result = await db.execute(
        select(Player).options(joinedload(Player.team)).where(Player.id == player_id)
    )
    player = result.unique().scalar_one_or_none()
    if not player:
        return None

    profile = {
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
        "stats": None,
        "recent_seasons": [],
        "injuries": [],
        "transactions": [],
    }

    # Draft
    if player.draft_year:
        profile["draft"] = {
            "year": player.draft_year,
            "round": player.draft_round,
            "pick": player.draft_pick,
            "team": player.draft_team,
        }

    # Depth chart
    r = await db.execute(
        select(DepthChart).where(DepthChart.player_id == player.id).order_by(DepthChart.slot).limit(1)
    )
    dc = r.scalar_one_or_none()
    if dc:
        profile["depth_chart"] = {
            "position": dc.position,
            "slot": dc.slot,
            "team_id": dc.team_id,
            "status": dc.status,
        }

    # Career stats
    r = await db.execute(
        select(
            func.count(PlayerWeeklyStats.id).label("games"),
            func.sum(PlayerWeeklyStats.pass_yards).label("pass_yds"),
            func.sum(PlayerWeeklyStats.pass_tds).label("pass_tds"),
            func.sum(PlayerWeeklyStats.pass_int).label("pass_int"),
            func.sum(PlayerWeeklyStats.rush_yards).label("rush_yds"),
            func.sum(PlayerWeeklyStats.rush_tds).label("rush_tds"),
            func.sum(PlayerWeeklyStats.receptions).label("rec"),
            func.sum(PlayerWeeklyStats.receiving_yards).label("rec_yds"),
            func.sum(PlayerWeeklyStats.receiving_tds).label("rec_tds"),
            func.sum(PlayerWeeklyStats.fantasy_points_ppr).label("fpts"),
            func.min(Season.year).label("first_year"),
            func.max(Season.year).label("last_year"),
        )
        .join(Season, PlayerWeeklyStats.season_id == Season.id)
        .where(PlayerWeeklyStats.player_id == player.id)
    )
    s = r.one()
    if s.games and s.games > 0:
        profile["stats"] = {
            "games": s.games,
            "first_year": s.first_year,
            "last_year": s.last_year,
            "pass_yds": int(s.pass_yds or 0),
            "pass_tds": int(s.pass_tds or 0),
            "pass_int": int(s.pass_int or 0),
            "rush_yds": int(s.rush_yds or 0),
            "rush_tds": int(s.rush_tds or 0),
            "rec": int(s.rec or 0),
            "rec_yds": int(s.rec_yds or 0),
            "rec_tds": int(s.rec_tds or 0),
            "fantasy_ppr": float(s.fpts or 0),
        }

        # Recent seasons (last 3)
        recent_years = sorted(set([s.last_year, s.last_year - 1, s.last_year - 2]), reverse=True) if s.last_year else []
        for yr in recent_years:
            if yr is None:
                continue
            r_sid = await db.execute(select(Season.id).where(Season.year == yr))
            sid = r_sid.scalar_one_or_none()
            if not sid:
                continue
            r2 = await db.execute(
                select(
                    func.count(PlayerWeeklyStats.id).label("gp"),
                    func.sum(PlayerWeeklyStats.pass_yards).label("pyd"),
                    func.sum(PlayerWeeklyStats.pass_tds).label("ptd"),
                    func.sum(PlayerWeeklyStats.pass_int).label("pint"),
                    func.sum(PlayerWeeklyStats.rush_yards).label("ryd"),
                    func.sum(PlayerWeeklyStats.rush_tds).label("rtd"),
                    func.sum(PlayerWeeklyStats.receptions).label("rec"),
                    func.sum(PlayerWeeklyStats.receiving_yards).label("recy"),
                    func.sum(PlayerWeeklyStats.receiving_tds).label("rectd"),
                    func.sum(PlayerWeeklyStats.fantasy_points_ppr).label("fpts"),
                ).where(
                    PlayerWeeklyStats.player_id == player.id,
                    PlayerWeeklyStats.season_id == sid,
                )
            )
            s2 = r2.one()
            if s2.gp and s2.gp > 0:
                profile["recent_seasons"].append({
                    "year": yr,
                    "games": s2.gp,
                    "pass_yds": int(s2.pyd or 0),
                    "pass_tds": int(s2.ptd or 0),
                    "pass_int": int(s2.pint or 0),
                    "rush_yds": int(s2.ryd or 0),
                    "rush_tds": int(s2.rtd or 0),
                    "rec": int(s2.rec or 0),
                    "rec_yds": int(s2.recy or 0),
                    "rec_tds": int(s2.rectd or 0),
                    "fantasy_ppr": float(s2.fpts or 0),
                })

    # Injury history
    r = await db.execute(
        select(Injury).where(Injury.player_id == player.id)
        .order_by(Injury.season_id.desc(), Injury.week.desc())
        .limit(10)
    )
    for inj in r.scalars().all():
        yr_r = await db.execute(select(Season.year).where(Season.id == inj.season_id))
        yr = yr_r.scalar_one_or_none()
        profile["injuries"].append({
            "week": inj.week,
            "year": yr,
            "injury": inj.injury_type,
            "status": inj.game_status or inj.practice_status or "Unknown",
        })

    # Transactions
    r = await db.execute(
        select(Transaction).where(Transaction.player_id == player.id)
        .order_by(Transaction.transaction_date.desc()).limit(10)
    )
    for t in r.scalars().all():
        profile["transactions"].append({
            "date": str(t.transaction_date.date()),
            "type": t.transaction_type,
            "details": t.details or "",
        })

    # Narrative write-up — cached in DB to avoid DeepSeek calls on every page load
    if player.profile_writeup:
        profile["writeup"] = player.profile_writeup
    else:
        from app.ingestion.player_profiles import build_profile as _build_profile
        try:
            writeup = await _build_profile(db, player)
            # Cache in DB
            player.profile_writeup = writeup
            await db.flush()
            await db.commit()
            profile["writeup"] = writeup
        except Exception as e:
            print(f"[Earl] Write-up generation failed for {player.name}: {e}")
            profile["writeup"] = None

    return profile


@router.get("/players/search/{name}")
async def search_players(name: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Player)
        .options(joinedload(Player.team))
        .where(Player.name.ilike(f"%{name}%"))
        .order_by(Player.name)
    )
    players = result.unique().scalars().all()
    return [await _player_to_out(p) for p in players]
