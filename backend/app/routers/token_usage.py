"""Token usage tracking endpoints."""

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.token_usage import UserTokenUsage
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users/me", tags=["token-usage"])


class TokenUsageResponse(BaseModel):
    """Current month's token usage and limit."""

    month: str
    tokens_used: int
    token_limit: Optional[int] = None  # None = unlimited
    percent_used: Optional[float] = None


@router.get("/token-usage", response_model=TokenUsageResponse)
async def get_token_usage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current month's token usage and the user's monthly limit."""
    first_of_month = date.today().replace(day=1)

    result = await db.execute(
        select(UserTokenUsage).where(
            UserTokenUsage.user_id == user.id,
            UserTokenUsage.month == first_of_month,
        )
    )
    usage = result.scalar_one_or_none()

    tokens_used = usage.tokens_used if usage else 0
    token_limit = user.monthly_token_limit

    percent_used = None
    if token_limit and token_limit > 0:
        percent_used = round((tokens_used / token_limit) * 100, 1)

    return TokenUsageResponse(
        month=first_of_month.isoformat(),
        tokens_used=tokens_used,
        token_limit=token_limit,
        percent_used=percent_used,
    )


# ── Admin endpoints ──────────────────────────────────────────────────────

admin_router = APIRouter(prefix="/api/admin", tags=["admin-token-usage"])


class TokenLimitUpdate(BaseModel):
    monthly_token_limit: Optional[int] = None  # null = no limit


@admin_router.put("/users/{user_id}/token-limit")
async def set_user_token_limit(
    user_id: str,
    body: TokenLimitUpdate,
    admin_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only: set a user's monthly token limit."""
    # Verify admin
    if not admin_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    # Find the user
    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    target_user.monthly_token_limit = body.monthly_token_limit
    await db.commit()

    return {
        "ok": True,
        "user_id": user_id,
        "monthly_token_limit": body.monthly_token_limit,
    }


@admin_router.get("/users/{user_id}/token-usage")
async def get_user_token_usage(
    user_id: str,
    month: Optional[str] = Query(None, description="ISO date string for month start (defaults to current month)"),
    admin_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only: get a specific user's token usage for a given month."""
    if not admin_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    if month:
        try:
            month_date = date.fromisoformat(month)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid month format (use YYYY-MM-DD)")
    else:
        month_date = date.today().replace(day=1)

    result = await db.execute(
        select(UserTokenUsage).where(
            UserTokenUsage.user_id == user_id,
            UserTokenUsage.month == month_date,
        )
    )
    usage = result.scalar_one_or_none()

    return {
        "user_id": user_id,
        "month": month_date.isoformat(),
        "tokens_used": usage.tokens_used if usage else 0,
    }
