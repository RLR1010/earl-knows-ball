"""Shared utility for checking token limits and saving usage to the database."""

import logging
from datetime import date

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.token_usage import UserTokenUsage
from app.models.user import User
from app.models.admin import UserSubscription

logger = logging.getLogger(__name__)


async def _get_token_period_start(user: User, db: AsyncSession) -> date:
    """Get the start date of the current token usage period for a user.

    Uses the subscription's current_period_start to determine the billing
    cycle anchor.  Falls back to the 1st of the calendar month for users
    without an active subscription.
    """
    result = await db.execute(
        select(UserSubscription.current_period_start)
        .where(
            UserSubscription.user_id == user.id,
            UserSubscription.status.in_(["active", "trialing"]),
        )
        .order_by(UserSubscription.created_at.asc())
        .limit(1)
    )
    row = result.scalar_one_or_none()

    if row:
        return row.date()

    return date.today().replace(day=1)


async def check_token_limit(user: User, db: AsyncSession) -> tuple[bool, int]:
    """Check if a user has remaining tokens in the current billing period.

    Returns:
        Tuple of (is_allowed: bool, tokens_used_this_period: int).
        If user has no limit set (monthly_token_limit is None), always allowed.
    """
    if user.monthly_token_limit is None:
        return True, 0

    period_start = await _get_token_period_start(user, db)

    result = await db.execute(
        select(UserTokenUsage).where(
            UserTokenUsage.user_id == user.id,
            UserTokenUsage.month == period_start,
        )
    )
    usage = result.scalar_one_or_none()
    tokens_used = usage.tokens_used if usage else 0

    if tokens_used >= user.monthly_token_limit:
        # Monthly allotment exhausted for this period. Fall back to the
        # purchased (one-time) extra token bank if the user has any balance.
        extra = user.extra_token_balance or 0
        if extra > 0:
            return True, tokens_used
        return False, tokens_used

    return True, tokens_used


async def save_token_usage(user: User, db: AsyncSession, additional_tokens: int) -> None:
    """Record token usage for a user in the current billing period.

    Monthly allotment is consumed first; once the monthly bucket is full for
    the period, additional usage is drawn from the purchased extra_token_balance
    (one-time top-ups that roll over). The monthly usage row never exceeds the
    monthly limit, so the profile's "used / limit" display stays clean.
    """
    if additional_tokens <= 0:
        return

    period_start = await _get_token_period_start(user, db)

    result = await db.execute(
        select(UserTokenUsage).where(
            UserTokenUsage.user_id == user.id,
            UserTokenUsage.month == period_start,
        )
    )
    usage = result.scalar_one_or_none()
    tokens_used = usage.tokens_used if usage else 0

    # How much room is left in the monthly bucket this period?
    limit = user.monthly_token_limit
    free_room = max(0, (limit or 0) - tokens_used)
    to_monthly = min(additional_tokens, free_room)
    to_extra = additional_tokens - to_monthly

    if to_monthly > 0:
        if usage:
            usage.tokens_used += to_monthly
        else:
            usage = UserTokenUsage(
                user_id=user.id,
                month=period_start,
                tokens_used=to_monthly,
            )
            db.add(usage)
        usage_monthly_total = usage.tokens_used
        logger.info(
            "Token usage (monthly): +%d for user %s (period %s, total %d)",
            to_monthly, user.email, period_start, usage.tokens_used,
        )
    else:
        usage_monthly_total = tokens_used

    # Overflow beyond the monthly bucket comes out of the purchased bank
    if to_extra > 0:
        extra_before = user.extra_token_balance or 0
        user.extra_token_balance = max(0, extra_before - to_extra)
        logger.info(
            "Token usage (extra bank): -%d for user %s (was %d, now %s)",
            to_extra, user.email, extra_before, user.extra_token_balance,
        )

    await db.commit()
    logger.info(
        "Recorded %d tokens for user %s (period %s, monthly total %d)",
        additional_tokens, user.email, period_start, usage_monthly_total,
    )
