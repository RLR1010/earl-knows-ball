from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import Team, DepthChart

router = APIRouter()


@router.get("/teams")
async def list_teams(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Team).order_by(Team.name))
    teams = result.scalars().all()
    return teams


@router.get("/teams/by-abbr/{abbreviation}")
async def get_team_by_abbr(abbreviation: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Team).where(Team.abbreviation == abbreviation.upper())
    )
    team = result.scalar_one_or_none()
    return team


@router.get("/teams/{team_id}")
async def get_team(team_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Team).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    return team


@router.get("/teams/{team_id}/depth-chart")
async def get_team_depth_chart(
    team_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DepthChart)
        .where(DepthChart.team_id == team_id)
        .order_by(DepthChart.position, DepthChart.slot)
    )
    entries = result.scalars().all()
    return entries
