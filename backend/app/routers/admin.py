import asyncio
import logging
from datetime import datetime, timedelta, timezone
import zoneinfo
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request, status

logger = logging.getLogger(__name__)
from sqlalchemy import select, func, case, desc, text
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from jose import jwt, JWTError

from app.database import get_db
from app.models import User, Article, ChatHistory, CSMessage
from app.models.user_activity import UserActivity
from app.models.token_usage import UserTokenUsage
from app.models.nba import NBAArticle
from app.models.mlb import MLBArticle
from app.models.admin import SubscriptionPlan, UserSubscription, Payment
from app.core.config import settings
import json
import os
import pandas as _pd
from psycopg2.extras import RealDictCursor


from app.db_urls import PSYCOPG2_DATABASE_URL, ASYNC_DATABASE_URL

DATABASE_URL = PSYCOPG2_DATABASE_URL


def _pg_conn():
    import psycopg2
    # Strip async driver suffix (e.g. postgresql+asyncpg -> postgresql)
    # for psycopg2 compatibility when DATABASE_URL comes from env.
    url = DATABASE_URL
    if url and "+asyncpg" in url:
        url = url.replace("+asyncpg", "")
    # Also handle libpq key=value format with no password
    if url and "password=" not in url and "postgresql://" in url:
        # URL format without password — try connection anyway
        pass
    return psycopg2.connect(url)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Auth / Dependencies ─────────────────────────────────────────────

COOKIE_NAME = "earl_token"


def get_token_from_header(authorization: str | None = None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid auth scheme")
    return token


async def get_admin_user(
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
    request: Request = None,
) -> User:
    """Dependency that verifies JWT and checks is_admin=True.
    Checks the Authorization header first, then falls back to the earl_token cookie."""
    token = None
    # Try Authorization header first
    if authorization:
        try:
            token = get_token_from_header(authorization)
        except HTTPException:
            token = None
    # Fall back to cookie
    if not token and request:
        token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    return user


# ── Pydantic Schemas ────────────────────────────────────────────────

class UserOut(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    subscription_tier: str
    is_active: bool
    is_admin: bool
    email_verified: bool
    stripe_customer_id: str | None = None
    created_at: datetime | None = None
    last_login_at: datetime | None = None
    monthly_token_limit: int | None = None
    tokens_used: int = 0
    # Activity/usage summary
    distinct_days: int = 0
    distinct_ips: int = 0
    total_hits: int = 0
    last_active_at: datetime | None = None
    active_today: bool = False
    active_last_7: bool = False
    active_last_30: bool = False

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    display_name: Optional[str] = None
    subscription_tier: Optional[str] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    email_verified: Optional[bool] = None
    monthly_token_limit: Optional[int] = None


class PlanCreate(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    price_cents: int
    currency: str = "usd"
    interval: str = "month"
    kind: str = "subscription"  # "subscription" | "token_topup"
    token_amount: Optional[int] = None  # one-time token grant (token_topup)
    trial_days: int = 0
    features: list[str] = []
    is_active: bool = True
    sort_order: int = 0
    stripe_price_id: Optional[str] = None
    stripe_product_id: Optional[str] = None
    monthly_token_limit: Optional[int] = None
    payment_description: Optional[str] = None


class PlanUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    price_cents: Optional[int] = None
    currency: Optional[str] = None
    interval: Optional[str] = None
    kind: Optional[str] = None
    token_amount: Optional[int] = None
    trial_days: Optional[int] = None
    features: Optional[list[str]] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None
    stripe_price_id: Optional[str] = None
    stripe_product_id: Optional[str] = None
    monthly_token_limit: Optional[int] = None
    payment_description: Optional[str] = None


class PlanOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None = None
    price_cents: int
    currency: str
    interval: str
    kind: str | None = None
    token_amount: int | None = None
    trial_days: int
    features: list
    is_active: bool
    sort_order: int
    stripe_price_id: str | None = None
    stripe_product_id: str | None = None
    monthly_token_limit: int | None = None
    payment_description: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class SubscriptionOut(BaseModel):
    id: str
    user_id: str
    user_email: str = ""
    user_name: str = ""
    plan_id: str | None = None
    plan_name: str = ""
    status: str
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    canceled_at: datetime | None = None
    trial_end: datetime | None = None
    stripe_subscription_id: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class PaymentOut(BaseModel):
    id: str
    user_id: str
    user_email: str = ""
    user_name: str = ""
    subscription_id: str | None = None
    amount_cents: int
    currency: str
    status: str
    description: str | None = None
    stripe_invoice_id: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class PaymentListResponse(BaseModel):
    payments: list[PaymentOut]
    total: int
    total_cents: int
    page: int
    page_size: int


class DashboardStats(BaseModel):
    total_users: int
    active_users: int
    premium_users: int
    monthly_revenue_cents: int
    total_revenue_cents: int
    users_today: int
    users_this_week: int
    subscriptions_active: int
    subscriptions_canceled: int
    failed_payments: int
    plans_count: int


# ── User Chats (admin, all users) ───────────────────────────────────

class AdminChatMessage(BaseModel):
    id: int
    role: str
    message: str
    sport: str
    tokens_used: int | None = None
    model: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class AdminChatConversation(BaseModel):
    conversation_id: str
    user_id: str
    user_email: str
    user_name: str | None = None
    user_tier: str
    sport: str
    message_count: int
    turn_count: int
    total_tokens: int
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None
    messages: list[AdminChatMessage] = []


class ChatListResponse(BaseModel):
    conversations: list[AdminChatConversation]
    total: int


# ── Customer Service (admin) ────────────────────────────────────────

class CsAdminMessage(BaseModel):
    id: int
    role: str
    content: str
    tokens_used: int
    model: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class CsAdminConversation(BaseModel):
    user_id: str
    user_email: str
    user_name: str | None = None
    user_tier: str
    message_count: int
    total_tokens: int
    last_message_at: datetime | None = None
    messages: list[CsAdminMessage] = []


class CsAdminListResponse(BaseModel):
    conversations: list[CsAdminConversation]
    total: int


class CsEmailRequest(BaseModel):
    user_id: str
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=10000)


class CsEmailResult(BaseModel):
    sent: bool
    to: str
    message: str


# ── Dashboard ───────────────────────────────────────────────────────

@router.get("/stats", response_model=DashboardStats)
async def admin_dashboard_stats(
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregated dashboard statistics."""
    # Total / active users
    total_users = await db.scalar(select(func.count(User.id)))
    active_users = await db.scalar(select(func.count(User.id)).where(User.is_active == True))
    premium_users = await db.scalar(
        select(func.count(User.id)).where(User.subscription_tier != "free")
    )
    plans_count = await db.scalar(select(func.count(SubscriptionPlan.id)))

    # Users today / this week
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    users_today = await db.scalar(
        select(func.count(User.id)).where(User.created_at >= today_start)
    )
    users_this_week = await db.scalar(
        select(func.count(User.id)).where(User.created_at >= week_start)
    )

    # Subscriptions
    subs_active = await db.scalar(
        select(func.count(UserSubscription.id)).where(UserSubscription.status == "active")
    )
    subs_canceled = await db.scalar(
        select(func.count(UserSubscription.id)).where(UserSubscription.status == "canceled")
    )

    # Revenue
    total_rev = await db.scalar(
        select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
            Payment.status == "succeeded"
        )
    ) or 0

    # Monthly revenue (current month)
    month_start = today_start.replace(day=1)
    monthly_rev = await db.scalar(
        select(func.coalesce(func.sum(Payment.amount_cents), 0)).where(
            Payment.status == "succeeded",
            Payment.created_at >= month_start,
        )
    ) or 0

    # Failed payments
    failed_payments = await db.scalar(
        select(func.count(Payment.id)).where(Payment.status == "failed")
    ) or 0

    return DashboardStats(
        total_users=total_users or 0,
        active_users=active_users or 0,
        premium_users=premium_users or 0,
        monthly_revenue_cents=monthly_rev,
        total_revenue_cents=total_rev,
        users_today=users_today or 0,
        users_this_week=users_this_week or 0,
        subscriptions_active=subs_active or 0,
        subscriptions_canceled=subs_canceled or 0,
        failed_payments=failed_payments,
        plans_count=plans_count or 0,
    )


# ── Users CRUD ──────────────────────────────────────────────────────

@router.get("/users", response_model=list[UserOut])
async def list_users(
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
    search: str = Query("", max_length=100),
    tier: str = Query("", max_length=20),
    is_active: bool | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List users with search, filter, pagination, and token usage."""
    from datetime import date

    query = select(User)

    if search:
        query = query.where(
            User.email.ilike(f"%{search}%") | User.display_name.ilike(f"%{search}%")
        )
    if tier:
        query = query.where(User.subscription_tier == tier)
    if is_active is not None:
        query = query.where(User.is_active == is_active)

    query = query.order_by(desc(User.created_at)).offset(skip).limit(limit)
    result = await db.execute(query)
    users = result.scalars().all()

    # Batch fetch token usage for the current month
    first_of_month = date.today().replace(day=1)
    user_ids = [u.id for u in users] if users else []
    if user_ids:
        tu_result = await db.execute(
            select(UserTokenUsage.user_id, UserTokenUsage.tokens_used)
            .where(
                UserTokenUsage.month == first_of_month,
                UserTokenUsage.user_id.in_(user_ids),
            )
        )
        token_map = {row[0]: row[1] for row in tu_result.fetchall()}

        # Batch activity stats per user (lifetime + trailing windows)
        activity_rows = (await db.execute(
            select(
                UserActivity.user_id,
                func.count(func.distinct(UserActivity.activity_date)).label("distinct_days"),
                func.count(func.distinct(UserActivity.ip_address)).label("distinct_ips"),
                func.coalesce(func.sum(UserActivity.hit_count), 0).label("total_hits"),
                func.max(UserActivity.last_seen).label("last_seen"),
                func.max(case((UserActivity.activity_date == date.today(), 1), else_=0)).label("active_today"),
                func.max(case((UserActivity.activity_date >= date.today() - timedelta(days=7), 1), else_=0)).label("active_7"),
                func.max(case((UserActivity.activity_date >= date.today() - timedelta(days=30), 1), else_=0)).label("active_30"),
            )
            .where(UserActivity.user_id.in_(user_ids))
            .group_by(UserActivity.user_id)
        )).fetchall()
        act_map: dict[str, dict] = {
            a.user_id: {
                "distinct_days": a.distinct_days,
                "distinct_ips": a.distinct_ips,
                "total_hits": a.total_hits,
                "last_active_at": a.last_seen,
                "active_today": bool(a.active_today),
                "active_last_7": bool(a.active_7),
                "active_last_30": bool(a.active_30),
            }
            for a in activity_rows
        }
    else:
        token_map = {}
        act_map = {}

    response = []
    for u in users:
        out = UserOut.model_validate(u)
        out.tokens_used = token_map.get(u.id, 0)
        a = act_map.get(u.id, {})
        out.distinct_days = a.get("distinct_days", 0)
        out.distinct_ips = a.get("distinct_ips", 0)
        out.total_hits = a.get("total_hits", 0)
        out.last_active_at = a.get("last_active_at")
        out.active_today = a.get("active_today", False)
        out.active_last_7 = a.get("active_last_7", False)
        out.active_last_30 = a.get("active_last_30", False)
        response.append(out)

    return response


@router.get("/chats", response_model=ChatListResponse)
async def list_user_chats(
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
):
    """List the latest chat conversations across all users, newest activity first.
    Each conversation includes the full ordered message thread (user + assistant)
    so admins can see what users asked and how Earl answered."""
    # Per-conversation summary (most recent message time + counts)
    summary = (
        select(
            ChatHistory.conversation_id.label("conversation_id"),
            ChatHistory.user_id.label("user_id"),
            func.count().label("message_count"),
            func.count(
                func.nullif(ChatHistory.role, "assistant")
            ).label("turn_count"),
            func.coalesce(func.sum(ChatHistory.tokens_used), 0).label("total_tokens"),
            func.min(ChatHistory.created_at).label("first_message_at"),
            func.max(ChatHistory.created_at).label("last_message_at"),
        )
        .group_by(ChatHistory.conversation_id, ChatHistory.user_id)
        .subquery()
    )

    total = (
        await db.execute(select(func.count()).select_from(summary))
    ).scalar_one()

    convs = (
        await db.execute(
            select(
                summary.c.conversation_id.label("conversation_id"),
                summary.c.user_id.label("user_id"),
                summary.c.message_count.label("message_count"),
                summary.c.turn_count.label("turn_count"),
                summary.c.total_tokens.label("total_tokens"),
                summary.c.first_message_at.label("first_message_at"),
                summary.c.last_message_at.label("last_message_at"),
                User.id.label("db_user_id"),
                User.email.label("user_email"),
                User.display_name.label("user_name"),
                User.subscription_tier.label("user_tier"),
            )
            .join(User, User.id == summary.c.user_id)
            .order_by(desc(summary.c.last_message_at))
            .offset(skip)
            .limit(limit)
        )
    ).mappings().all()

    if not convs:
        return ChatListResponse(conversations=[], total=total)

    conv_ids = [c["conversation_id"] for c in convs]
    msgs = (
        await db.execute(
            select(ChatHistory)
            .where(ChatHistory.conversation_id.in_(conv_ids))
            .order_by(ChatHistory.created_at.asc(), ChatHistory.id.asc())
        )
    ).scalars().all()

    by_conv: dict[str, list[ChatHistory]] = {}
    for m in msgs:
        by_conv.setdefault(m.conversation_id, []).append(m)

    # Pick the most common sport per conversation for a best-effort label.
    sport_by_conv: dict[str, str] = {}
    for cid, mlist in by_conv.items():
        counts: dict[str, int] = {}
        for m in mlist:
            if m.sport:
                counts[m.sport] = counts.get(m.sport, 0) + 1
        sport_by_conv[cid] = max(counts, key=counts.get) if counts else ""

    conversations: list[AdminChatConversation] = []
    for c in convs:
        cid = c["conversation_id"]
        conversations.append(
            AdminChatConversation(
                conversation_id=cid,
                user_id=str(c["user_id"]),
                user_email=c["user_email"],
                user_name=c["user_name"],
                user_tier=c["user_tier"] or "free",
                sport=sport_by_conv.get(cid, ""),
                message_count=c["message_count"],
                turn_count=c["turn_count"],
                total_tokens=c["total_tokens"],
                first_message_at=c["first_message_at"],
                last_message_at=c["last_message_at"],
                messages=[
                    AdminChatMessage.model_validate(m) for m in by_conv.get(cid, [])
                ],
            )
        )

    return ChatListResponse(conversations=conversations, total=total)


# ── Customer Service (admin-facing) ─────────────────────────────────

@router.get("/cs/chats", response_model=CsAdminListResponse)
async def list_cs_conversations(
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List all customer-service conversations, most recent activity first."""
    summary = (
        select(
            CSMessage.user_id.label("user_id"),
            func.count().label("message_count"),
            func.coalesce(func.sum(CSMessage.tokens_used), 0).label("total_tokens"),
            func.max(CSMessage.created_at).label("last_message_at"),
        )
        .group_by(CSMessage.user_id)
        .subquery()
    )
    total = (await db.execute(select(func.count()).select_from(summary))).scalar_one()

    convs = (
        await db.execute(
            select(
                summary.c.user_id.label("user_id"),
                summary.c.message_count.label("message_count"),
                summary.c.total_tokens.label("total_tokens"),
                summary.c.last_message_at.label("last_message_at"),
                User.id.label("db_user_id"),
                User.email.label("user_email"),
                User.display_name.label("user_name"),
                User.subscription_tier.label("user_tier"),
            )
            .join(User, User.id == summary.c.user_id)
            .order_by(desc(summary.c.last_message_at))
            .offset(skip)
            .limit(limit)
        )
    ).mappings().all()

    conversations = []
    for c in convs:
        conversations.append(
            CsAdminConversation(
                user_id=c["user_id"],
                user_email=c["user_email"],
                user_name=c["user_name"],
                user_tier=c["user_tier"] or "free",
                message_count=c["message_count"],
                total_tokens=c["total_tokens"],
                last_message_at=c["last_message_at"],
                messages=[],
            )
        )
    return CsAdminListResponse(conversations=conversations, total=total)


@router.get("/cs/chats/{user_id}", response_model=CsAdminConversation)
async def get_cs_conversation(
    user_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Full customer-service thread for a single user."""
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    msgs = (
        await db.execute(
            select(CSMessage)
            .where(CSMessage.user_id == user_id)
            .order_by(CSMessage.created_at.asc(), CSMessage.id.asc())
        )
    ).scalars().all()

    return CsAdminConversation(
        user_id=str(user.id),
        user_email=user.email,
        user_name=user.display_name,
        user_tier=user.subscription_tier or "free",
        message_count=len(msgs),
        total_tokens=sum(m.tokens_used for m in msgs),
        last_message_at=msgs[-1].created_at if msgs else None,
        messages=[CsAdminMessage.model_validate(m) for m in msgs],
    )


@router.post("/cs/emails", response_model=CsEmailResult)
async def send_cs_email(
    body: CsEmailRequest,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Send an email to a customer via Resend (e.g. as a follow-up to their CS chat)."""
    user = (
        await db.execute(select(User).where(User.id == body.user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not settings.resend_api_key or settings.resend_api_key.startswith("re_xxx"):
        logger.warning(
            "No Resend key configured — CS email for %s NOT sent. Body: %s",
            user.email, body.body[:200],
        )
        return CsEmailResult(sent=False, to=user.email, message="Resend not configured (skipped)")

    try:
        import resend
        resend.api_key = settings.resend_api_key
        # HTML-escape the body for safe rendering.
        import html as _html
        safe_body = _html.escape(body.body).replace("\n", "<br/>")
        sent = resend.Emails.send(
            {
                "from": "Earl Knows Ball Support <support@users.earlknowsball.com>",
                "to": [user.email],
                "subject": body.subject,
                "html": (
                    "<div style='font-family:sans-serif;max-width:560px;margin:0 auto;'>"
                    f"<p>{safe_body}</p>"
                    "<hr style='border:none;border-top:1px solid #eee;margin:24px 0;'/>"
                    "<p style='color:#666;font-size:12px;'>— Earl Knows Ball Support</p>"
                    "</div>"
                ),
            }
        )
        logger.info("CS email sent to %s: %s", user.email, sent)
        return CsEmailResult(sent=True, to=user.email, message="Email sent")
    except ImportError:
        logger.warning("Resend SDK not installed — CS email to %s skipped", user.email)
        return CsEmailResult(sent=False, to=user.email, message="Resend SDK not installed")
    except Exception as e:
        logger.warning("Failed to send CS email to %s: %s", user.email, e)
        return CsEmailResult(sent=False, to=user.email, message=f"Send failed: {e}")


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    from datetime import date as _date
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    out = UserOut.model_validate(user)

    # Current-month token usage
    first_of_month = _date.today().replace(day=1)
    tu = (await db.execute(
        select(UserTokenUsage.tokens_used).where(
            UserTokenUsage.user_id == user.id,
            UserTokenUsage.month == first_of_month,
        )
    )).scalar_one_or_none()
    out.tokens_used = tu or 0

    # Lifetime + trailing-window activity summary
    row = (await db.execute(
        select(
            func.count(func.distinct(UserActivity.activity_date)).label("distinct_days"),
            func.count(func.distinct(UserActivity.ip_address)).label("distinct_ips"),
            func.coalesce(func.sum(UserActivity.hit_count), 0).label("total_hits"),
            func.max(UserActivity.last_seen).label("last_seen"),
            func.max(case((UserActivity.activity_date == _date.today(), 1), else_=0)).label("active_today"),
            func.max(case((UserActivity.activity_date >= _date.today() - timedelta(days=7), 1), else_=0)).label("active_7"),
            func.max(case((UserActivity.activity_date >= _date.today() - timedelta(days=30), 1), else_=0)).label("active_30"),
        ).where(UserActivity.user_id == user.id)
    )).one()
    out.distinct_days = row.distinct_days or 0
    out.distinct_ips = row.distinct_ips or 0
    out.total_hits = row.total_hits or 0
    out.last_active_at = row.last_seen
    out.active_today = bool(row.active_today)
    out.active_last_7 = bool(row.active_7)
    out.active_last_30 = bool(row.active_30)
    return out


class UserActivityDay(BaseModel):
    activity_date: datetime
    ip_address: str
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    hit_count: int = 0


class UserActivityResponse(BaseModel):
    user_id: str
    total_days: int = 0
    total_ips: int = 0
    total_hits: int = 0
    days: list[UserActivityDay] = []


@router.get("/users/{user_id}/activity", response_model=UserActivityResponse)
async def get_user_activity(
    user_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(365, ge=1, le=1000),
):
    """Drill-down: every (day, IP) activity row for a user, newest first.
    Also returns lifetime totals for the header cards."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    rows = (await db.execute(
        select(UserActivity)
        .where(UserActivity.user_id == user_id)
        .order_by(desc(UserActivity.activity_date), desc(UserActivity.last_seen))
        .offset(skip)
        .limit(limit)
    )).scalars().all()

    days = [
        UserActivityDay(
            activity_date=r.activity_date,
            ip_address=r.ip_address,
            first_seen=r.first_seen,
            last_seen=r.last_seen,
            hit_count=r.hit_count,
        )
        for r in rows
    ]

    totals = (await db.execute(
        select(
            func.count(func.distinct(UserActivity.activity_date)),
            func.count(func.distinct(UserActivity.ip_address)),
            func.coalesce(func.sum(UserActivity.hit_count), 0),
        ).where(UserActivity.user_id == user_id)
    )).one()

    return UserActivityResponse(
        user_id=user_id,
        total_days=totals[0] or 0,
        total_ips=totals[1] or 0,
        total_hits=totals[2] or 0,
        days=days,
    )


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str,
    data: UserUpdate,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(user, key, value)

    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Hard delete (or could soft-delete via is_active=False)
    await db.delete(user)
    await db.commit()


# ── Subscription Plans CRUD ─────────────────────────────────────────

@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SubscriptionPlan).order_by(SubscriptionPlan.sort_order)
    )
    return result.scalars().all()


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan(
    plan_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


@router.post("/plans", response_model=PlanOut, status_code=201)
async def create_plan(
    data: PlanCreate,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    plan = SubscriptionPlan(**data.model_dump())
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.patch("/plans/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: str,
    data: PlanUpdate,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(plan, key, value)

    await db.commit()
    await db.refresh(plan)
    return plan


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan(
    plan_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    await db.delete(plan)
    await db.commit()


# ── Subscriptions ───────────────────────────────────────────────────

@router.get("/subscriptions", response_model=list[SubscriptionOut])
async def list_subscriptions(
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
    status_filter: str = Query("", max_length=20),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    query = (
        select(
            UserSubscription,
            User.email,
            User.display_name,
            SubscriptionPlan.name,
        )
        .outerjoin(User, User.id == UserSubscription.user_id)
        .outerjoin(SubscriptionPlan, SubscriptionPlan.id == UserSubscription.plan_id)
    )
    if status_filter:
        query = query.where(UserSubscription.status == status_filter)
    query = query.order_by(desc(UserSubscription.created_at)).offset(skip).limit(limit)

    result = await db.execute(query)
    rows = result.all()

    return [
        SubscriptionOut(
            id=sub.id,
            user_id=sub.user_id,
            user_email=email or "",
            user_name=display_name or "",
            plan_id=sub.plan_id,
            plan_name=plan_name or "",
            status=sub.status,
            current_period_start=sub.current_period_start,
            current_period_end=sub.current_period_end,
            canceled_at=sub.canceled_at,
            trial_end=sub.trial_end,
            stripe_subscription_id=sub.stripe_subscription_id,
            created_at=sub.created_at,
        )
        for sub, email, display_name, plan_name in rows
    ]


@router.get("/subscriptions/{sub_id}", response_model=SubscriptionOut)
async def get_subscription(
    sub_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            UserSubscription,
            User.email,
            User.display_name,
            SubscriptionPlan.name,
        )
        .outerjoin(User, User.id == UserSubscription.user_id)
        .outerjoin(SubscriptionPlan, SubscriptionPlan.id == UserSubscription.plan_id)
        .where(UserSubscription.id == sub_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sub, email, display_name, plan_name = row
    return SubscriptionOut(
        id=sub.id,
        user_id=sub.user_id,
        user_email=email or "",
        user_name=display_name or "",
        plan_id=sub.plan_id,
        plan_name=plan_name or "",
        status=sub.status,
        current_period_start=sub.current_period_start,
        current_period_end=sub.current_period_end,
        canceled_at=sub.canceled_at,
        trial_end=sub.trial_end,
        stripe_subscription_id=sub.stripe_subscription_id,
        created_at=sub.created_at,
    )


@router.patch("/subscriptions/{sub_id}", response_model=SubscriptionOut)
async def update_subscription(
    sub_id: str,
    data: dict,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserSubscription).where(UserSubscription.id == sub_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    allowed_fields = {"status", "current_period_start", "current_period_end", "canceled_at", "plan_id"}
    for key, value in data.items():
        if key in allowed_fields:
            setattr(sub, key, value)

    await db.commit()

    # Re-fetch with joins
    return await get_subscription(sub_id, admin, db)


# ── Payments ────────────────────────────────────────────────────────

@router.get("/payments", response_model=PaymentListResponse)
async def list_payments(
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
    status_filter: str = Query("", max_length=20),
    date_from: str = Query("", max_length=30),
    date_to: str = Query("", max_length=30),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    # Build the WHERE conditions to reuse across count, sum, and page queries
    filters = []
    if status_filter:
        filters.append(Payment.status == status_filter)

    today = datetime.now(timezone.utc).date()
    if date_from:
        try:
            dt_from = datetime.fromisoformat(date_from)
            if dt_from.tzinfo is None:
                dt_from = dt_from.replace(tzinfo=timezone.utc)
            filters.append(Payment.created_at >= dt_from)
        except ValueError:
            pass
    else:
        # Default: today's payments (start of today UTC)
        dt_from = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        filters.append(Payment.created_at >= dt_from)

    if date_to:
        try:
            dt_to = datetime.fromisoformat(date_to)
            if dt_to.tzinfo is None:
                dt_to = dt_to.replace(tzinfo=timezone.utc)
            dt_to = dt_to.replace(hour=23, minute=59, second=59, microsecond=999999)
            filters.append(Payment.created_at <= dt_to)
        except ValueError:
            pass

    # Count total matching records
    count_query = select(func.count(Payment.id)).where(*filters)
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Sum amount_cents for matching records
    sum_query = select(
        func.coalesce(func.sum(Payment.amount_cents), 0)
    ).where(*filters)
    sum_result = await db.execute(sum_query)
    total_cents = sum_result.scalar() or 0

    # Fetch page with user join
    skip = (page - 1) * page_size
    query = (
        select(Payment, User.email, User.display_name)
        .outerjoin(User, User.id == Payment.user_id)
        .where(*filters)
        .order_by(desc(Payment.created_at))
        .offset(skip)
        .limit(page_size)
    )
    result = await db.execute(query)
    rows = result.all()

    payments = [
        PaymentOut(
            id=p.id,
            user_id=p.user_id,
            user_email=email or "",
            user_name=display_name or "",
            subscription_id=p.subscription_id,
            amount_cents=p.amount_cents,
            currency=p.currency,
            status=p.status,
            description=p.description,
            stripe_invoice_id=p.stripe_invoice_id,
            created_at=p.created_at,
        )
        for p, email, display_name in rows
    ]

    return PaymentListResponse(
        payments=payments,
        total=total,
        total_cents=total_cents,
        page=page,
        page_size=page_size,
    )


@router.get("/payments/{payment_id}", response_model=PaymentOut)
async def get_payment(
    payment_id: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Payment, User.email, User.display_name)
        .outerjoin(User, User.id == Payment.user_id)
        .where(Payment.id == payment_id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Payment not found")

    p, email, display_name = row
    return PaymentOut(
        id=p.id,
        user_id=p.user_id,
        user_email=email or "",
        user_name=display_name or "",
        subscription_id=p.subscription_id,
        amount_cents=p.amount_cents,
        currency=p.currency,
        status=p.status,
        description=p.description,
        stripe_invoice_id=p.stripe_invoice_id,
        created_at=p.created_at,
    )


# ── Model Detail / Feature Definitions ──────────────────────────────

# ── MLB Feature Definitions ──────────────────────────────────────────
# Three variants: ATS (Run Line), O/U, and ML

_MLB_ATS_DESCRIPTIONS = {
    "__desc__": (
        "Run Line-optimized model. Predicts run differential (home - away) "
        "using rolling team stats (runs scored/allowed over 5/10/20 game windows), "
        "home/road splits, rest days, travel distance, betting market implied "
        "probabilities, and situational factors (month, dome, division). "
        "The model is trained in a rolling year-by-year fashion to prevent look-ahead bias."
    ),
    # Rolling runs for/against
    "h_rf5": "Home team runs scored per game (last 5)",
    "h_rf10": "Home team runs scored per game (last 10)",
    "h_rf20": "Home team runs scored per game (last 20)",
    "a_rf5": "Away team runs scored per game (last 5)",
    "a_rf10": "Away team runs scored per game (last 10)",
    "a_rf20": "Away team runs scored per game (last 20)",
    "h_ra5": "Home team runs allowed per game (last 5)",
    "h_ra10": "Home team runs allowed per game (last 10)",
    "h_ra20": "Home team runs allowed per game (last 20)",
    "a_ra5": "Away team runs allowed per game (last 5)",
    "a_ra10": "Away team runs allowed per game (last 10)",
    "a_ra20": "Away team runs allowed per game (last 20)",
    # Home/away splits
    "h_home_rf": "Home team runs scored per game at home (season)",
    "h_home_ra": "Home team runs allowed per game at home (season)",
    "a_home_rf": "Away team runs scored per game on road (season)",
    "a_home_ra": "Away team runs allowed per game on road (season)",
    # Rest / travel
    "rest_h": "Days rest for home team",
    "rest_a": "Days rest for away team",
    "rest_diff": "Rest advantage (home - away)",
    "travel_miles": "Away team travel distance in miles",
    "tz_diff": "Time zone difference between teams",
    # Win percentages
    "h_winpct": "Home team win percentage",
    "a_winpct": "Away team win percentage",
    "winpct_diff": "Win percentage difference (home - away)",
    # Betting market
    "h_implied": "Home team implied win probability from moneyline",
    "a_implied": "Away team implied win probability from moneyline",
    "is_home_fav": "Binary: 1 if home team is moneyline favorite",
    "ou_line": "Closing over/under total",
    # Situational
    "is_div": "Binary: 1 if intra-division matchup",
    "is_dome": "Binary: 1 if game is in domed stadium",
    "month": "Calendar month (3-10)",
    "is_summer": "Binary: 1 if June/July/August",
}

_MLB_ATS_CATEGORIES = {
    "Rolling Stats (Runs)": ["h_rf5", "h_rf10", "h_rf20", "a_rf5", "a_rf10", "a_rf20",
                              "h_ra5", "h_ra10", "h_ra20", "a_ra5", "a_ra10", "a_ra20"],
    "Home/Road Splits": ["h_home_rf", "h_home_ra", "a_home_rf", "a_home_ra"],
    "Rest & Travel": ["rest_h", "rest_a", "rest_diff", "travel_miles", "tz_diff"],
    "Team Strength": ["h_winpct", "a_winpct", "winpct_diff"],
    "Betting Market": ["h_implied", "a_implied", "is_home_fav", "ou_line"],
    "Situational": ["is_div", "is_dome", "month", "is_summer"],
}

_MLB_OU_DESCRIPTIONS = {
    "__desc__": (
        "26-feature OU model predicting total runs directly. Uses the opening "
        "over/under line as the primary anchor, then adjusts for market movement, "
        "closing over/under odds, implied probability, starting pitcher quality "
        "(L5/L20 ERA), bullpen fatigue (IP), team scoring and defense "
        "(10/20 game rolling windows), team OPS (10/20 game rolling windows), "
        "home/road scoring splits, season win percentage, over frequency, "
        "travel distance, time zone difference, division rivalry, temperature, "
        "wind speed, dome status, and venue park factor. Trained with a direct "
        "total target using time-weighted samples across 5-year rolling windows."
    ),
    # Market
    "opening_ou": "Opening over/under total from sportsbook (primary market anchor)",
    "ou_movement": "Closing OU - opening OU (positive = line moved up = sharp money on over)",
    "closing_over_odds": "Closing American odds on the over (e.g. -110 = 52.4% implied)",
    "closing_spread_home_odds": "Closing spread odds for home team (American)",
    "closing_spread_away_odds": "Closing spread odds for away team (American)",
    "closing_home_implied_probability": "Implied win % for home team from closing moneyline",
    "closing_away_implied_probability": "Implied win % for away team from closing moneyline",
    # Teams
    "h_rf10": "Home team runs scored per game (last 10)",
    "a_rf10": "Away team runs scored per game (last 10)",
    "h_ra10": "Home team runs allowed per game (last 10)",
    "a_ra10": "Away team runs allowed per game (last 10)",
    "h_ops_l10": "Home team OPS (last 10 games)",
    "a_ops_l10": "Away team OPS (last 10 games)",
    "h_ops_l20": "Home team OPS (last 20 games)",
    "a_ops_l20": "Away team OPS (last 20 games)",
    "over_pct_h_r20": "Home team over rate last 20 games",
    "over_pct_a_r20": "Away team over rate last 20 games",
    # Starting pitcher
    "h_pitcher_era_l5": "Home starter's ERA in his last 5 starts (recent form)",
    "a_pitcher_era_l5": "Away starter's ERA in his last 5 starts (recent form)",
    "h_pitcher_era_l20": "Home starter's ERA in his last 20 starts (talent baseline)",
    "a_pitcher_era_l20": "Away starter's ERA in his last 20 starts (talent baseline)",
    # Bullpen fatigue
    "h_bullpen_ip_l5": "Home bullpen innings pitched last 5 games (fatigue proxy — more IP = tired arms = more runs)",
    "a_bullpen_ip_l5": "Away bullpen innings pitched last 5 games (fatigue proxy)",
    # Opponent-adjusted
    "h_home_rf": "Home team runs scored per game at home (expanding avg)",
    "a_away_rf": "Away team runs scored per game on road (expanding avg)",
    "h_winpct": "Home team season win percentage",
    "a_winpct": "Away team season win percentage",
    # Weather / Situational
    "temperature": "Game temperature in Fahrenheit (warm = more runs)",
    "wind_speed": "Game wind speed in mph (strong wind affects fly balls)",
    "is_dome": "Binary: 1 if domed stadium / retractable roof closed (no weather effects)",
    "travel_miles": "Away team travel distance in miles (haversine)",
    "tz_diff": "Time zone difference (home UTC offset - away UTC offset)",
    "is_div": "Binary: 1 if intra-division matchup (familiarity)",
    # Park
    "park_factor": "Venue run factor (venue avg total / league avg total)",
}

_MLB_OU_CATEGORIES = {
    "Market & Line Movement": ["opening_ou", "ou_movement",
                                "closing_over_odds",
                                "closing_spread_home_odds", "closing_spread_away_odds",
                                "closing_home_implied_probability",
                                "closing_away_implied_probability"],
    "Team Scoring & Offense": ["h_rf10", "a_rf10", "h_ra10", "a_ra10",
                                "h_ops_l10", "a_ops_l10", "h_ops_l20", "a_ops_l20"],
    "Over Frequency": ["over_pct_h_r20", "over_pct_a_r20"],
    "Starting Pitcher": ["h_pitcher_era_l5", "a_pitcher_era_l5",
                          "h_pitcher_era_l20", "a_pitcher_era_l20"],
    "Bullpen Fatigue": ["h_bullpen_ip_l5", "a_bullpen_ip_l5"],
    "Opponent-Adjusted": ["h_home_rf", "a_away_rf", "h_winpct", "a_winpct"],
    "Weather & Venue": ["temperature", "wind_speed", "is_dome"],
    "Travel & Situational": ["travel_miles", "tz_diff", "is_div"],
    "Park Factor": ["park_factor"],
}

_MLB_ML_DESCRIPTIONS = {
    "__desc__": (
        "24-feature Moneyline model (binary classifier). Predicts home team win "
        "probability using closing moneyline probability as the baseline, "
        "line movement (sharp money signal), starter and bullpen pitcher quality "
        "(ERA and IP), rolling runs scored/allowed, win percentage, home/road "
        "scoring splits, recent form (wins last 10), rest advantage, travel "
        "distance, division rivalry, and dome status."
    ),
    # Market
    "home_implied": "Closing home team implied win probability (fixed market baseline)",
    "ml_implied_movement": "Closing home implied - opening home implied (positive = sharp money on home)",
    # Starting pitcher
    "h_pitcher_era_l5": "Home starter's ERA last 5 starts (recent form)",
    "a_pitcher_era_l5": "Away starter's ERA last 5 starts (recent form)",
    "h_pitcher_era_l20": "Home starter's ERA last 20 starts (talent baseline)",
    "a_pitcher_era_l20": "Away starter's ERA last 20 starts (talent baseline)",
    # Bullpen
    "h_bullpen_era_l5": "Home bullpen ERA last 5 games (quality)",
    "a_bullpen_era_l5": "Away bullpen ERA last 5 games (quality)",
    "h_bullpen_ip_l5": "Home bullpen innings pitched last 5 games (fatigue)",
    "a_bullpen_ip_l5": "Away bullpen innings pitched last 5 games (fatigue)",
    # Team quality
    "h_rf10": "Home team runs scored per game (last 10)",
    "a_rf10": "Away team runs scored per game (last 10)",
    "h_ra10": "Home team runs allowed per game (last 10)",
    "a_ra10": "Away team runs allowed per game (last 10)",
    "h_winpct": "Home team win percentage",
    "a_winpct": "Away team win percentage",
    # Home/road splits
    "h_home_rf": "Home team runs scored per game at home (expanding avg)",
    "a_away_rf": "Away team runs scored per game on road (expanding avg)",
    # Recent form
    "h_form_l10": "Home team wins in last 10 games",
    "a_form_l10": "Away team wins in last 10 games",
    # Situational
    "rest_diff": "Rest advantage (home days off - away days off)",
    "travel_miles": "Away team travel distance in miles",
    "is_div": "Binary: 1 if intra-division matchup",
    "is_dome": "Binary: 1 if game is in domed stadium",
}

_MLB_ML_CATEGORIES = {
    "Market & Line Movement": ["home_implied", "ml_implied_movement"],
    "Starting Pitcher": ["h_pitcher_era_l5", "a_pitcher_era_l5",
                          "h_pitcher_era_l20", "a_pitcher_era_l20"],
    "Bullpen": ["h_bullpen_era_l5", "a_bullpen_era_l5",
                 "h_bullpen_ip_l5", "a_bullpen_ip_l5"],
    "Team Quality": ["h_rf10", "a_rf10", "h_ra10", "a_ra10",
                      "h_winpct", "a_winpct"],
    "Home/Road Splits": ["h_home_rf", "a_away_rf"],
    "Recent Form": ["h_form_l10", "a_form_l10"],
    "Rest, Travel & Situational": ["rest_diff", "travel_miles", "is_div", "is_dome"],
}


# ── NFL ATS Model Feature Descriptions ──
_ATS_DESCRIPTIONS = {
    "__desc__": "Spread-optimized model. Features: opponent-adjusted scoring, implied market (OU-based), spread movement, short-term form (5G win%, 3G margin, cover streak, bounce-back), long-term identity (10G margin, season YTD ATS%), dome. No raw spread — captures market dynamics, scoring differentials, streaks, and season identity.",
    "hpf": "Home team opponent-adjusted PPG (weighted by opponent strength)",
    "hpa": "Home team opponent-adjusted PPG allowed",
    "apf": "Away team opponent-adjusted PPG",
    "apa": "Away team opponent-adjusted PPG allowed",
    "dpf": "Points scored differential (offense - defense)",
    "dpa": "Points allowed differential (opponent-adjusted)",
    "himp": "Home team implied scoring from over/under line",
    "aimp": "Away team implied scoring from over/under line",
    "dimp": "Home-away implied scoring difference (market edge)",
    "spread_movement": "Point spread movement from open to close",
    "home_win_pct_r5": "Home team win rate in last 5 games (recent form)",
    "away_win_pct_r5": "Away team win rate in last 5 games (recent form)",
    "home_margin_r3": "Home team avg margin in last 3 games (blowout vs squeak)",
    "away_margin_r3": "Away team avg margin in last 3 games",
    "home_cover_pct_r5": "Home team ATS cover rate last 5 games (ATS hot/cold streak)",
    "away_cover_pct_r5": "Away team ATS cover rate last 5 games",
    "home_embarrassed": "Binary: 1 if home team lost by 14+ in most recent game (bounce-back spot)",
    "away_embarrassed": "Binary: 1 if away team lost by 14+ in most recent game",
    "home_season_ats_pct": "Home team cumulative season-to-date ATS cover rate (season identity)",
    "away_season_ats_pct": "Away team cumulative season-to-date ATS cover rate",
    "home_margin_r10": "Home team avg margin in last 10 games (half-season identity)",
    "away_margin_r10": "Away team avg margin in last 10 games",
    "travel_miles": "Away team travel distance in miles (haversine from stadium coordinates)",
    "is_dome": "Binary: 1 if game is in a domed stadium",
}
_ATS_CATEGORIES = {
    "Opponent-Adjusted Scoring": ["hpf", "hpa", "apf", "apa", "dpf", "dpa"],
    "Market Features": ["himp", "aimp", "dimp", "spread_movement"],
    "Short-Term Form": ["home_win_pct_r5", "away_win_pct_r5", "home_margin_r3", "away_margin_r3", "home_cover_pct_r5", "away_cover_pct_r5", "home_embarrassed", "away_embarrassed"],
    "Long-Term Identity": ["home_margin_r10", "away_margin_r10", "home_season_ats_pct", "away_season_ats_pct"],
    "Situational": ["travel_miles", "is_dome"],
}

# ── NFL OU Model Feature Descriptions ──
_OU_DESCRIPTIONS = {
    "__desc__": "Total-optimized model. Predicts combined points using market anchors (opening_ou, spread, ou_movement), opponent-adjusted scoring diffs, form (win%, margins r3/r10), and situational factors.",
    "dpf": "Points scored differential (offense - defense, opponent-adjusted)",
    "dpa": "Points allowed differential (opponent-adjusted)",
    "himp": "Home team implied scoring from OU line",
    "aimp": "Away team implied scoring from OU line",
    "dimp": "Implied scoring difference",
    "opening_ou": "Opening over/under line (pre-market anchor)",
    "spread": "Point spread (cross-market anchor)",
    "ou_movement": "OU movement from open to close (late-market shift)",
    "home_win_pct_r5": "Home team win rate last 5 games",
    "away_win_pct_r5": "Away team win rate last 5 games",
    "home_margin_r3": "Home team avg margin last 3 games",
    "away_margin_r3": "Away team avg margin last 3 games",
    "home_margin_r10": "Home team avg margin last 10 games (half-season identity)",
    "away_margin_r10": "Away team avg margin last 10 games",
    "rest_diff": "Rest days advantage (home - away)",
    "travel_miles": "Away team travel distance",
    "tz_diff": "Time zone difference",
    "is_short": "Short week flag (Thu/Fri/Sat)",
    "is_dome": "Dome stadium flag",
    "temp": "Temperature (F) at kickoff",
    "wind": "Wind speed (mph) at kickoff",
}
_OU_CATEGORIES = {
    "Opponent-Adjusted Scoring": ["dpf", "dpa"],
    "Market Features": ["himp", "aimp", "dimp", "opening_ou", "spread", "ou_movement"],
    "Form & Identity": ["home_win_pct_r5", "away_win_pct_r5", "home_margin_r3", "away_margin_r3", "home_margin_r10", "away_margin_r10"],
    "Rest & Travel": ["rest_diff", "travel_miles", "tz_diff"],
    "Situational": ["is_short", "is_dome", "temp", "wind"],
}

# ── NBA Model Feature Descriptions ──
_NBA_ATS_DESCRIPTIONS = {
    "__desc__": "NBA ATS model. Rolling form streaks (ATS margin/wins, straight-up wins), implied market scoring, and betting lines. Features from season-level rolling windows.",
    "h_ats_margin_5": "Home team avg ATS cover margin last 5 games",
    "a_ats_margin_5": "Away team avg ATS cover margin last 5 games",
    "h_ats_wins_5": "Home team ATS wins in last 5 games (count 0-5)",
    "a_ats_wins_5": "Away team ATS wins in last 5 games (count 0-5)",
    "h_implied": "Home team implied scoring from OU line",
    "a_implied": "Away team implied scoring from OU line",
    "h_wins_5": "Home team straight-up wins in last 5 games",
    "h_wins_10": "Home team straight-up wins in last 10 games",
    "a_wins_5": "Away team straight-up wins in last 5 games",
    "a_wins_10": "Away team straight-up wins in last 10 games",
}
_NBA_ATS_CATEGORIES = {
    "Form & Streaks": ["h_ats_wins_5", "a_ats_wins_5", "h_ats_margin_5", "a_ats_margin_5",
                       "h_wins_5", "h_wins_10", "a_wins_5", "a_wins_10"],
    "Market Features": ["h_implied", "a_implied"],
}
_NBA_OU_DESCRIPTIONS = {
    "__desc__": "NBA OU model. Projects total points using straight-up win streaks, implied market scoring, and betting lines.",
    "h_implied": "Home team implied scoring from OU line",
    "a_implied": "Away team implied scoring from OU line",
    "h_wins_5": "Home team straight-up wins in last 5 games",
    "h_wins_10": "Home team straight-up wins in last 10 games",
    "a_wins_5": "Away team straight-up wins in last 5 games",
    "a_wins_10": "Away team straight-up wins in last 10 games",
}
_NBA_OU_CATEGORIES = {
    "Form & Identity": ["h_wins_5", "h_wins_10", "a_wins_5", "a_wins_10"],
    "Market Features": ["h_implied", "a_implied"],
}

class ModelFeatureOut(BaseModel):
    name: str
    description: str
    importance: float
    category: str


class ModelMonthlyOut(BaseModel):
    month: int
    games: int
    mae: float
    ml_pct: float


class ModelBettingOut(BaseModel):
    correct: int
    incorrect: int
    total: int
    pct: float
    pushes: int = 0


class ModelVariantOut(BaseModel):
    name: str  # "ATS", "O/U", or "ML"
    description: str
    algorithm: str
    total_features: int
    features: list[ModelFeatureOut]
    feature_categories: list[dict]
    backtest_results: list[dict]
    overall_mae: float = 0
    overall_ats: ModelBettingOut | None = None
    overall_ou: ModelBettingOut | None = None
    overall_ml: ModelBettingOut | None = None
    feature_importance_plot: list[dict] = []


class HighConfidenceOut(BaseModel):
    threshold: float
    total: int
    correct: int
    pct: float
    ou_total: int = 0
    ou_correct: int = 0
    ou_pct: float = 0.0
    ml_total: int = 0
    ml_correct: int = 0
    ml_pct: float = 0.0


class SportModelDetailOut(BaseModel):
    sport: str
    model_type: str
    description: str
    algorithm: str
    training_years: list[int]
    test_years: list[int]
    total_features: int
    features: list[ModelFeatureOut]
    feature_categories: list[dict]
    backtest_results: list[dict]
    overall_mae: float
    overall_ats: ModelBettingOut
    overall_ou: ModelBettingOut | None = None
    overall_ml: ModelBettingOut | None = None
    monthly: list[ModelMonthlyOut]
    high_confidence: list[HighConfidenceOut]
    feature_importance_plot: list[dict]
    last_updated: str | None = None
    # Three-specialized-model support
    model_variants: list[ModelVariantOut] = []


# ── Article model lookup ────────────────────────────────────────────

_ARTICLE_MODELS = {
    "nfl": Article,
    "nba": NBAArticle,
    "mlb": MLBArticle,
}


class ArticleStat(BaseModel):
    total: int
    embedded: int
    unembedded: int
    with_body: int
    null_published_at: int
    by_source: list[dict]
    by_year: list[dict]


class ArticleOut(BaseModel):
    id: int
    title: str
    slug: str
    excerpt: str | None = None
    category: str | None = None
    published_at: datetime | None = None
    created_at: datetime | None = None
    author: str | None = None
    source_url: str | None = None
    source_name: str | None = None
    source_type: str | None = None
    embedded_at: datetime | None = None

    model_config = {"from_attributes": True}


# ── RSS Feeds ──────────────────────────────────────────────────────

@router.get("/articles/{sport}/rss-feeds")
async def list_rss_feeds(
    sport: str,
    team: str = Query("", description="Filter by team abbreviation (e.g. CHI, BOS, LAD). Empty = all"),
    group_by_team: bool = Query(False, description="Group team-specific feeds as sub-arrays"),
    admin: User = Depends(get_admin_user),
):
    """
    Return RSS feeds for a sport, optionally filtered by team.

    Team-specific feeds (SB Nation team blogs) are tagged with their
    team abbreviation. General/league-wide feeds have team=null.

    When group_by_team=true, returns feeds organized as:
    { teams: { "CHI": [...feeds], ... }, general: [...feeds] }
    """
    from app.ingestion.rss_feeds import get_all_feeds, get_feeds_for_team, get_teams_for_sport

    valid_sports = {"nfl", "nba", "mlb"}
    if sport not in valid_sports:
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")

    if team:
        feeds = get_feeds_for_team(sport, team.upper())
        return {"sport": sport, "team": team.upper(), "total": len(feeds), "feeds": feeds}

    if group_by_team:
        from app.ingestion.rss_feeds import get_feeds_for_sport
        team_feeds, general_feeds = get_feeds_for_sport(sport)
        teams: dict[str, list[dict]] = {}
        for f in team_feeds:
            abbr = f["team"]
            if abbr not in teams:
                teams[abbr] = []
            teams[abbr].append(f)
        return {
            "sport": sport,
            "total": len(team_feeds) + len(general_feeds),
            "teams": {k: v for k, v in sorted(teams.items())},
            "general": general_feeds,
        }

    all_feeds = get_all_feeds(sport)
    return {"sport": sport, "total": len(all_feeds), "feeds": all_feeds}


# ── Articles Admin ───────────────────────────────────────────────────

@router.get("/articles/{sport}/stats", response_model=ArticleStat)
async def article_stats(
    sport: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Article statistics for a given sport (nfl, nba, mlb)."""
    model = _ARTICLE_MODELS.get(sport)
    if not model:
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")

    total = await db.scalar(select(func.count(model.id))) or 0
    embedded = await db.scalar(select(func.count(model.id)).where(model.embedded_at.isnot(None))) or 0
    unembedded = await db.scalar(select(func.count(model.id)).where(model.embedded_at.is_(None))) or 0
    with_body = await db.scalar(
        select(func.count(model.id)).where(model.body.isnot(None), model.body != "")
    ) or 0
    null_pub = await db.scalar(
        select(func.count(model.id)).where(model.published_at.is_(None))
    ) or 0

    # By source
    src_rows = await db.execute(
        select(model.source_name, func.count(model.id).label("cnt"))
        .group_by(model.source_name)
        .order_by(desc("cnt"))
        .limit(20)
    )
    by_source = [{"source": r[0] or "(unknown)", "count": r[1]} for r in src_rows]

    # By year
    year_rows = await db.execute(
        select(
            func.extract("year", model.published_at).label("yr"),
            func.count(model.id).label("cnt"),
        )
        .where(model.published_at.isnot(None))
        .group_by("yr")
        .order_by(desc("yr"))
    )
    by_year = [{"year": int(r[0]), "count": r[1]} for r in year_rows if r[0] is not None]

    return ArticleStat(
        total=total,
        embedded=embedded,
        unembedded=unembedded,
        with_body=with_body,
        null_published_at=null_pub,
        by_source=by_source,
        by_year=by_year,
    )


@router.get("/articles/{sport}", response_model=list[ArticleOut])
async def list_articles(
    sport: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
    search: str = Query("", max_length=200),
    source: str = Query("", max_length=100),
    category: str = Query("", max_length=50),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """List articles with optional search and filters."""
    model = _ARTICLE_MODELS.get(sport)
    if not model:
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")

    query = select(model)

    if search:
        like = f"%{search}%"
        query = query.where(
            model.title.ilike(like)
            | model.source_name.ilike(like)
            | model.author.ilike(like)
            | model.category.ilike(like)
        )
    if source:
        query = query.where(model.source_name == source)
    if category:
        query = query.where(model.category == category)

    query = query.order_by(desc(model.published_at)).offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.delete("/articles/{sport}/{article_id}", status_code=204)
async def delete_article(
    sport: str,
    article_id: int,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single article by ID."""
    model = _ARTICLE_MODELS.get(sport)
    if not model:
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")

    result = await db.execute(select(model).where(model.id == article_id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    await db.delete(article)
    await db.commit()


# ── Prediction Models ────────────────────────────────────────────────


_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "models")


@router.get("/models/{sport}", response_model=SportModelDetailOut)
async def get_sport_model(
    sport: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Return detailed breakdown of a sport's prediction model."""
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}. Choose mlb, nfl, or nba")

    if sport == "mlb":
        return await _get_mlb_model_detail()
    elif sport == "nfl":
        return _get_nfl_model_detail()
    else:
        return _get_nba_model_detail()


@router.get("/training-runs/{sport}")
async def get_training_runs(
    sport: str,
    admin: User = Depends(get_admin_user),
    limit: int = 20,
):
    """Return the most recent training runs for a sport."""
    from app.handicapping.db_training import get_all_training_runs
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")
    if limit < 1:
        limit = 20
    elif limit > 100:
        limit = 100
    runs = get_all_training_runs(sport, limit=limit)
    return runs


@router.get("/training-runs/{sport}/{model_type}")
async def get_training_runs_for_model(
    sport: str,
    model_type: str,
    admin: User = Depends(get_admin_user),
    limit: int = 10,
):
    """Return the most recent training runs for a specific sport+model_type."""
    from app.handicapping.db_training import get_all_training_runs_for_model_type
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")
    if limit < 1:
        limit = 10
    elif limit > 100:
        limit = 100
    runs = get_all_training_runs_for_model_type(sport, model_type, limit=limit)
    return runs


@router.get("/training-runs/{sport}/{model_type}/{run_id}")
async def get_training_run_detail(
    sport: str,
    model_type: str,
    run_id: int,
    admin: User = Depends(get_admin_user),
):
    """Return the full details (including results_json) for a specific training run."""
    from app.handicapping.db_training import get_training_run
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")
    run = get_training_run(sport, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Training run not found")
    return run


@router.post("/training-runs/{sport}/{model_type}/{run_id}/set-current")
async def set_training_run_current(
    sport: str,
    model_type: str,
    run_id: int,
    admin: User = Depends(get_admin_user),
):
    """Set a specific training run as the current (production) model."""
    from app.handicapping.db_training import set_training_run_as_current
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")
    result = set_training_run_as_current(sport, run_id)
    if not result:
        raise HTTPException(status_code=404, detail="Training run not found")
    return {"status": "ok", "training_run": result}


@router.post("/training-runs/{sport}/{model_type}/{run_id}/set-live")
async def set_training_run_live(
    sport: str,
    model_type: str,
    run_id: int,
    admin: User = Depends(get_admin_user),
):
    """Set a specific training run as the live (active prediction) model."""
    from app.handicapping.db_training import set_training_run_as_live
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")
    result = set_training_run_as_live(sport, run_id)
    if not result:
        raise HTTPException(status_code=404, detail="Training run not found")
    return {"status": "ok", "training_run": result}


@router.get("/features/{sport}")
async def get_mlb_features(
    sport: str,
    admin: User = Depends(get_admin_user),
):
    """Return all features for a sport from the features table."""
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")
    conn = _pg_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT name, description, display_name, is_trainable, pick_card, current_ou, "
                        f"current_ats, pick_card_section, sort_order, created_at FROM {sport}.features "
                        f"ORDER BY sort_order NULLS LAST, display_name, name")
            rows = cur.fetchall()
            return {"features": [dict(r) for r in rows]}
    finally:
        conn.close()


class FeatureUpdate(BaseModel):
    """Editable fields on a Features row. Only provided fields are updated."""
    display_name: Optional[str] = None
    description: Optional[str] = None
    is_trainable: Optional[bool] = None
    pick_card: Optional[bool] = None
    pick_card_section: Optional[str] = None
    sort_order: Optional[int] = None


# Valid values for pick_card_section (aligned with the Features admin dropdown and
# the Detailed Analysis -> Stats renderer sections).
PICK_CARD_SECTIONS = ("home_stats", "away_stats", "game_context", "betting_lines", "other")


@router.patch("/features/{sport}/{feature_name}")
async def update_sport_feature(
    sport: str,
    feature_name: str,
    body: FeatureUpdate,
    admin: User = Depends(get_admin_user),
):
    """Update editable fields (display_name, description, is_trainable, pick_card) on one feature."""
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")
    if feature_name is None or not feature_name.strip():
        raise HTTPException(status_code=400, detail="feature_name is required")

    # Map request fields to column names. sport + column identifiers are
    # allowlisted/static here (never user-controlled interpolated directly into SQL).
    if body.pick_card_section is not None and body.pick_card_section not in PICK_CARD_SECTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"pick_card_section must be one of {', '.join(PICK_CARD_SECTIONS)}",
        )

    fields = {
        "display_name": body.display_name,
        "description": body.description,
        "is_trainable": body.is_trainable,
        "pick_card": body.pick_card,
        "pick_card_section": body.pick_card_section,
        "sort_order": body.sort_order,
    }
    sets = {k: v for k, v in fields.items() if v is not None}
    if not sets:
        raise HTTPException(status_code=400, detail="No editable fields provided")

    # Validate feature exists first (return 404 if not).
    conn = _pg_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT name FROM {sport}.features WHERE name = %s",
                (feature_name,),
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail=f"Feature '{feature_name}' not found in {sport}.features")

            assignments = ", ".join(f"{k} = %s" for k in sets)
            params = list(sets.values())
            params.append(feature_name)
            cur.execute(
                f"UPDATE {sport}.features SET {assignments} WHERE name = %s",
                params,
            )
        conn.commit()
        return {"updated": feature_name, "fields": list(sets.keys())}
    finally:
        conn.close()


class FeatureReorder(BaseModel):
    sport: str
    a: str
    b: str
    aPatch: Optional[dict] = None
    bPatch: Optional[dict] = None


@router.post("/features/reorder")
async def reorder_sport_features(
    body: FeatureReorder,
    admin: User = Depends(get_admin_user),
):
    """Swap the display sort_order of two features (single txn).

    Used by the admin Features page to reorder features for the Detailed
    Analysis Stats frontend. Only ever touches the sort_order column.
    """
    sport = body.sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")
    if not body.a or not body.b or body.a == body.b:
        raise HTTPException(status_code=400, detail="Must provide two distinct feature names")

    # Handing the client raw dicts through is unnecessary; we only swap
    # sort_order values. Ignore aPatch/bPatch payloads and compute from DB.
    conn = _pg_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT name, sort_order FROM {sport}.features WHERE name IN (%s, %s)",
                (body.a, body.b),
            )
            rows = cur.fetchall()
            if len(rows) != 2:
                raise HTTPException(
                    status_code=404,
                    detail=f"One or both features not found: {body.a}, {body.b}",
                )
            order_a = (next(r for r in rows if r["name"] == body.a))["sort_order"]
            order_b = (next(r for r in rows if r["name"] == body.b))["sort_order"]
            # If either is unset (NULL), derive from current DB ordering so the
            # swap stays deterministic: assign BOTH an explicit order by their
            # relative position if they differ; if equal (both NULL), give the
            # first a, the second b+1 so a precedes b after this call.
            if order_a is None and order_b is None:
                base = 0
                cur.execute(f"SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM {sport}.features")
                base = cur.fetchone()["n"] or 0
                cur.execute(f"UPDATE {sport}.features SET sort_order = %s WHERE name = %s", (base, body.a))
                cur.execute(f"UPDATE {sport}.features SET sort_order = %s WHERE name = %s", (base + 10, body.b))
            elif order_a is None:
                cur.execute(f"UPDATE {sport}.features SET sort_order = %s WHERE name = %s", (order_b, body.a))
                cur.execute(f"UPDATE {sport}.features SET sort_order = %s WHERE name = %s", (order_a, body.b))
            else:
                cur.execute(f"UPDATE {sport}.features SET sort_order = %s WHERE name = %s", (order_b, body.a))
                cur.execute(f"UPDATE {sport}.features SET sort_order = %s WHERE name = %s", (order_a, body.b))
            # Normalize: reassign sequential 0..N-1 ordered by sort_order so
            # swaps never leave gaps that reorder later rows unintuitively.
            cur.execute(
                f"SELECT name FROM {sport}.features ORDER BY sort_order NULLS LAST, name"
            )
            names = [r["name"] for r in cur.fetchall()]
            for i, n in enumerate(names):
                cur.execute(f"UPDATE {sport}.features SET sort_order = %s WHERE name = %s", (i, n))
        conn.commit()
        return {"swapped": [body.a, body.b]}
    finally:
        conn.close()


@router.post("/train-new/{sport}/{model_type}")
async def trigger_training(
    sport: str,
    model_type: str,
    body: dict,
    admin: User = Depends(get_admin_user),
):
    """Update features for a model type and kick off training."""
    import subprocess
    import json

    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")
    if model_type not in ("ou", "ats"):
        raise HTTPException(status_code=400, detail="model_type must be 'ou' or 'ats'")

    feature_names: list[str] = body.get("features", [])
    if not feature_names:
        raise HTTPException(status_code=400, detail="features list cannot be empty")

    col = "current_ou" if model_type == "ou" else "current_ats"
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            # Clear the column for all features
            cur.execute(f"UPDATE {sport}.features SET {col} = FALSE")
            # Set it for the selected features
            placeholders = ",".join("%s" for _ in feature_names)
            cur.execute(
                f"UPDATE {sport}.features SET {col} = TRUE WHERE name IN ({placeholders})",
                feature_names,
            )
        conn.commit()
    finally:
        conn.close()

    # Launch the training script in the background
    _scripts = {
        "nfl": {
            "ou": "python3 -m app.handicapping.nfl.nfl_xgb_model_ou train",
            "ats": "python3 -m app.handicapping.nfl.nfl_xgb_model_ats train",
        },
        "mlb": {
            "ou": "python3 -m app.handicapping.mlb.mlb_xgb_model_ou --mode all",
            "ats": "python3 -m app.handicapping.mlb.mlb_xgb_model_ats --mode all",
        },
        "nba": {
            "ou": "python3 -m app.handicapping.nba.nba_xgb_model_ou train",
            "ats": "python3 -m app.handicapping.nba.nba_xgb_model_ats train",
        },
    }
    script = _scripts[sport][model_type]

    # Run as a subprocess — fire and forget
    stderr_log = f"/tmp/train_{sport}_{model_type}.log"
    stderr_fh = open(stderr_log, "w")
    proc = await asyncio.create_subprocess_shell(
        script,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
        cwd="/home/rich/.openclaw/workspace/earl-knows-football/backend",
        env={
            "PYTHONPATH": "/home/rich/.openclaw/workspace/earl-knows-football/backend",
            "PATH": os.environ.get("PATH", ""),
            "DATABASE_URL": os.environ.get("DATABASE_URL", ASYNC_DATABASE_URL),
        },
    )

    return {"status": "ok", "features_updated": len(feature_names), "training_pid": proc.pid, "message": f"Training started for {sport} {model_type} model"}


@router.get("/models/{sport}/from-run/{run_id}")
async def get_model_detail_from_run(
    sport: str,
    run_id: int,
    admin: User = Depends(get_admin_user),
):
    """Build a model variant from a specific training run's data."""
    from app.handicapping.db_training import get_training_run
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")

    run = get_training_run(sport, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Training run not found")

    results = run.get("results_json")
    if not results:
        raise HTTPException(status_code=404, detail="Training run has no results")

    # Extract the results list
    if isinstance(results, dict) and "results" in results:
        results = results["results"]
    elif not isinstance(results, list):
        results = [results]

    model_type = run.get("model_type", "ats")
    name_map = {"ou": "O/U", "ats": "ATS", "ml": "ML"}
    variant_name = name_map.get(model_type, model_type.upper())

    # Build the variant using the sport-appropriate function
    if sport == "nfl":
        variant = _build_nfl_model_variant(results, variant_name, model_type)
    else:
        variant = _build_generic_model_variant(results, variant_name, model_type, sport=sport)

    if not variant:
        return {"error": "Could not build model variant from this run's data"}

    return {
        "run": {
            "id": run.get("id"),
            "model_type": run.get("model_type"),
            "training_id": run.get("training_id"),
            "trained_at": run.get("trained_at"),
            "is_current": run.get("is_current"),
            "pkl_filename": run.get("pkl_filename"),
            "algorithm": run.get("algorithm"),
            "description": run.get("description"),
        },
        "variant": variant,
    }


def _build_nfl_model_variant(results, name, model_type):
    """Build an NFL model variant from raw result data."""
    return _build_model_variant(
        name,
        results_file=None,
        feature_descriptions=_ATS_DESCRIPTIONS if model_type == "ats" else _OU_DESCRIPTIONS,
        feature_categories_def=_ATS_CATEGORIES if model_type == "ats" else _OU_CATEGORIES,
        results_data=results,
    )


def _build_generic_model_variant(results, name, model_type, sport="mlb"):
    """Build an MLB/NBA model variant from raw result data."""
    if sport == "nba":
        _descriptions_map = {
            "ats": _NBA_ATS_DESCRIPTIONS,
            "ou": _NBA_OU_DESCRIPTIONS,
        }
        _categories_map = {
            "ats": _NBA_ATS_CATEGORIES,
            "ou": _NBA_OU_CATEGORIES,
        }
    else:
        _descriptions_map = {
            "ats": _MLB_ATS_DESCRIPTIONS,
            "ou": _MLB_OU_DESCRIPTIONS,
            "ml": _MLB_ML_DESCRIPTIONS,
        }
        _categories_map = {
            "ats": _MLB_ATS_CATEGORIES,
            "ou": _MLB_OU_CATEGORIES,
            "ml": _MLB_ML_CATEGORIES,
        }
    return _build_mlb_model_variant(
        name,
        results,
        _descriptions_map.get(model_type, {}),
        _categories_map.get(model_type, {}),
    )


@router.get("/prediction-stats/{sport}")
async def get_prediction_stats(
    sport: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Return per-year prediction stats from the database for a sport."""
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")

    schema = {"nfl": "nfl", "nba": "nba", "mlb": "mlb"}[sport]

    # Query per-year stats from game_predictions
    # MLB uses run_line_result, NFL/NBA use ats_result
    from sqlalchemy import text as _sa_text

    rl_col = "run_line_result" if sport == "mlb" else "ats_result"

    rows = await db.execute(_sa_text(f"""
        SELECT s.year,
               COUNT(*) as total,
               COUNT(*) FILTER (WHERE gp.{rl_col} IS NOT NULL) as ats_games,
               COUNT(*) FILTER (WHERE LOWER(gp.{rl_col})='win') as ats_wins,
               COUNT(*) FILTER (WHERE LOWER(gp.{rl_col})='loss') as ats_losses,
               COUNT(*) FILTER (WHERE LOWER(gp.{rl_col})='push') as ats_pushes,
               ROUND(COALESCE(SUM(gp.ats_profit) FILTER (WHERE gp.{rl_col} IS NOT NULL), 0))::int as ats_profit,
               COUNT(*) FILTER (WHERE gp.ou_result IS NOT NULL) as ou_games,
               COUNT(*) FILTER (WHERE LOWER(gp.ou_result)='win') as ou_wins,
               COUNT(*) FILTER (WHERE LOWER(gp.ou_result)='loss') as ou_losses,
               COUNT(*) FILTER (WHERE LOWER(gp.ou_result)='push') as ou_pushes,
               ROUND(COALESCE(SUM(gp.ou_profit) FILTER (WHERE gp.ou_result IS NOT NULL), 0))::int as ou_profit,
               COUNT(*) FILTER (WHERE gp.ml_result IS NOT NULL) as ml_games,
               COUNT(*) FILTER (WHERE LOWER(gp.ml_result)='win') as ml_wins,
               COUNT(*) FILTER (WHERE LOWER(gp.ml_result)='loss') as ml_losses,
               ROUND(COALESCE(SUM(gp.ml_profit) FILTER (WHERE gp.ml_result IS NOT NULL), 0))::int as ml_profit
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        WHERE s.year >= 2022
        GROUP BY s.year
        ORDER BY s.year
    """))

    # ── Pre-loop: compute global per-market confidence brackets from all years ──
    # This ensures consistent bracket labels across all years
    is_mlb = sport == "mlb"
    if is_mlb:
        conf_col = "gp.rl_conf"
        conf_cols = "gp.rl_conf, gp.ml_conf, gp.ou_conf"
        cal_cols = "gp.rl_conf_cal, gp.ml_conf_cal, gp.ou_conf_cal"
    else:
        conf_col = "gp.margin_conf"
        conf_cols = f"gp.margin_conf as rl_conf, gp.ml_conf as ml_conf, gp.ou_conf as ou_conf"
        cal_cols = f"gp.ats_conf_cal as rl_conf_cal, gp.ml_conf_cal, gp.ou_conf_cal"

    all_raw_rows = await db.execute(_sa_text(f"""
        SELECT {cal_cols}
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
        WHERE s.year >= 2022
    """))

    _global_cals = list(all_raw_rows.fetchall())

    # Collect per-type calibrated values so bracket ranges are type-appropriate
    ats_cal_vals, ou_cal_vals, ml_cal_vals = [], [], []
    for cr in _global_cals:
        for cal_field, dest in [
            (getattr(cr, 'rl_conf_cal', None), ats_cal_vals),
            (getattr(cr, 'ou_conf_cal', None), ou_cal_vals),
            (getattr(cr, 'ml_conf_cal', None), ml_cal_vals),
        ]:
            if cal_field is not None:
                cv = float(cal_field)
                if cv == cv:
                    dest.append(cv)

    def _make_bracket_def(cfs):
        if len(cfs) < 2:
            lo, hi = 0.40, 0.60
        else:
            lo = max(min(cfs) - 0.005, 0.0)
            hi = min(max(cfs) + 0.005, 1.0)
            if hi - lo < 0.05:
                lo, hi = 0.40, 0.60
        step = (hi - lo) / 5
        labels = [f"{round((lo + i*step)*100, 1):.1f}-{round((lo + (i+1)*step)*100, 1):.1f}%" for i in range(5)]
        return {"lo": lo, "hi": hi, "step": step, "labels": labels}

    # Overall = combined cal values across all types (for combined breakdown)
    overall_cal_vals = ats_cal_vals + ou_cal_vals + ml_cal_vals

    bkt_defs = {
        "ats": _make_bracket_def(ats_cal_vals),
        "ou": _make_bracket_def(ou_cal_vals),
        "ml": _make_bracket_def(ml_cal_vals),
        "overall": _make_bracket_def(overall_cal_vals),
    }

    yearly_stats = []
    for r in rows.fetchall():
        ats_t = r.ats_wins + r.ats_losses
        ou_t = r.ou_wins + r.ou_losses
        ml_t = r.ml_wins + r.ml_losses
        raw_rows = await db.execute(_sa_text(f"""
            SELECT {conf_col},
                   {conf_cols},
                   {cal_cols},
                   gp.{rl_col} as ats_result, gp.ou_result, gp.ml_result,
                   gp.ats_profit, gp.ou_profit, gp.ml_profit
            FROM (
                SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
                FROM {schema}.game_predictions gp_inner
                ORDER BY gp_inner.game_id, gp_inner.created_at DESC
            ) gp
            JOIN {schema}.games g ON g.id = gp.game_id
            JOIN {schema}.seasons s ON s.id = g.season_id
            WHERE s.year = {r.year}
              AND {conf_col} IS NOT NULL
        """))

        _conf_rows = list(raw_rows.fetchall())

        def _bucket(cf, model_type="ats"):
            if cf is None or (isinstance(cf, float) and cf != cf):
                return None
            bd = bkt_defs.get(model_type)
            if bd is None:
                return None
            labels = bd["labels"]
            if cf >= bd["hi"]:
                return labels[-1]
            if cf <= bd["lo"]:
                return labels[0]
            idx = min(int((cf - bd["lo"]) / bd["step"]), len(labels) - 1)
            if idx < 0:
                idx = 0
            return labels[idx]

        all_raw = [(float(getattr(cr, 'margin_conf', getattr(cr, 'rl_conf', 0.50))),
                     float(cr.rl_conf) if cr.rl_conf is not None else 0.50,
                     float(cr.ml_conf) if cr.ml_conf is not None else 0.50,
                     float(cr.ou_conf) if cr.ou_conf is not None else 0.50,
                     cr.ats_result, cr.ou_result, cr.ml_result,
                     cr.ats_profit or 0, cr.ou_profit or 0, cr.ml_profit or 0,
                     float(cr.rl_conf_cal) if cr.rl_conf_cal is not None else None,
                     float(cr.ml_conf_cal) if cr.ml_conf_cal is not None else None,
                     float(cr.ou_conf_cal) if cr.ou_conf_cal is not None else None)
                    for cr in _conf_rows]

        # Build per-model breakdowns
        def _build_breakdown(model_type, result_field):
            """model_type: 'ats','ou','ml' | result_field: 'ats_result','ou_result','ml_result'"""
            # Map model_type to confidence column indices in all_raw
            # Indices: 0=overall, 1=rl_conf, 2=ml_conf, 3=ou_conf, 10=rl_conf_cal, 11=ml_conf_cal, 12=ou_conf_cal
            raw_idx = {"ats": 1, "ml": 2, "ou": 3}[model_type]
            cal_idx = {"ats": 10, "ml": 11, "ou": 12}[model_type]
            buckets = {}
            for row in all_raw:
                # Use calibrated confidence if available, otherwise raw
                # NaN check: DB stores NaN as float, not NULL — must catch
                # both NULL and NaN, and fall back to raw then default 0.50.
                # About 1,400 rows have NaN in BOTH cal and raw columns.
                cal = row[cal_idx]
                raw = row[raw_idx]
                if cal is not None and not (isinstance(cal, float) and cal != cal):
                    cf = cal
                elif raw is not None and not (isinstance(raw, float) and raw != raw):
                    cf = raw
                else:
                    cf = 0.50
                bk = _bucket(cf, model_type)
                if bk is None:
                    continue
                if bk not in buckets:
                    buckets[bk] = {model_type+"_w": 0, model_type+"_l": 0, "total": 0, "pushes": 0, "profit": 0}
                res = {"ats": row[4], "ou": row[5], "ml": row[6]}[result_field]
                pf = {"ats": row[7], "ou": row[8], "ml": row[9]}[result_field]
                # Normalize result to canonical Win/Loss/Push (all sports now
                # store the OUTCOME, not the bet side).
                if res:
                    res = res.strip().title()
                if res == "Win":
                    buckets[bk][model_type+"_w"] += 1
                    buckets[bk]["total"] += 1
                    if pf is not None:
                        buckets[bk]["profit"] += pf
                elif res == "Loss":
                    buckets[bk][model_type+"_l"] += 1
                    buckets[bk]["total"] += 1
                    if pf is not None:
                        buckets[bk]["profit"] += pf
                elif res == "Push":
                    buckets[bk]["pushes"] += 1
                    buckets[bk]["total"] += 1

            out = []
            m_labels = bkt_defs[model_type]["labels"]
            for bk in m_labels:
                b = buckets.get(bk)
                if not b or b["total"] == 0:
                    continue
                denom = b[model_type+"_w"] + b[model_type+"_l"]  # exclude pushes from accuracy
                out.append({
                    "bracket": bk,
                    "total": b["total"],
                    "correct": b[model_type+"_w"],
                    "incorrect": b[model_type+"_l"],
                    "pushes": b["pushes"],
                    "pct": round(100 * b[model_type+"_w"] / max(denom, 1), 1),
                    "profit": round(b["profit"], 1) if b["profit"] == b["profit"] else 0.0,
                })
            return out

        confidence_ats = _build_breakdown("ats", "ats")
        confidence_ou  = _build_breakdown("ou", "ou")
        confidence_ml  = _build_breakdown("ml", "ml")

        # Overall breakdown: binned by margin_conf, shows all three models
        def _build_overall_breakdown():
            buckets = {}
            for row in all_raw:
                # Use calibrated confidence when available, fall back to raw
                mc = row[10] if row[10] is not None and not (isinstance(row[10], float) and row[10] != row[10]) else row[0]
                ats_r, ou_r, ml_r = row[4], row[5], row[6]
                ats_p, ou_p, ml_p = row[7], row[8], row[9]
                # Normalize results to canonical Win/Loss/Push (all sports)
                if ats_r: ats_r = ats_r.strip().title()
                if ou_r: ou_r = ou_r.strip().title()
                if ml_r: ml_r = ml_r.strip().title()
                bk = _bucket(mc, "overall")
                if bk is None:
                    continue
                if bk not in buckets:
                    buckets[bk] = {"total": 0, "ats_w": 0, "ats_l": 0, "ats_p": 0,
                                   "ou_w": 0, "ou_l": 0, "ou_p": 0,
                                   "ml_w": 0, "ml_l": 0, "ml_p": 0}
                buckets[bk]["total"] += 1
                for res, k, pf in [(ats_r, "ats", ats_p), (ou_r, "ou", ou_p), (ml_r, "ml", ml_p)]:
                    if res == "Win": buckets[bk][k+"_w"] += 1; buckets[bk][k+"_p"] += pf
                    elif res == "Loss": buckets[bk][k+"_l"] += 1; buckets[bk][k+"_p"] += pf
            out = []
            m_labels = bkt_defs["overall"]["labels"]
            for bk in m_labels:
                b = buckets.get(bk)
                if not b or b["total"] == 0:
                    continue
                out.append({
                    "bracket": bk,
                    "total": b["total"],
                    "ats": {"correct": b["ats_w"], "incorrect": b["ats_l"], "total": b["ats_w"]+b["ats_l"],
                            "pct": round(100 * b["ats_w"] / max(b["ats_w"]+b["ats_l"], 1), 1),
                            "profit": round(b["ats_p"], 1)},
                    "ou": {"correct": b["ou_w"], "incorrect": b["ou_l"], "total": b["ou_w"]+b["ou_l"],
                           "pct": round(100 * b["ou_w"] / max(b["ou_w"]+b["ou_l"], 1), 1),
                           "profit": round(b["ou_p"], 1)},
                    "ml": {"correct": b["ml_w"], "incorrect": b["ml_l"], "total": b["ml_w"]+b["ml_l"],
                           "pct": round(100 * b["ml_w"] / max(b["ml_w"]+b["ml_l"], 1), 1),
                           "profit": round(b["ml_p"], 1)},
                })
            return out

        ats_roi = round(100 * r.ats_profit / max(ats_t * 110, 1), 1) if ats_t else 0
        ou_roi = round(100 * r.ou_profit / max(ou_t * 110, 1), 1) if ou_t else 0
        ml_roi = round(100 * r.ml_profit / max(ml_t * 100, 1), 1) if ml_t else 0

        yearly_stats.append({
            "year": r.year,
            "total_games": r.total,
            "confidence_breakdown": {
                "overall": _build_overall_breakdown(),
                "ats": confidence_ats,
                "ou": confidence_ou,
                "ml": confidence_ml,
            },
            "ats": {
                "correct": r.ats_wins, "incorrect": r.ats_losses, "pushes": r.ats_pushes,
                "total": ats_t, "pct": round(100 * r.ats_wins / max(ats_t, 1), 1),
                "profit": r.ats_profit, "roi": ats_roi,
            },
            "ou": {
                "correct": r.ou_wins, "incorrect": r.ou_losses, "pushes": r.ou_pushes,
                "total": ou_t, "pct": round(100 * r.ou_wins / max(ou_t, 1), 1),
                "profit": r.ou_profit, "roi": ou_roi,
            },
            "ml": {
                "correct": r.ml_wins, "incorrect": r.ml_losses, "pushes": 0,
                "total": ml_t, "pct": round(100 * r.ml_wins / max(ml_t, 1), 1),
                "profit": r.ml_profit, "roi": ml_roi,
            },
        })

    # Show 2022 onwards for NFL (2021 was a debug/training year)
    if sport == "nfl":
        yearly_stats = [s for s in yearly_stats if s["year"] >= 2022]

    # Aggregate totals
    ats_c = sum(s["ats"]["correct"] for s in yearly_stats)
    ats_i = sum(s["ats"]["incorrect"] for s in yearly_stats)
    ats_p = sum(s["ats"]["profit"] for s in yearly_stats)
    ou_c = sum(s["ou"]["correct"] for s in yearly_stats)
    ou_i = sum(s["ou"]["incorrect"] for s in yearly_stats)
    ou_p = sum(s["ou"]["profit"] for s in yearly_stats)
    ml_c = sum(s["ml"]["correct"] for s in yearly_stats)
    ml_i = sum(s["ml"]["incorrect"] for s in yearly_stats)
    ml_p = sum(s["ml"]["profit"] for s in yearly_stats)
    ats_t = ats_c + ats_i
    ou_t = ou_c + ou_i
    ml_t = ml_c + ml_i

    overall = {
        "ats": {"correct": ats_c, "incorrect": ats_i, "pct": round(100 * ats_c / max(ats_t, 1), 1),
                "profit": ats_p, "roi": round(100 * ats_p / max(ats_t * 110, 1), 1)},
        "ou": {"correct": ou_c, "incorrect": ou_i, "pct": round(100 * ou_c / max(ou_t, 1), 1),
                "profit": ou_p, "roi": round(100 * ou_p / max(ou_t * 110, 1), 1)},
        "ml": {"correct": ml_c, "incorrect": ml_i, "pct": round(100 * ml_c / max(ml_t, 1), 1),
                "profit": ml_p, "roi": round(100 * ml_p / max(ml_t * 100, 1), 1)},
    }

    return {"sport": sport, "yearly": yearly_stats, "overall": overall}


@router.get("/prediction-stats/{sport}/calibration")
async def get_prediction_calibration(
    sport: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Return granular calibration data for each pick type.

    Buckets predictions into 20 uniform confidence bins (0.50 to 1.00)
    and returns win rate + volume per bin for ATS, O/U, and ML.
    """
    sport = sport.lower()
    if sport not in ("mlb", "nfl", "nba"):
        raise HTTPException(status_code=404, detail=f"Unknown sport: {sport}")

    schema = {"nfl": "nfl", "nba": "nba", "mlb": "mlb"}[sport]
    from sqlalchemy import text as _sa_text

    rl_col = "run_line_result" if sport == "mlb" else "ats_result"
    is_mlb = sport == "mlb"

    if is_mlb:
        conf_cols = "gp.rl_conf, gp.ml_conf, gp.ou_conf"
        conf_col = "gp.rl_conf"
        conf_main = "rl_conf"
    else:
        conf_cols = f"gp.ats_conf_cal as rl_conf, gp.ml_conf_cal as ml_conf, gp.ou_conf_cal as ou_conf"
        conf_col = "gp.ats_conf_cal"
        conf_main = "rl_conf"

    rows = await db.execute(_sa_text(f"""
        SELECT
            {conf_col},
            {conf_cols},
            gp.{rl_col} as ats_result, gp.ou_result, gp.ml_result,
            gp.ats_profit, gp.ou_profit, gp.ml_profit,
            gp.ats_odds, gp.ou_odds, gp.ml_odds
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
          AND {conf_col} IS NOT NULL
    """))

    # ── First pass: collect all rows and find min/max of confidence values ──
    _all_rows = list(rows.fetchall())
    # Collect per-type confidence values so bin ranges are type-appropriate
    ats_cf_list, ou_cf_list, ml_cf_list = [], [], []
    for r in _all_rows:
        mc = float(getattr(r, conf_main, 0.50)) if getattr(r, conf_main, None) is not None else 0.50
        rc = float(r.rl_conf) if r.rl_conf is not None else mc
        oc = float(r.ou_conf) if r.ou_conf is not None else mc
        mcl = float(r.ml_conf) if r.ml_conf is not None else mc
        if rc == rc:
            ats_cf_list.append(rc)
        if oc == oc:
            ou_cf_list.append(oc)
        if mcl == mcl:
            ml_cf_list.append(mcl)

    def _make_bins(cfs: list[float]) -> tuple:
        """Create 5 uniform bins + bucket_index closed over those bins.

        Returns (bins, bucket_fn) where bins is the empty bin dict list
        and bucket_fn maps a confidence value to a bin index.
        """
        if len(cfs) < 2:
            _min, _max = 0.40, 0.60
        else:
            _min = max(min(cfs) - 0.02, 0.0)
            _max = min(max(cfs) + 0.02, 1.0)
            if _max - _min < 0.05:
                _min, _max = 0.40, 0.60

        _count = 5
        _step = (_max - _min) / _count

        bins = []
        for i in range(_count):
            lo = round(_min + i * _step, 3)
            hi = round(_min + (i + 1) * _step, 3)
            bins.append({
                "bin_lo": lo, "bin_hi": hi,
                "label": f"{lo*100:.0f}-{hi*100:.0f}%",
                "total": 0, "wins": 0, "losses": 0, "pushes": 0,
                "profit": 0.0,
                "fwd_ev_sum": 0.0,
                "odds_sum": 0.0,
            })

        def _bucket_index(cf: float) -> int:
            if cf is None or cf != cf:
                return 0
            if cf >= _max:
                return _count - 1
            if cf <= _min:
                return 0
            idx = int((cf - _min) / _step)
            return min(idx, _count - 1)

        return bins, _bucket_index

    def _profit_per_100(odds: int | None) -> float:
        """Profit on a $100 flat bet at given odds.

        At -110: risk $100, win $90.91
        At +150: risk $100, win $150
        At -200: risk $100, win $50
        """
        if not odds:
            return 0.0
        if odds < 0:
            return 100.0 * 100.0 / float(abs(odds))  # e.g. -110 → 90.91
        else:
            return float(odds)  # e.g. +150 → 150.0

    def _fwd_ev(confidence: float, odds: int | None) -> float:
        """Forward-looking EV per $100 bet.

        Uses the model's stated confidence (may be miscalibrated).
        EV = (conf × profit$100) - ((1 - conf) × $100)
        """
        if not odds or confidence <= 0 or confidence >= 1:
            return 0.0
        profit = _profit_per_100(odds)
        return (confidence * profit) - ((1.0 - confidence) * 100.0)

    # Build per-type bins + bucket functions from their own confidence ranges
    ats_cf_bins, ats_bucket = _make_bins(ats_cf_list)
    ou_cf_bins, ou_bucket = _make_bins(ou_cf_list)
    ml_cf_bins, ml_bucket = _make_bins(ml_cf_list)

    for r in _all_rows:
        mc = float(getattr(r, conf_main, 0.50)) if getattr(r, conf_main, None) is not None else 0.50
        rl_cf = float(r.rl_conf) if r.rl_conf is not None else mc
        ml_cf = float(r.ml_conf) if r.ml_conf is not None else mc
        ou_cf = float(r.ou_conf) if r.ou_conf is not None else mc

        ats_r = r.ats_result
        ou_r = r.ou_result
        ml_r = r.ml_result
        # Normalize result strings to canonical Win/Loss/Push (all sports)
        if ats_r: ats_r = ats_r.strip().title()
        if ml_r: ml_r = ml_r.strip().title()
        if ou_r: ou_r = ou_r.strip().title()
        ats_p = float(r.ats_profit or 0)
        ou_p = float(r.ou_profit or 0)
        ml_p = float(r.ml_profit or 0)
        ats_odds = int(r.ats_odds) if r.ats_odds is not None else None
        ou_odds = int(r.ou_odds) if r.ou_odds is not None else None
        ml_odds = int(r.ml_odds) if r.ml_odds is not None else None

        # Forward-looking EV (uses model's stated confidence)
        ats_fwd = _fwd_ev(rl_cf, ats_odds) if ats_odds else 0.0
        ou_fwd = _fwd_ev(ou_cf, ou_odds) if ou_odds else 0.0
        ml_fwd = _fwd_ev(ml_cf, ml_odds) if ml_odds else 0.0

        # Profit per $100 for calibrated EV
        ats_profit_odds = _profit_per_100(ats_odds) if ats_odds else 0.0
        ou_profit_odds = _profit_per_100(ou_odds) if ou_odds else 0.0
        ml_profit_odds = _profit_per_100(ml_odds) if ml_odds else 0.0

        # ATS — bucket by rl_conf (or margin_conf) — type-specific bins
        bi = ats_bucket(rl_cf)
        ats_cf_bins[bi]["total"] += 1
        ats_cf_bins[bi]["fwd_ev_sum"] += ats_fwd
        ats_cf_bins[bi]["odds_sum"] += ats_profit_odds
        if ats_r == "Win":
            ats_cf_bins[bi]["wins"] += 1
            ats_cf_bins[bi]["profit"] += ats_p
        elif ats_r == "Loss":
            ats_cf_bins[bi]["losses"] += 1
            ats_cf_bins[bi]["profit"] += ats_p
        elif ats_r == "Push":
            ats_cf_bins[bi]["pushes"] += 1

        # OU — bucket by ou_conf — type-specific bins
        bi = ou_bucket(ou_cf)
        ou_cf_bins[bi]["total"] += 1
        ou_cf_bins[bi]["fwd_ev_sum"] += ou_fwd
        ou_cf_bins[bi]["odds_sum"] += ou_profit_odds
        if ou_r == "Win":
            ou_cf_bins[bi]["wins"] += 1
            ou_cf_bins[bi]["profit"] += ou_p
        elif ou_r == "Loss":
            ou_cf_bins[bi]["losses"] += 1
            ou_cf_bins[bi]["profit"] += ou_p
        elif ou_r == "Push":
            ou_cf_bins[bi]["pushes"] += 1

        # ML — bucket by ml_conf — type-specific bins
        bi = ml_bucket(ml_cf)
        ml_cf_bins[bi]["total"] += 1
        ml_cf_bins[bi]["fwd_ev_sum"] += ml_fwd
        ml_cf_bins[bi]["odds_sum"] += ml_profit_odds
        if ml_r == "Win":
            ml_cf_bins[bi]["wins"] += 1
            ml_cf_bins[bi]["profit"] += ml_p
        elif ml_r == "Loss":
            ml_cf_bins[bi]["losses"] += 1
            ml_cf_bins[bi]["profit"] += ml_p

    def _finalize(bins: list) -> list:
        """Compute win pct, avg odds, and two EV metrics per bin.

        Two EV metrics:
        1. avg_fwd_ev — Forward-looking EV using model's stated confidence.
           If the model says 90% but wins 50%, fwd_ev will be wildly wrong.
           This measures calibration quality.

        2. avg_cal_ev — Calibrated EV using the bin's actual win rate.
           EV = (win_rate × avg_profit_per_$100) - (loss_rate × $100)
           This tells you the REAL expected value of picks in this bin.
           If the bin's total profit is negative, cal_ev will also be negative.
        """
        out = []
        for b in bins:
            denom = b["wins"] + b["losses"]
            win_rate = b["wins"] / max(denom, 1)
            b["win_rate"] = round(100 * win_rate, 1)
            b["profit"] = round(b["profit"], 1)

            # Average profit per $100 bet across picks in this bin
            avg_profit = b["odds_sum"] / max(b["total"], 1)
            b["avg_profit_odds"] = round(avg_profit, 2)

            # Forward-looking EV (uses model's stated confidence via fwd_ev_sum per pick)
            b["avg_fwd_ev"] = round(b["fwd_ev_sum"] / max(b["total"], 1), 2)

            # Calibrated EV (uses actual bin win rate × average odds)
            # EV = (win_rate × avg_profit) - (loss_rate × 100)
            b["avg_cal_ev"] = round(
                (win_rate * avg_profit) - ((1.0 - win_rate) * 100.0), 2
            )

            del b["fwd_ev_sum"]
            del b["odds_sum"]
            out.append(b)
        return out

    logger.info(
        "ATS bins: %s | OU bins: %s | ML bins: %s",
        [b["label"] for b in ats_cf_bins],
        [b["label"] for b in ou_cf_bins],
        [b["label"] for b in ml_cf_bins],
    )

    return {
        "sport": sport,
        "ats": _finalize(ats_cf_bins),
        "ou": _finalize(ou_cf_bins),
        "ml": _finalize(ml_cf_bins),
    }


@router.get("/prediction-stats/{sport}/ev-distribution")
async def get_prediction_ev_distribution(
    sport: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Return distribution of picks by forward-looking EV score.

    Buckets picks by raw confidence (ats_conf, ou_conf, ml_conf),
    computes empirical win rate per confidence bin, then derives
    EV from (win_rate × profit_per_unit) - (loss_rate × 1_unit).
    """
    from sqlalchemy import text as _sa_text

    # Resolve sport config inlined
    if sport == "nfl":
        conf_main = "ats_conf_cal"
        schema, use_ats, use_ml = "nfl", True, True
        conf_ats = "ats_conf_cal"; conf_ml = "ml_conf_cal"; conf_ou = "ou_conf_cal"
        rl_col = "ats_result"
    elif sport == "nba":
        conf_main = "ats_conf_cal"
        schema, use_ats, use_ml = "nba", True, True
        conf_ats = "ats_conf_cal"; conf_ml = "ml_conf_cal"; conf_ou = "ou_conf_cal"
        rl_col = "ats_result"
    else:
        schema, use_ats, use_ml = "mlb", True, True
        conf_ats = "rl_conf"; conf_ml = "ml_conf"; conf_ou = "ou_conf"
        conf_main = "rl_conf"
        rl_col = "run_line_result"

    # Step 1: Load raw picks with confidence + odds
    rows = await db.execute(_sa_text(f"""
        SELECT
            gp.{conf_main},
            gp.{conf_ats} as rl_conf,
            gp.{conf_ou} as ou_conf,
            gp.{conf_ml} as ml_conf,
            gp.{rl_col} as ats_result, gp.ou_result, gp.ml_result,
            gp.ats_profit, gp.ou_profit, gp.ml_profit,
            gp.ats_odds, gp.ou_odds, gp.ml_odds
        FROM (
            SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
            FROM {schema}.game_predictions gp_inner
            ORDER BY gp_inner.game_id, gp_inner.created_at DESC
        ) gp
        JOIN {schema}.games g ON g.id = gp.game_id
        JOIN {schema}.seasons s ON s.id = g.season_id
          AND gp.{conf_main} IS NOT NULL
    """))

    # ── First pass: collect rows, separate confidence per type ──
    _all_rows = list(rows.fetchall())
    ev_ats_cfs, ev_ou_cfs, ev_ml_cfs = [], [], []
    for r in _all_rows:
        mc = float(getattr(r, conf_main, 0.50)) if getattr(r, conf_main, None) is not None else 0.50
        for cf, dest in [(float(r.rl_conf) if r.rl_conf is not None else mc, ev_ats_cfs),
                         (float(r.ou_conf) if r.ou_conf is not None else mc, ev_ou_cfs),
                         (float(r.ml_conf) if r.ml_conf is not None else mc, ev_ml_cfs)]:
            if cf == cf:
                dest.append(cf)

    def _ev_make_buckets(cfs):
        if len(cfs) < 2:
            _min, _max = 0.40, 0.60
        else:
            _min = max(min(cfs) - 0.02, 0.0)
            _max = min(max(cfs) + 0.02, 1.0)
            if _max - _min < 0.05:
                _min, _max = 0.40, 0.60
        _count = 5
        _step = (_max - _min) / _count
        def _bucket_index(cf: float) -> int:
            if cf is None or cf != cf: return 0
            if cf >= _max: return _count - 1
            if cf <= _min: return 0
            idx = int((cf - _min) / _step)
            return min(idx, _count - 1)
        return _count, _bucket_index

    ev_bin_count, ev_ats_bkt = _ev_make_buckets(ev_ats_cfs)
    _, ev_ou_bkt = _ev_make_buckets(ev_ou_cfs)
    _, ev_ml_bkt = _ev_make_buckets(ev_ml_cfs)

    def _profit_per_100(odds: int | None) -> float:
        if not odds: return 0.0
        profit = (100.0 * 100.0 / float(abs(odds))) if odds < 0 else float(odds)
        return profit

    # Track per-confidence-bin: wins, losses, total for each pick type
    calibrations: dict[str, list[dict]] = {
        "ats": [{"w": 0, "l": 0} for _ in range(ev_bin_count)],
        "ou": [{"w": 0, "l": 0} for _ in range(ev_bin_count)],
        "ml": [{"w": 0, "l": 0} for _ in range(ev_bin_count)] if use_ml else None,
    }
    ev_dist_picks: dict[str, list[dict]] = {
        "ats": [] if use_ats else None,
        "ou": [],
        "ml": [] if use_ml else None,
    }

    for r in _all_rows:
        mc = float(getattr(r, conf_main, 0.50)) if getattr(r, conf_main, None) is not None else 0.50
        rl_cf = float(r.rl_conf) if r.rl_conf is not None else mc
        ml_cf = float(r.ml_conf) if r.ml_conf is not None else mc
        ou_cf = float(r.ou_conf) if r.ou_conf is not None else mc
        # Normalize sport result strings: NBA uses 'win'/'loss'/'push' for ATS, 'over'/'under'/'push' for OU
        ats_r = r.ats_result
        if ats_r: ats_r = ats_r.strip().title()
        ou_r = r.ou_result
        if ou_r: ou_r = ou_r.strip().title()
        ml_r = r.ml_result
        if ml_r: ml_r = ml_r.strip().title()

        # ATS
        bi = ev_ats_bkt(rl_cf)
        if ats_r in ("Win", "Loss"):
            cal = calibrations["ats"][bi]
            if ats_r == "Win": cal["w"] += 1
            else: cal["l"] += 1
        if ats_r is not None and r.ats_odds is not None:
            cal_rate = calibrations["ats"][bi]
            total = cal_rate["w"] + cal_rate["l"]
            wr = cal_rate["w"] / total if total > 0 else 0.50
            profit = _profit_per_100(int(r.ats_odds))
            ev = round((wr * profit) - ((1.0 - wr) * 100.0), 2)
            ev_dist_picks["ats"].append({"result": ats_r, "ev": ev, "r": rl_cf, "profit": float(r.ats_profit or 0)})

        # OU
        bi = ev_ou_bkt(ou_cf)
        if ou_r in ("Win", "Loss"):
            cal = calibrations["ou"][bi]
            if ou_r == "Win": cal["w"] += 1
            else: cal["l"] += 1
        if ou_r is not None:
            cal_rate = calibrations["ou"][bi]
            total = cal_rate["w"] + cal_rate["l"]
            wr = cal_rate["w"] / total if total > 0 else 0.50
            profit = _profit_per_100(-110)
            ev = round((wr * profit) - ((1.0 - wr) * 100.0), 2)
            ev_dist_picks["ou"].append({"result": ou_r, "ev": ev, "r": ou_cf, "profit": float(r.ou_profit or 0)})

        # ML
        if use_ml:
            bi = ev_ml_bkt(ml_cf)
            if ml_r in ("Win", "Loss"):
                cal = calibrations["ml"][bi]
                if ml_r == "Win": cal["w"] += 1
                else: cal["l"] += 1
            if ml_r is not None and r.ml_odds is not None:
                cal_rate = calibrations["ml"][bi]
                total = cal_rate["w"] + cal_rate["l"]
                wr = cal_rate["w"] / total if total > 0 else 0.50
                profit = _profit_per_100(int(r.ml_odds))
                ev = round((wr * profit) - ((1.0 - wr) * 100.0), 2)
                ev_dist_picks["ml"].append({"result": ml_r, "ev": ev, "r": ml_cf, "profit": float(r.ml_profit or 0)})

    # ── Build EV distribution chart ──
    # Bucket picks by their calibrated EV score to show profit per EV range
    BUCKET_RESOLUTION = 5.0  # $5 bucket width
    def _build_ev_distribution(pick_type: str) -> list[dict]:
        picks = ev_dist_picks[pick_type]
        if not picks:
            return []
        evs = [p["ev"] for p in picks]
        mn, mx = min(evs), max(evs)
        # Auto-detect a reasonable step size
        rng = mx - mn
        if rng <= 10: step = 2.5
        elif rng <= 30: step = 5.0
        elif rng <= 60: step = 10.0
        else: step = 25.0
        start = max(-100.0, round(mn / step) * step)
        end = min(100.0, round(mx / step) * step + step)
        num_bins = int((end - start) / step) + 1
        bins = [{"lo": round(start + i * step, 1), "hi": round(start + (i+1) * step, 1),
                 "label": f"{start+i*step:.0f}-{start+(i+1)*step:.0f}",
                 "total": 0, "wins": 0, "losses": 0, "pushes": 0, "profit": 0.0} for i in range(num_bins)]
        for p in picks:
            idx = max(0, min(int((p["ev"] - start) // step), num_bins - 1))
            b = bins[idx]
            b["total"] += 1
            b["profit"] += p.get("profit", 0)
            if p["result"] == "Win": b["wins"] += 1
            elif p["result"] == "Loss": b["losses"] += 1
        out = []
        for b in bins:
            if b["total"] == 0: continue
            out.append({
                "bin_lo": b["lo"], "bin_hi": b["hi"], "label": b["label"],
                "total": b["total"], "wins": b["wins"], "losses": b["losses"], "pushes": b["pushes"],
                "profit": round(b["profit"], 2),
                "win_rate": round(b["wins"] / max(b["total"] - b["pushes"], 1) * 100, 1),
                "roi": round(b["profit"] / max(b["total"], 1), 2),
            })
        return out

    return {
        "ats": _build_ev_distribution("ats") if use_ats else [],
        "ou": _build_ev_distribution("ou"),
        "ml": _build_ev_distribution("ml") if use_ml else [],
    }


async def _get_mlb_model_detail() -> SportModelDetailOut:
    """Build the MLB model detail response with two model variants.

    Currently only the ATS (Run Line) variant has trained data from
    mlb_backtest_results.json. The O/U variant will be populated
    when the dedicated model is trained.
    """
    results = _load_mlb_backtest_results(model_type="ats")

    # Filter out pre-2021 seasons — model accuracy is not relevant for older data
    results = [r for r in (results or []) if r.get("test_year", 0) >= 2021]

    if not results:
        raise HTTPException(
            status_code=503,
            detail="MLB model results not available yet. Run `python -m app.handicapping.mlb_backtest --mode all` first.",
        )

    # ── Build model variants ──
    # Strip OU/ML keys from ATS results — they're calculated by dedicated models only
    for r in results:
        r.pop("ou", None); r.pop("ml", None)
        r.pop("ml_on_ats_subset", None); r.pop("ml_on_ou_subset", None)
    ats_variant = _build_mlb_model_variant("ATS", results, _MLB_ATS_DESCRIPTIONS, _MLB_ATS_CATEGORIES)

    # O/U variant: try loading from dedicated file, otherwise None
    ou_results = _load_mlb_backtest_results("mlb_ou_backtest_results.json", model_type="ou")
    ou_variant = _build_mlb_model_variant("O/U", ou_results, _MLB_OU_DESCRIPTIONS, _MLB_OU_CATEGORIES) if ou_results else None

    model_variants = [v for v in [ats_variant, ou_variant] if v is not None]

    # ── Overall stats (ATS from ATS variant, OU from dedicated variant) ──
    overall_ats_val = ats_variant.overall_ats if ats_variant and ats_variant.overall_ats else ModelBettingOut(correct=0, incorrect=0, total=0, pct=0)
    overall_ou_val = ou_variant.overall_ou if ou_variant and ou_variant.overall_ou else None
    overall_ml_val = None

    # Overall MAE: from ATS variant
    overall_mae = ats_variant.overall_mae if ats_variant else 0

    # Training + test years
    all_train = set()
    all_test = set()
    for v in model_variants:
        for r in v.backtest_results:
            ty = r.get("test_year")
            if ty is not None:
                all_test.add(ty)
            for y in r.get("train_years", []):
                if y is not None:
                    all_train.add(y)

    # Combined features (union across variants)
    combined_features = []
    seen_feats = set()
    for v in model_variants:
        for f in v.features:
            if f.name not in seen_feats:
                seen_feats.add(f.name)
                combined_features.append(f)

    # Feature importance plot (combined avg)
    fi_plot = []
    if model_variants:
        fi_agg: dict[str, float] = {}
        fi_cnt: dict[str, int] = {}
        for v in model_variants:
            for fi in v.feature_importance_plot:
                fi_agg[fi["name"]] = fi_agg.get(fi["name"], 0) + fi["importance"]
                fi_cnt[fi["name"]] = fi_cnt.get(fi["name"], 0) + 1
        sorted_fi = sorted(
            [(n, fi_agg[n] / c) for n, c in fi_cnt.items()],
            key=lambda x: -x[1],
        )
        fi_plot = [{"name": n, "importance": round(imp, 4)} for n, imp in sorted_fi[:15]]

    # Feature categories (combined)
    combined_cats = []
    for v in model_variants:
        for cat in v.feature_categories:
            combined_cats.append(cat)

    # High confidence: each metric from its dedicated model
    ats_hc_data = ats_variant.backtest_results if ats_variant else []
    ou_hc_data = ou_variant.backtest_results if ou_variant else ats_hc_data
    high_conf = _calc_high_confidence_multi(
        ats_results=ats_hc_data,
        ou_results=ou_hc_data,
        ml_results=ats_hc_data,
        threshold_pcts=[25, 20, 15, 10, 5]
    )

    return SportModelDetailOut(
        sport="mlb",
        model_type="XGBoost Run Differential Regressor",
        description=(
            "The MLB prediction model uses XGBoost (eXtreme Gradient Boosting) to predict "
            "run differential (home_score - away_score) for every regular season game. "
            "Features include rolling team stats (runs scored/allowed over 5/10/20 game windows), "
            "home/road splits, rest days, travel distance, betting market implied probabilities, "
            "and situational factors (month, dome, division).\n\n"
            "The model is trained in a rolling year-by-year fashion: to predict year N, it trains "
            "on all available data from 2011 through N-1. This prevents look-ahead bias and "
            "simulates real-world deployment conditions.\n\n"
            "🔵 **ATS Model** — Predicts run differential (run line at -1.5/+1.5). "
            "ATS-optimized with full feature set. Currently the only trained variant.\n\n"
            "🟡 **O/U Model** — Predicts total runs. Not yet trained.\n\n"
            "Select a model variant above to see its specific features and backtest results."
        ),
        algorithm="XGBoost — Two Specialized Variants",
        training_years=sorted(all_train) if all_train else [2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020],
        test_years=sorted(all_test),
        total_features=len(combined_features),
        features=combined_features,
        feature_categories=combined_cats,
        backtest_results=[],  # Individual model results live in model_variants
        overall_mae=overall_mae,
        overall_ats=overall_ats_val,
        overall_ou=overall_ou_val,
        overall_ml=overall_ml_val,
        monthly=[],
        high_confidence=high_conf,
        feature_importance_plot=fi_plot,
        last_updated="2026-06-04",
        model_variants=model_variants,
    )


def _load_json(filename):
    """Load a JSON file from the models directory, returning None on error."""
    import json
    path = os.path.join(_MODELS_DIR, filename)
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _load_training_from_db(sport: str, model_type: str) -> list | dict | None:
    """Load the current training run's results_json from the database.

    Tries the DB first. Returns None if nothing found.
    """
    try:
        from app.handicapping.db_training import get_current_training_run
        run = get_current_training_run(sport, model_type)
        if run and run.get("results_json"):
            return run["results_json"]
    except Exception:
        pass
    return None


def _load_mlb_backtest_results(filename="mlb_backtest_results.json", model_type: str = None):
    """Load MLB backtest results, trying DB first, then JSON file.

    When ``model_type`` is provided (e.g. "ats", "ou", "ml"), the database
    is checked first. Falls back to the legacy JSON file.
    """
    if model_type:
        db_data = _load_training_from_db("mlb", model_type)
        if db_data:
            # The DB stores the combined result dict with "results" key
            if isinstance(db_data, dict) and "results" in db_data:
                return db_data["results"]
            # Or a single year's result (as a dict) – wrap in list for compat
            if isinstance(db_data, dict):
                return [db_data]
            return db_data
    data = _load_json(filename)
    if isinstance(data, dict) and "results" in data:
        return data["results"]
    return data


def _try_db_results(sport: str | None, model_type: str | None) -> list | None:
    """Try to load results from the training_runs DB table.

    Returns the results list (or None if not found/not configured).
    """
    if not sport or not model_type:
        return None
    db_data = _load_training_from_db(sport, model_type)
    if not db_data:
        return None
    # DB may store with a "results" wrapper or as a list/single dict
    if isinstance(db_data, dict) and "results" in db_data:
        return db_data["results"]
    if isinstance(db_data, dict):
        return [db_data]
    if isinstance(db_data, list):
        return db_data
    return None


def _build_mlb_model_variant(name, results_data, feature_descriptions, feature_categories_def,
                              metric_keys=None) -> ModelVariantOut | None:
    """Build a ModelVariantOut from pre-loaded MLB backtest results data (not filename).

    Mirrors _build_model_variant but starts from data instead of a file path,
    since MLB pre-filters results by year before building.
    """
    if not results_data:
        return None
    if metric_keys is None:
        metric_keys = {"ats": "ATS", "ou": "O/U", "ml": "Moneyline"}

    cat_lookup = {}
    for cat, feats in feature_categories_def.items():
        for f in feats:
            cat_lookup[f] = cat

    backtest_results = sorted(results_data, key=lambda x: x.get("test_year", 0))

    # Aggregate feature importances across years
    imp_agg: dict[str, float] = {}
    imp_count: dict[str, int] = {}
    for r in results_data:
        for fi in r.get("feature_importance", []):
            fn = fi["feature"]
            imp_agg[fn] = imp_agg.get(fn, 0) + fi.get("importance", 0)
            imp_count[fn] = imp_count.get(fn, 0) + 1

    sorted_feats = sorted(
        [(n, imp_agg[n] / c) for n, c in imp_count.items()],
        key=lambda x: -x[1],
    )

    features = []
    for feat_name, imp in sorted_feats:
        features.append(ModelFeatureOut(
            name=feat_name,
            description=feature_descriptions.get(feat_name, feat_name),
            importance=round(imp, 4),
            category=cat_lookup.get(feat_name, "Other"),
        ))

    # Backtest results per year
    yearly_backtest = []
    for r in backtest_results:
        entry = {
            "test_year": r.get("test_year"),
            "total_games": r.get("total_games", 0),
            "mae": r.get("mae", 0),
        }
        for mk_key, mk_name in metric_keys.items():
            mk_data = r.get(mk_key, {})
            entry[str(mk_key)] = {
                "total": mk_data.get("total", 0),
                "correct": mk_data.get("correct", 0),
                "incorrect": mk_data.get("incorrect", 0),
                "pct": mk_data.get("pct", 0.0),
            }
        yearly_backtest.append(entry)

    # Feature importance plot (top 15)
    fi_plot = [{"name": f.name, "importance": f.importance} for f in features[:15]]

    # Feature categories
    feature_cats = []
    for cat_name, feats in feature_categories_def.items():
        cat_import = sum(f.importance for f in features if f.name in feats)
        feature_cats.append({
            "name": cat_name,
            "feature_count": len(feats),
            "total_importance": round(cat_import, 4),
            "features": feats,
        })

    # Overall metrics
    all_mae = [r.get("mae", 0) for r in results_data]
    overall_mae = round(sum(all_mae) / max(len(all_mae), 1), 2)

    def _sum_metric(key, sub=None):
        vals = [r.get(key, {}) if sub is None else r.get(key, {}).get(sub, 0) for r in results_data if r.get(key)]
        return sum(vals)

    total_ats = _sum_metric("ats", "total") or _sum_metric("total_games")
    total_ats_correct = _sum_metric("ats", "correct")
    total_ou = _sum_metric("ou", "correct") + _sum_metric("ou", "incorrect")
    total_ou_correct = _sum_metric("ou", "correct")
    total_ml = _sum_metric("ml", "correct") + _sum_metric("ml", "incorrect")
    total_ml_correct = _sum_metric("ml", "correct")

    ats_incorrect = _sum_metric("ats", "incorrect")
    ou_incorrect = _sum_metric("ou", "incorrect")
    ml_incorrect = _sum_metric("ml", "incorrect")
    ats_pushes = _sum_metric("ats", "pushes") or _sum_metric("ats", "push")
    ou_pushes = _sum_metric("ou", "pushes") or _sum_metric("ou", "push")

    algorithm = "XGBoost"
    if name == "ML":
        algorithm = "XGBoost Classifier + Platt Calibration (n_estimators=300, max_depth=4)"
    elif name == "O/U":
        algorithm = "XGBoost Regressor (n_estimators=200, max_depth=4)"
    elif name == "ATS":
        algorithm = "XGBoost Regressor (n_estimators=200, max_depth=4, learning_rate=0.05)"

    return ModelVariantOut(
        name=name,
        description=feature_descriptions.get("__desc__", ""),
        algorithm=algorithm,
        total_features=len(features),
        features=features,
        feature_categories=feature_cats,
        backtest_results=yearly_backtest,
        feature_importance_plot=fi_plot,
        overall_mae=overall_mae,
        overall_ats=ModelBettingOut(
            correct=total_ats_correct,
            incorrect=ats_incorrect,
            total=total_ats,
            pct=round(100 * total_ats_correct / max(total_ats, 1), 1),
            pushes=ats_pushes,
        ) if total_ats > 0 else None,
        overall_ou=ModelBettingOut(
            correct=total_ou_correct,
            incorrect=ou_incorrect,
            total=total_ou,
            pct=round(100 * total_ou_correct / max(total_ou, 1), 1),
            pushes=ou_pushes,
        ) if total_ou > 0 else None,
        overall_ml=ModelBettingOut(
            correct=total_ml_correct,
            incorrect=ml_incorrect,
            total=total_ml,
            pct=round(100 * total_ml_correct / max(total_ml, 1), 1),
            pushes=0,
        ) if total_ml > 0 else None,
    )


def _calc_high_confidence_multi(ats_results, ou_results, ml_results, threshold_pcts):
    """Compute high confidence picks from multi-model results.

    For each threshold percentage, computes aggregate ATS, O/U, and ML stats
    from the combined backtest results.
    """
    hc = []
    total_ats = sum(r.get("ats", {}).get("total", r.get("total_games", 0)) for r in (ats_results or []))
    total_ats_correct = sum(r.get("ats", {}).get("correct", 0) for r in (ats_results or []))
    total_ou = sum(r.get("ou", {}).get("total", 0) for r in (ou_results or []))
    total_ou_correct = sum(r.get("ou", {}).get("correct", 0) for r in (ou_results or []))
    total_ml = sum(r.get("ml", {}).get("total", 0) for r in (ml_results or []))
    total_ml_correct = sum(r.get("ml", {}).get("correct", 0) for r in (ml_results or []))

    for pct in threshold_pcts:
        ats_pct = round(100 * total_ats_correct / max(total_ats, 1), 1) if total_ats else 0
        ou_pct = round(100 * total_ou_correct / max(total_ou, 1), 1) if total_ou else 0
        ml_pct = round(100 * total_ml_correct / max(total_ml, 1), 1) if total_ml else 0
        hc.append({
            "threshold": pct,
            "total": int(total_ats * pct / 100),
            "correct": int(total_ats_correct * pct / 100),
            "pct": ats_pct,
            "ou_total": int(total_ou * pct / 100),
            "ou_correct": int(total_ou_correct * pct / 100),
            "ou_pct": ou_pct,
            "ml_total": int(total_ml * pct / 100),
            "ml_correct": int(total_ml_correct * pct / 100),
            "ml_pct": ml_pct,
        })
    return hc


def _build_model_variant(name, results_file, feature_descriptions, feature_categories_def,
                          metric_keys=None,
                          sport: str = None, model_type: str = None,
                          results_data: list = None) -> ModelVariantOut | None:
    """Build a ModelVariantOut from a results file (or DB or in-memory data).

    Args:
        name: "ATS", "O/U", or "ML"
        results_file: JSON filename in _MODELS_DIR (fallback)
        feature_descriptions: dict of feature_name -> description
        feature_categories_def: dict of category_name -> list[feature_names]
        metric_keys: which overall metrics this model is optimized for (e.g. ["ats"])
        sport: sport schema ("nfl", "nba", "mlb") — if set, DB is tried first
        model_type: model type in DB ("ats", "ou", "ml") — if set, DB is tried first
        results_data: pass results list directly (skips DB/file loading)
    """
    import json
    # Use in-memory data if provided
    if results_data is not None:
        results = results_data
    else:
        # Try DB first if sport+model_type provided
        results = _try_db_results(sport, model_type)
        if not results:
            results = _load_json(results_file)
    if not results:
        return None

    cat_lookup = {}
    for cat, feats in feature_categories_def.items():
        for f in feats:
            cat_lookup[f] = cat

    backtest_results = sorted(results, key=lambda x: x.get("test_year", 0))

    # Aggregate feature importances across years
    imp_agg: dict[str, float] = {}
    imp_count: dict[str, int] = {}
    for r in results:
        for fi in r.get("feature_importance", []):
            fn = fi["feature"]
            imp_agg[fn] = imp_agg.get(fn, 0) + fi.get("importance", 0)
            imp_count[fn] = imp_count.get(fn, 0) + 1

    sorted_feats = sorted(
        [(n, imp_agg[n] / c) for n, c in imp_count.items()],
        key=lambda x: -x[1],
    )

    features = []
    for feat_name, imp in sorted_feats:
        features.append(ModelFeatureOut(
            name=feat_name,
            description=feature_descriptions.get(feat_name, feat_name),
            importance=round(imp, 4),
            category=cat_lookup.get(feat_name, "Other"),
        ))



    fi_plot = [{"name": f.name, "importance": f.importance} for f in features[:15]]

    # Feature categories
    feature_cats = []
    for cat_name, feats in feature_categories_def.items():
        cat_import = sum(f.importance for f in features if f.name in feats)
        feature_cats.append({
            "name": cat_name,
            "feature_count": len(feats),
            "total_importance": round(cat_import, 4),
            "features": feats,
        })

    # Overall metrics
    all_mae = [r.get("mae", 0) for r in results]
    overall_mae = round(sum(all_mae) / max(len(all_mae), 1), 2)

    def _sum_metric(key, sub=None):
        vals = [r.get(key, {}) if sub is None else r.get(key, {}).get(sub, 0) for r in results if r.get(key)]
        return sum(vals)

    def _count_metric(key, sub):
        vals = [r.get(key, {}).get(sub, 0) for r in results if r.get(key)]
        return sum(vals)

    ats_total = _count_metric("ats", "correct") + _count_metric("ats", "incorrect")
    ou_total = _count_metric("ou", "correct") + _count_metric("ou", "incorrect")
    ml_total = _count_metric("ml", "correct") + _count_metric("ml", "incorrect")

    algorithm = "XGBoost Regressor (n_estimators=200, max_depth=4, learning_rate=0.05)"
    if name == "ML":
        algorithm = "XGBoost Classifier + Platt Calibration (n_estimators=300, max_depth=4)"
    elif name == "O/U":
        algorithm = "XGBoost Regressor with Pace/YPG/Variance (n_estimators=200, max_depth=4)"

    return ModelVariantOut(
        name=name,
        description=feature_descriptions.get("__desc__", f"{name}-optimized NFL prediction model"),
        algorithm=algorithm,
        total_features=len(features),
        features=features,
        feature_categories=feature_cats,
        backtest_results=backtest_results,
        overall_mae=overall_mae,
        overall_ats=ModelBettingOut(
            correct=_count_metric("ats", "correct"),
            incorrect=_count_metric("ats", "incorrect"),
            total=ats_total,
            pct=round(100 * _count_metric("ats", "correct") / max(ats_total, 1), 1),
            pushes=_count_metric("ats", "pushes") or _count_metric("ats", "push"),
        ) if ats_total > 0 else None,
        overall_ou=ModelBettingOut(
            correct=_count_metric("ou", "correct"),
            incorrect=_count_metric("ou", "incorrect"),
            total=ou_total,
            pct=round(100 * _count_metric("ou", "correct") / max(ou_total, 1), 1),
            pushes=_count_metric("ou", "pushes") or _count_metric("ou", "push"),
        ) if ou_total > 0 else None,
        overall_ml=ModelBettingOut(
            correct=_count_metric("ml", "correct"),
            incorrect=_count_metric("ml", "incorrect"),
            total=ml_total,
            pct=round(100 * _count_metric("ml", "correct") / max(ml_total, 1), 1),
        ) if ml_total > 0 else None,
        feature_importance_plot=fi_plot,
    )


def _get_nfl_model_detail() -> SportModelDetailOut:
    """Build NFL model detail with two specialized model variants.

    Loads from separate result files:
    - ats_backtest_results.json (ATS-optimized model)
    - ou_results_baseline.json (OU-optimized model)

    Falls back to the legacy nfl_backtest_results.json for the v2 model stats.
    """
    import os

    ats_variant = _build_model_variant("ATS", "ats_backtest_results.json", _ATS_DESCRIPTIONS, _ATS_CATEGORIES,
                                        sport="nfl", model_type="ats")

    ou_variant = _build_model_variant("O/U", "ou_results_baseline.json", _OU_DESCRIPTIONS, _OU_CATEGORIES,
                                        sport="nfl", model_type="ou")

    # ── ML Model ──



    # ── Legacy model (v2, for backward compat) ──
    legacy_results = _load_json("nfl_backtest_results.json")

    # Merge model_variants (only non-None ones)
    model_variants = [v for v in [ats_variant, ou_variant] if v is not None]

    # Overall stats: use ATS model for ATS, OU model for OU, ML model for ML
    overall_ats_val = ats_variant.overall_ats if ats_variant and ats_variant.overall_ats else ModelBettingOut(correct=0, incorrect=0, total=0, pct=0)
    overall_ou_val = ou_variant.overall_ou if ou_variant and ou_variant.overall_ou else ModelBettingOut(correct=0, incorrect=0, total=0, pct=0, pushes=0)
    overall_ml_val = None

    # Overall MAE: average of available model MAEs
    mae_vals = []
    if ats_variant and ats_variant.overall_mae: mae_vals.append(ats_variant.overall_mae)
    if ou_variant and ou_variant.overall_mae: mae_vals.append(ou_variant.overall_mae)
    overall_mae = round(sum(mae_vals) / max(len(mae_vals), 1), 2) if mae_vals else 0

    # Training + test years across all models
    all_train = set()
    all_test = set()
    for v in model_variants:
        for r in v.backtest_results:
            ty = r.get("test_year")
            if ty is not None:
                all_test.add(ty)
            for y in r.get("train_years", []):
                if y is not None:
                    all_train.add(y)

    # Combined features list (union across models, deduplicated)
    combined_features = []
    seen_feats = set()
    for v in model_variants:
        for f in v.features:
            if f.name not in seen_feats:
                seen_feats.add(f.name)
                combined_features.append(f)

    # Feature importance plot: union, ordered by avg importance
    fi_plot = []
    if model_variants:
        fi_agg: dict[str, float] = {}
        fi_cnt: dict[str, int] = {}
        for v in model_variants:
            for fi in v.feature_importance_plot:
                fi_agg[fi["name"]] = fi_agg.get(fi["name"], 0) + fi["importance"]
                fi_cnt[fi["name"]] = fi_cnt.get(fi["name"], 0) + 1
        sorted_fi = sorted(
            [(n, fi_agg[n] / c) for n, c in fi_cnt.items()],
            key=lambda x: -x[1],
        )
        fi_plot = [{"name": n, "importance": round(imp, 4)} for n, imp in sorted_fi[:15]]

    # Feature categories (combined)
    combined_cats = []
    for v in model_variants:
        for cat in v.feature_categories:
            combined_cats.append(cat)

    # High confidence: each metric from its dedicated model
    ats_hc_data = ats_variant.backtest_results if ats_variant else []
    ou_hc_data = ou_variant.backtest_results if ou_variant else []
    high_conf = _calc_high_confidence_multi(
        ats_results=ats_hc_data,
        ou_results=ou_hc_data,
        ml_results=None,
        threshold_pcts=[25, 20, 15, 10, 5]
    )

    return SportModelDetailOut(
        sport="nfl",
        model_type="Two Specialized Models (ATS / O/U)",
        description=(
            "The NFL prediction system uses two separate specialized XGBoost models, "
            "each optimized for a different betting market:\n\n"
            "🔵 **ATS Model** — Predicts margin of victory. Features: opponent-adjusted PPG, "
            "implied scoring from OU line, spread movement, dome. No raw spread. "
            "Purely spread-beating specialist.\n\n"
            "🟡 **O/U Model** — Predicts total points (home+away). Features: opponent-adjusted "
            "PPG, pace stats (snap counts), yards/game, scoring variance, OU movement, "
            "and all situational factors.\n\n"
            "Select a model variant above to see its specific features, backtest results, "
            "and performance metrics."
        ),
        algorithm="XGBoost — Two Specialized Variants",
        training_years=sorted(all_train) if all_train else [2017, 2018, 2019, 2020],
        test_years=sorted(all_test),
        total_features=len(combined_features),
        features=combined_features,
        feature_categories=combined_cats,
        backtest_results=[],  # Individual model results live in model_variants
        overall_mae=overall_mae,
        overall_ats=overall_ats_val,
        overall_ou=overall_ou_val,
        overall_ml=overall_ml_val,
        monthly=[],
        high_confidence=high_conf,
        feature_importance_plot=fi_plot,
        last_updated="2026-06-02",
        model_variants=model_variants,
    )


def _get_nba_model_detail() -> SportModelDetailOut:
    """Build NBA model detail with ATS + OU specialized variants."""

    # ── Load ATS + OU results from training_runs ──
    try:
        conn = _pg_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT model_type, results_json::text FROM nba.training_runs "
            "WHERE model_type = 'ats' ORDER BY trained_at DESC LIMIT 1"
        )
        ats_row = cur.fetchone()
        ats_results = json.loads(ats_row[1]) if ats_row and ats_row[1] else []
        cur.execute(
            "SELECT model_type, results_json::text FROM nba.training_runs "
            "WHERE model_type = 'ou' ORDER BY trained_at DESC LIMIT 1"
        )
        ou_row = cur.fetchone()
        ou_results = json.loads(ou_row[1]) if ou_row and ou_row[1] else []
        cur.close()
        conn.close()
    except Exception:
        ats_results = []
        ou_results = []

    ats_variant = _build_model_variant("ATS", None, _NBA_ATS_DESCRIPTIONS, _NBA_ATS_CATEGORIES, results_data=ats_results)
    ou_variant = _build_model_variant("O/U", None, _NBA_OU_DESCRIPTIONS, _NBA_OU_CATEGORIES, results_data=ou_results)
    model_variants = [v for v in [ats_variant, ou_variant] if v is not None]

    # ── Combine for top-level summary ───────────────────────────────────────
    all_train = set()
    all_test = set()
    for v in model_variants:
        for r in v.backtest_results or []:
            ty = r.get("test_year")
            if ty is not None:
                all_test.add(ty)
            for y in r.get("train_years", []):
                if y is not None:
                    all_train.add(y)
    training_years = sorted(all_train)
    test_years = sorted(all_test)

    features = []
    seen_feats = set()
    for v in model_variants:
        for f in v.features or []:
            if f.name not in seen_feats:
                seen_feats.add(f.name)
                features.append(f)

    fi_plot = []
    if model_variants:
        fi_agg: dict[str, float] = {}
        fi_cnt: dict[str, int] = {}
        for v in model_variants:
            for fi in v.feature_importance_plot or []:
                n = fi["name"]
                fi_agg[n] = fi_agg.get(n, 0) + fi["importance"]
                fi_cnt[n] = fi_cnt.get(n, 0) + 1
        fi_plot = [
            {"name": n, "importance": round(fi_agg[n] / max(fi_cnt[n], 1), 4)}
            for n in sorted(fi_agg, key=lambda x: -fi_agg[x])[:15]
        ]

    combined_cats = []
    for v in model_variants:
        for cat in v.feature_categories or []:
            existing = next((c for c in combined_cats if c["name"] == cat["name"]), None)
            if existing:
                existing["feature_count"] += cat["feature_count"]
                existing["total_importance"] += cat["total_importance"]
                existing["features"] = list(set(existing["features"] + cat["features"]))
            else:
                combined_cats.append(dict(cat))

    all_mae = []
    all_ats_correct = 0
    all_ats_incorrect = 0
    all_ats_pushes = 0
    for v in model_variants:
        for r in v.backtest_results or []:
            all_mae.append(r.get("mae", 0))
            all_ats_correct += r.get("ats", {}).get("correct", 0)
            all_ats_incorrect += r.get("ats", {}).get("incorrect", 0)
            all_ats_pushes += r.get("ats", {}).get("pushes", r.get("ats", {}).get("push", 0))
    overall_mae = round(sum(all_mae) / max(len(all_mae), 1), 2)
    ats_total = all_ats_correct + all_ats_incorrect
    overall_ats = ModelBettingOut(
        correct=all_ats_correct, incorrect=all_ats_incorrect, total=ats_total,
        pct=round(100 * all_ats_correct / max(ats_total, 1), 1), pushes=all_ats_pushes,
    )

    high_confidence = []
    if ats_total > 0:
        pct = round(100 * all_ats_correct / max(ats_total, 1), 1)
        high_confidence = [
            {"threshold": t, "total": int(ats_total * t / 100),
             "correct": int(all_ats_correct * t / 100),
             "pct": pct}
            for t in [25, 20, 15, 10, 5]
        ]

    return SportModelDetailOut(
        sport="nba",
        model_type="XGBoost Point Differential Regressor",
        description=(
            "NBA model with two specialized variants: ATS (spread cover) "
            "and O/U (total points). Features focus on rolling ATS margins, "
            "straight-up win streaks, and implied market scoring."
        ),
        training_years=training_years,
        test_years=test_years,
        total_features=len(features),
        features=features,
        feature_categories=combined_cats,
        backtest_results=[],
        overall_mae=overall_mae,
        overall_ats=overall_ats,
        overall_ou=None,
        overall_ml=None,
        monthly=[],
        high_confidence=high_confidence,
        feature_importance_plot=fi_plot,
        last_updated="2026-06-12",
        algorithm="XGBoost Regressor (n_estimators=200, max_depth=4)",
        model_variants=model_variants,
    )


def _pg_conn():
    import psycopg2
    url = DATABASE_URL
    if url and "+asyncpg" in url:
        url = url.replace("+asyncpg", "")
    return psycopg2.connect(url)


@router.get("/prediction-stats/{sport}/results")
async def get_prediction_results_by_date(
    sport: str,
    start: str = Query(..., description="Start date (YYYY-MM-DD, inclusive)"),
    end: str = Query(..., description="End date (YYYY-MM-DD, inclusive)"),
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Return pick results over a date range: win% + units profit per bet
    type (ATS/Run Line, OU, ML) and profit broken down by EV score.

    Uses the latest prediction per game ({sport}.game_predictions), the same
    dedup logic as the EV-distribution endpoint, filtered by game date.
    """
    from sqlalchemy import text as _sa_text
    from sqlalchemy.exc import SQLAlchemyError
    from datetime import datetime, timezone as _tz

    # ---- Resolve sport config (mirrors ev-distribution) ----------------
    if sport == "nfl":
        schema, use_ml = "nfl", True
        conf_ats, conf_ou, conf_ml = "ats_conf_cal", "ou_conf_cal", "ml_conf_cal"
        rl_col, ou_col, ml_col = "ats_result", "ou_result", "ml_result"
    elif sport == "nba":
        schema, use_ml = "nba", True
        conf_ats, conf_ou, conf_ml = "ats_conf_cal", "ou_conf_cal", "ml_conf_cal"
        rl_col, ou_col, ml_col = "ats_result", "ou_result", "ml_result"
    else:
        schema, use_ml = "mlb", True
        conf_ats, conf_ou, conf_ml = "rl_conf_cal", "ou_conf_cal", "ml_conf_cal"
        rl_col, ou_col, ml_col = "run_line_result", "ou_result", "ml_result"

    # Inclusive date range; g.date is tz-aware UTC.
    try:
        start_dt = datetime.fromisoformat(start + "T00:00:00").replace(tzinfo=_tz.utc)
        end_dt = datetime.fromisoformat(end + "T23:59:59").replace(tzinfo=_tz.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date range format")

    try:
        rows = await db.execute(
            _sa_text(f"""
            SELECT
                gp.{rl_col} AS ats_result, gp.{ou_col} AS ou_result,
                gp.{ml_col} AS ml_result,
                gp.ats_profit, gp.ou_profit, gp.ml_profit,
                gp.{conf_ats} AS rl_conf, gp.{conf_ou} AS ou_conf,
                gp.{conf_ml} AS ml_conf,
                gp.ats_odds, gp.ou_odds, gp.ml_odds
            FROM (
                SELECT DISTINCT ON (gp_inner.game_id) gp_inner.*
                FROM {schema}.game_predictions gp_inner
                ORDER BY gp_inner.game_id, gp_inner.created_at DESC
            ) gp
            JOIN {schema}.games g ON g.id = gp.game_id
            WHERE g.date >= :start AND g.date <= :end
            """),
            {"start": start_dt, "end": end_dt},
        )
        _rows = list(rows.fetchall())
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=500, detail=f"Query failed: {exc}")

    def _stats(attr_result, attr_profit):
        """Aggregate one bet type from rows. result in (Win, Loss, Push/None)."""
        wins = losses = pushes = n = 0
        units = 0.0
        for r in _rows:
            res = getattr(r, attr_result)
            if res is None:
                continue
            n += 1
            prof = getattr(r, attr_profit)
            units += float(prof) if prof is not None else 0.0
            if res == "Win":
                wins += 1
            elif res == "Loss":
                losses += 1
            else:
                pushes += 1
        return {
            "total": n,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "win_pct": round(100.0 * wins / n, 1) if n else 0.0,
            "profit": round(units, 2),
        }

    by_type = {
        "ats": _stats("ats_result", "ats_profit"),
        "ou": _stats("ou_result", "ou_profit"),
        "ml": _stats("ml_result", "ml_profit") if use_ml else {
            "total": 0, "wins": 0, "losses": 0, "pushes": 0, "win_pct": 0.0, "profit": 0.0,
        },
    }

    # ---- Profit by EV score --------------------------------------------
    # For each pick with a result, approximate EV using the stored confidence
    # as win probability and the stored odds to derive per-unit value, then
    # bucket by $5 EV windows so we can chart profit vs EV.
    def _win_dollar(odds):
        if not odds:
            return 100.0
        o = int(odds)
        if o == 0:
            return 100.0
        return float(o) if o > 0 else 100.0 * 100.0 / abs(o)

    profit_by_ev = {}
    for r in _rows:
        for key, attr_conf, attr_odds, attr_profit, attr_res in (
            ("ats", "rl_conf", "ats_odds", "ats_profit", "ats_result"),
            ("ou", "ou_conf", "ou_odds", "ou_profit", "ou_result"),
            ("ml", "ml_conf", "ml_odds", "ml_profit", "ml_result"),
        ):
            prof = getattr(r, attr_profit)
            conf = getattr(r, attr_conf)
            odds = getattr(r, attr_odds)
            res = getattr(r, attr_res)
            if prof is None or conf is None or odds is None:
                continue
            ev = (float(conf) * _win_dollar(odds)) - ((1.0 - float(conf)) * 100.0)
            bucket = int(ev // 5.0) * 5.0
            entry = profit_by_ev.setdefault(
                bucket, {"n": 0, "wins": 0, "losses": 0, "profit": 0.0}
            )
            entry["n"] += 1
            entry["profit"] += float(prof)
            if res == "Win":
                entry["wins"] += 1
            elif res == "Loss":
                entry["losses"] += 1

    profit_by_ev_list = [
        {
            "ev": round(b, 0),
            "n": e["n"],
            "wins": e["wins"],
            "losses": e["losses"],
            "profit": round(e["profit"], 2),
        }
        for b, e in sorted(profit_by_ev.items())
    ]

    return {
        "range": {"start": start, "end": end},
        "by_type": by_type,
        "profit_by_ev": profit_by_ev_list,
    }


class TaskStatusOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    task_type: str
    cron_expr: str
    timezone: str
    enabled: bool
    created_at: datetime | None = None
    last_status: str | None = None
    last_run: datetime | None = None
    last_duration: int | None = None
    last_error: str | None = None
    next_run: str | None = None

    model_config = {"from_attributes": True}


class TaskRunOut(BaseModel):
    id: int
    task_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    error_message: str | None = None
    details: dict | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


@router.get("/tasks", response_model=list[TaskStatusOut])
async def get_tasks(admin: User = Depends(get_admin_user)):
    """List all tasks with last run info + next run times."""
    from app.task_scheduler import get_task_statuses, get_next_run_times
    tasks = await get_task_statuses()
    next_runs = get_next_run_times()
    for t in tasks:
        t["next_run"] = next_runs.get(t["name"])
    return tasks


@router.get("/tasks/{name}/runs", response_model=list[TaskRunOut])
async def get_task_runs(
    name: str,
    limit: int = Query(20, ge=1, le=100),
    admin: User = Depends(get_admin_user),
):
    """Get run history for a task."""
    from app.task_scheduler import get_task_runs
    return await get_task_runs(name, limit)


@router.post("/tasks/{name}/trigger")
async def trigger_task(name: str, admin: User = Depends(get_admin_user)):
    """Manually trigger a task."""
    from app.task_scheduler import trigger_task
    ok = await trigger_task(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Task '{name}' not found or scheduler not running")
    return {"status": "triggered", "task": name}


@router.patch("/tasks/{name}/enabled")
async def set_task_enabled(name: str, body: dict, admin: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    """Enable or disable a task by name. Reloads the scheduler job immediately."""
    enabled = bool(body.get("enabled"))
    result = await db.execute(
        text("UPDATE task_config SET enabled = :enabled WHERE name = :name"),
        {"enabled": enabled, "name": name},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Task not found: {name}")

    from app.task_scheduler import _scheduler, load_tasks, _update_next_runs
    if _scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not running")
    await load_tasks(_scheduler)
    _update_next_runs()
    return {"status": "ok", "name": name, "enabled": enabled}


@router.post("/tasks/refresh")
async def refresh_tasks(admin: User = Depends(get_admin_user)):
    """Reload tasks from DB into scheduler."""
    from app.task_scheduler import _scheduler, load_tasks
    if _scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not running")
    await load_tasks(_scheduler)
    from app.task_scheduler import _update_next_runs
    _update_next_runs()
    return {"status": "refreshed", "jobs": len(_scheduler.get_jobs())}


# ── Database Explorer ───────────────────────────────────────────────


@router.get("/db/schemas")
async def db_list_schemas(admin: User = Depends(get_admin_user)):
    """List all schemas in the database."""
    # Can't use bindparam for schema name in information_schema, so hardcode the schemas we care about
    schemas = ["public", "nfl", "nba", "mlb"]
    return schemas


@router.get("/db/schemas/{schema_name}/tables")
async def db_list_tables(
    schema_name: str,
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """List all user tables in the given schema."""
    result = await db.execute(
        text("""
            SELECT tablename AS table_name
            FROM pg_catalog.pg_tables
            WHERE schemaname = :schema
            ORDER BY tablename
        """),
        {"schema": schema_name},
    )
    rows = result.fetchall()
    return [{"table_name": r[0]} for r in rows]


@router.get("/db/schemas/{schema_name}/tables/{table_name}")
async def db_get_table(
    schema_name: str,
    table_name: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Get column info and row data for a table."""
    from sqlalchemy import inspect

    # 1) Column info via information_schema
    col_result = await db.execute(
        text("""
            SELECT
                c.column_name,
                c.data_type,
                CASE WHEN c.is_nullable = 'YES' THEN true ELSE false END AS nullable,
                CASE WHEN pk.col IS NOT NULL THEN true ELSE false END AS is_pk,
                c.column_default AS "default"
            FROM information_schema.columns c
            LEFT JOIN (
                SELECT ku.column_name AS col
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage ku
                    ON tc.constraint_name = ku.constraint_name
                    AND tc.table_schema = ku.table_schema
                WHERE tc.constraint_type = 'PRIMARY KEY'
                  AND tc.table_schema = :schema
                  AND tc.table_name = :table
            ) pk ON pk.col = c.column_name
            WHERE c.table_schema = :schema2
              AND c.table_name = :table2
            ORDER BY c.ordinal_position
        """),
        {"schema": schema_name, "table": table_name, "schema2": schema_name, "table2": table_name},
    )
    columns = [
        {
            "column_name": r[0],
            "data_type": r[1],
            "nullable": r[2],
            "is_pk": r[3],
            "default": r[4],
        }
        for r in col_result.fetchall()
    ]

    # 2) Row count
    count_result = await db.execute(
        text(f'SELECT COUNT(*) FROM "{schema_name}"."{table_name}"'),
    )
    total_count = count_result.scalar()

    # 3) Row data
    raw_columns = [c["column_name"] for c in columns]
    cols_quoted = ", ".join(f'"{c}"' for c in raw_columns)
    data_result = await db.execute(
        text(f"SELECT {cols_quoted} FROM \"{schema_name}\".\"{table_name}\" ORDER BY 1 OFFSET :offset LIMIT :limit"),
        {"offset": offset, "limit": limit},
    )
    import math
    rows = []
    for r in data_result.fetchall():
        row = dict(zip(raw_columns, r))
        # Replace NaN and Infinity float values with None (not JSON-compliant)
        for k, v in row.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                row[k] = None
        rows.append(row)

    return {
        "schema_name": schema_name,
        "table_name": table_name,
        "columns": columns,
        "rows": rows,
        "total_count": total_count,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total_count,
    }


# ── Data Loader ────────────────────────────────────────────────────────
# This endpoint lets admins run a data loader for a single game and see
# every feature (raw + computed) that the data loader produces.

@router.get("/data-loader/{sport}/load")
async def data_loader_load_game(
    sport: str,
    game_id: int = Query(..., description="Game ID to load"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Load and build all features for a single game via the sport's data loader.

    This endpoint shows EXACTLY what the sport data_loader collects for the game
    (the same values fed to the model). It reuses the loader's own SQL + build
    pipeline — no duplicated SQL lives here. The feature list is the
    ``is_trainable`` set from the per-sport ``features`` table.
    """
    sport = sport.lower()
    if sport not in ("nfl", "mlb", "nba"):
        raise HTTPException(status_code=400, detail=f"Unsupported sport '{sport}'. Supported: nfl, mlb, nba")

    from app.core.config import settings
    from sqlalchemy import text as _sa_text
    db_url = str(settings.database_url).replace("+asyncpg", "")

    try:
        # ── Pull the trainable feature list + metadata from the DB registry ──
        _feat_rows = (await db.execute(_sa_text(
            "SELECT name, COALESCE(display_name, name) AS display_name, "
            "description, current_ats, current_ou, pick_card "
            f"FROM {sport}.features WHERE is_trainable = TRUE ORDER BY name"
        ))).fetchall()
        feats = [r[0] for r in _feat_rows]

        # ── Load the game features through the sport's own data_loader ──
        if sport == "nfl":
            from app.handicapping.nfl.data_loader import NFLDataLoader
            from app.handicapping.nfl import engine as _eng
            dl = NFLDataLoader(db_url=db_url)
            _impute = _eng._impute_feature
            df = dl.load_data(game_ids=[game_id], include_upcoming=True, feature_names=feats)
            model_ats = set(_eng._get_features("ats")) | set(_eng._get_features("ou"))
        elif sport == "nba":
            from app.handicapping.nba.data_loader import NBADataLoader
            from app.handicapping.nba import nba_engine as _eng
            dl = NBADataLoader(db_url=db_url)
            _impute = _eng._impute_feature
            df = dl.load_inference_data(game_ids=[game_id])
            model_ats = set(_eng._get_features("ats")) | set(_eng._get_features("ou"))
        else:  # mlb
            from app.handicapping.mlb.data_loader import MLBDataLoader, build_features as mlb_build_features
            from app.handicapping.mlb import mlb_engine as _eng
            import pandas as _pd
            dl = MLBDataLoader(db_url=db_url)
            _impute = _eng._impute_feature
            # build_features computes the L10 / expanding-mean features (h_rf_avg, h_ra_avg,
            # a_rf_avg, a_ra_avg, h/a_winpct_l10, h/a_form_l10, winpct_l10_diff) from the stacked
            # game-log present in the DataFrame (groupby(team).shift(1).rolling/expanding).
            # Loading ONLY the target game leaves a 1-row-per-team frame => those all become NaN/blank.
            # Load the target game + its season's prior FINAL games so the game-log context exists.
            target = dl.load_games(game_ids=[game_id], status=None)
            if target is not None and not target.empty and "season_year" in target.columns:
                _sy = int(target.iloc[0]["season_year"]) if target.iloc[0]["season_year"] is not None else 2026
            else:
                _sy = 2026
            prior = dl.load_games(seasons=[_sy], status="FINAL", include_upcoming=False)
            gdf = _pd.concat([prior, target], ignore_index=True)
            bdf = mlb_build_features(gdf.copy())
            df = bdf[bdf["game_id"] == game_id] if "game_id" in bdf.columns else bdf
            model_ats = set(_eng._get_features().get("ats", [])) | set(_eng._get_features().get("ou", []))

        if df is None or df.empty:
            raise HTTPException(status_code=404, detail=f"No feature data for game {game_id}")
        row = df.iloc[0]
        # NBA keeps pristine raw values in "<feat>_raw" twins (blended path). When a
        # twin exists, show the RAW display value (in-season, else prior season); the
        # blended "value" below is still exactly what the model consumes.
        raw_key = None
        if sport == "nba":
            raw_key = lambda feat: (feat + "_raw") if (feat + "_raw") in row.index else feat
        feat_meta = {r[0]: r for r in _feat_rows}

        # ── Game summary (games/teams catalog read, NOT feature SQL) ──
        # Only NFL.games has a `week` column; NBA/MLB don't, so select it
        # conditionally (NULL otherwise) to keep the cross-sport query valid.
        _week_sql = "g.week" if sport == "nfl" else "NULL::int AS week"
        _gi = (await db.execute(_sa_text(
            "SELECT g.id AS game_id, s.id AS season_id, " + _week_sql + ", "
            "g.home_score, g.away_score, "
            "g.date AS game_date, g.status, "
            "ht.abbreviation AS ha, ht.name AS home_team, ht.abbreviation AS home_abbr, "
            "at.abbreviation AS aa, at.name AS away_team, at.abbreviation AS away_abbr "
            f"FROM {sport}.games g "
            f"JOIN {sport}.seasons s ON s.id = g.season_id "
            f"JOIN {sport}.teams ht ON ht.id = g.home_team_id "
            f"JOIN {sport}.teams at ON at.id = g.away_team_id "
            f"WHERE g.id = {int(game_id)}"
        ))).mappings().first()
        game_info = dict(_gi) if _gi else {"game_id": game_id}

        def _safe_val(v):
            if v is None:
                return None
            try:
                f = float(v)
                if f != f or f in (float("inf"), float("-inf")):  # NaN/inf
                    return None
            except (TypeError, ValueError):
                pass
            return v if isinstance(v, (int, float, str, bool)) or v is None else str(v)

        features = []
        for feat in feats:
            m = feat_meta.get(feat)
            display_name = m[1] if m and len(m) > 1 else feat
            description = m[2] if m and len(m) > 2 else ""
            cur_ats = (m[3] if m and len(m) > 3 else None)
            cur_ou = (m[4] if m and len(m) > 4 else None)
            pick_card = (m[5] if m and len(m) > 5 else None)

            raw_k = raw_key(feat) if raw_key is not None else feat
            raw = _safe_val(row.get(raw_k))
            # Model-facing value: mirror the engine's _extract_feature_vector logic.
            # Use the RAW value when it is present (non-None, non-NaN); only fall
            # back to the engine imputer when the raw value is truly missing.
            # (This prevents the panel from showing an imputed fallback like
            # season ERA for a day/night split the row actually HAS.)
            is_raw_missing = (
                raw is None
                or (isinstance(raw, float) and raw != raw)  # NaN
            )
            if feat in model_ats:
                try:
                    if not is_raw_missing:
                        model_value = raw
                    else:
                        model_value = _impute(row, feat)
                except Exception:
                    model_value = raw
                is_imputed = is_raw_missing and (model_value is not None)
                value = model_value if model_value is not None else raw
            else:
                is_imputed = False
                value = raw

            features.append({
                "name": feat,
                "display_name": display_name,
                "group": "computed",
                "description": description or "",
                "value": _safe_val(value),
                "raw_value": raw,
                "type": "computed",
                "is_model_value": feat in model_ats,
                "is_imputed": bool(is_imputed),
                "current_ats": cur_ats,
                "current_ou": cur_ou,
                "aliases": [],
            })

        return {
            "game_id": game_id,
            "game_info": game_info,
            "sport": sport,
            "total_features": len(features),
            "raw_features": 0,
            "game_features": 0,
            "lines_features": 0,
            "result_features": 0,
            "computed_features": len(features),
            "features": features,
            "source": "data_loader.{}.load_data".format(sport),
            "note": "All features are the is_trainable set from {}.features, built by the sport data_loader.".format(sport),
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error("Data loader error for %s game_id=%d: %s\n%s", sport, game_id, str(e), tb)
        raise HTTPException(status_code=500, detail=f"Data loader error: {str(e)}")
async def data_loader_game_info(
    sport: str,
    game_id: int = Query(..., description="Game ID to look up"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Quick lookup: check if a game_id exists and return basic info."""
    sport = sport.lower()
    if sport not in ("nfl", "mlb", "nba"):
        raise HTTPException(status_code=400, detail=f"Unsupported sport '{sport}'")

    schema_name = sport
    table_name = "games"

    try:
        # Column name: all sports use 'id' as PK; some older code expects 'game_id' too
        id_col = "id"
        result = await db.execute(
            text(f'SELECT * FROM "{schema_name}"."{table_name}" WHERE {id_col} = :gid'),
            {"gid": game_id},
        )
        row = result.mappings().first()
        if not row:
            # Fallback: try 'game_id' column name
            result = await db.execute(
                text(f'SELECT * FROM "{schema_name}"."{table_name}" WHERE game_id = :gid'),
                {"gid": game_id},
            )
            row = result.mappings().first()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"No game found with game_id={game_id} in {sport.upper()}",
            )

        # Convert to dict — only basic fields
        basic_fields = ["game_id", "season_id", "home_team", "away_team",
                        "home_score", "away_score", "game_date", "status", "week"]
        info = {k: row[k] for k in basic_fields if k in row._mapping}

        # Also get home/away abbreviations if they exist
        if "ha" in row._mapping:
            info["ha"] = row["ha"]
        if "aa" in row._mapping:
            info["aa"] = row["aa"]
        if "home_abbr" in row._mapping:
            info["home_abbr"] = row["home_abbr"]
        if "away_abbr" in row._mapping:
            info["away_abbr"] = row["away_abbr"]

        return {"game_id": game_id, "sport": sport, "exists": True, "info": info}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
