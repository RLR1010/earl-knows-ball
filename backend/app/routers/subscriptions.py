from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional
import json
import logging

from app.database import get_db
from app.models import User
from app.models.admin import SubscriptionPlan, UserSubscription, Payment
from app.core.config import settings
from app.routers.auth import get_current_user, get_token_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


# ── Schemas ─────────────────────────────────────────────────────────

class PlanPublic(BaseModel):
    id: str
    name: str
    slug: str
    description: str | None = None
    price_cents: int
    currency: str
    interval: str
    trial_days: int
    features: list
    is_active: bool
    sort_order: int

    model_config = {"from_attributes": True}


class CheckoutRequest(BaseModel):
    plan_id: str
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class CheckoutResponse(BaseModel):
    url: str | None = None
    mock: bool = False
    message: str = ""


class SubscriptionStatus(BaseModel):
    has_active: bool
    subscription: dict | None = None
    upcoming_invoice: dict | None = None


# ── Public: List Active Plans ───────────────────────────────────────

@router.get("/plans", response_model=list[PlanPublic])
async def list_plans(db: AsyncSession = Depends(get_db)):
    """Public endpoint — list all active subscription plans."""
    result = await db.execute(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.is_active == True)
        .order_by(SubscriptionPlan.sort_order)
    )
    return result.scalars().all()


# ── Auth Helpers ───────────────────────────────────────────────────

async def _get_user_from_request(request: Request, db: AsyncSession) -> User | None:
    """Extract user from Authorization header."""
    auth = request.headers.get("authorization", "")
    if not auth:
        return None
    try:
        return await get_token_user(auth, db)
    except Exception:
        return None


# ── Stripe Helpers ─────────────────────────────────────────────────

def _stripe_available() -> bool:
    return bool(settings.stripe_secret_key)


def _get_stripe():
    """Lazy-import stripe and configure."""
    import stripe
    stripe.api_key = settings.stripe_secret_key
    return stripe


async def _get_or_create_stripe_customer(user: User, stripe) -> str:
    """Get existing Stripe customer ID or create one."""
    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer = stripe.Customer.create(
        email=user.email,
        metadata={"user_id": user.id},
    )
    user.stripe_customer_id = customer.id
    return customer.id


# ── Create Checkout Session ─────────────────────────────────────────

@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout_session(
    req: CheckoutRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a Stripe Checkout Session for subscription purchase.
    Falls back to mock mode when Stripe is not configured."""
    auth = request.headers.get("authorization", "")
    user = await get_token_user(auth, db)

    # Get plan
    result = await db.execute(
        select(SubscriptionPlan).where(SubscriptionPlan.id == req.plan_id)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not plan.is_active:
        raise HTTPException(status_code=400, detail="Plan is not active")

    # Mock mode (no Stripe configured)
    if not _stripe_available():
        return CheckoutResponse(
            url=None,
            mock=True,
            message=f"Stripe not configured. Would subscribe to '{plan.name}' (${plan.price_cents/100:.2f}/{plan.interval}). "
                    f"Set STRIPE_SECRET_KEY in .env to enable live payments.",
        )

    try:
        stripe = _get_stripe()

        # Create or get Stripe customer
        customer_id = await _get_or_create_stripe_customer(user, stripe)

        success_url = req.success_url or f"{settings.base_url}/subscriptions/success?session_id={{CHECKOUT_SESSION_ID}}"
        cancel_url = req.cancel_url or f"{settings.base_url}/subscriptions/cancel"

        # Build checkout session
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "user_id": user.id,
                "plan_id": plan.id,
            },
            subscription_data={
                "metadata": {
                    "user_id": user.id,
                    "plan_id": plan.id,
                },
                "trial_period_days": plan.trial_days or None,
            },
        )

        return CheckoutResponse(url=session.url)

    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        raise HTTPException(status_code=500, detail=f"Payment processing error: {str(e)}")


# ── Stripe Webhook ──────────────────────────────────────────────────

@router.post("/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle Stripe webhook events (no auth — Stripe signs the payload)."""
    payload = await request.body()

    if _stripe_available() and settings.stripe_webhook_secret:
        # Verify Stripe signature
        stripe = _get_stripe()
        sig_header = request.headers.get("stripe-signature")
        if not sig_header:
            return JSONResponse(status_code=400, content={"error": "Missing stripe-signature header"})
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, settings.stripe_webhook_secret
            )
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "Invalid payload"})
        except stripe.error.SignatureVerificationError:
            return JSONResponse(status_code=400, content={"error": "Invalid signature"})
    else:
        # Mock/dev mode — parse as JSON directly
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    event_type = event.get("type", event.get("type", ""))
    data_object = event.get("data", {}).get("object", event)

    logger.info(f"Stripe webhook: {event_type}")

    try:
        if event_type == "checkout.session.completed":
            await _handle_checkout_completed(data_object, db)
        elif event_type == "customer.subscription.updated":
            await _handle_subscription_updated(data_object, db)
        elif event_type == "customer.subscription.deleted":
            await _handle_subscription_deleted(data_object, db)
        elif event_type == "invoice.paid":
            await _handle_invoice_paid(data_object, db)
        elif event_type == "invoice.payment_failed":
            await _handle_invoice_failed(data_object, db)
        else:
            logger.info(f"Unhandled event type: {event_type}")

    except Exception as e:
        logger.error(f"Webhook handler error: {e}")
        # Return 200 so Stripe doesn't retry; errors are logged
        return JSONResponse(status_code=200, content={"received": True, "error": str(e)})

    return JSONResponse(status_code=200, content={"received": True})


async def _handle_checkout_completed(session: dict, db: AsyncSession):
    """When checkout is completed, create/update the subscription record."""
    user_id = session.get("metadata", {}).get("user_id")
    plan_id = session.get("metadata", {}).get("plan_id")
    stripe_sub_id = session.get("subscription")
    customer_id = session.get("customer")

    if not user_id or not stripe_sub_id:
        logger.warning(f"Checkout completed missing user_id/plan_id: {session.get('id')}")
        return

    # Update user's stripe_customer_id if not set
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if user and not user.stripe_customer_id:
        user.stripe_customer_id = customer_id

    # Try getting the subscription details from Stripe for period info
    period_start = datetime.now(timezone.utc)
    period_end = None
    status = "active"

    if _stripe_available():
        try:
            stripe = _get_stripe()
            stripe_sub = stripe.Subscription.retrieve(stripe_sub_id)
            period_start = datetime.fromtimestamp(stripe_sub.current_period_start, tz=timezone.utc)
            period_end = datetime.fromtimestamp(stripe_sub.current_period_end, tz=timezone.utc)
            status = stripe_sub.status
        except Exception as e:
            logger.warning(f"Could not retrieve Stripe subscription: {e}")

    # Check if subscription already exists
    existing = await db.execute(
        select(UserSubscription).where(UserSubscription.stripe_subscription_id == stripe_sub_id)
    )
    sub = existing.scalar_one_or_none()

    if sub:
        sub.status = status
        sub.plan_id = plan_id
        sub.current_period_start = period_start
        sub.current_period_end = period_end
    else:
        sub = UserSubscription(
            user_id=user_id,
            plan_id=plan_id,
            status=status,
            current_period_start=period_start,
            current_period_end=period_end,
            stripe_subscription_id=stripe_sub_id,
            stripe_customer_id=customer_id,
        )
        db.add(sub)

    # Update user subscription_tier from plan
    if user and plan_id:
        plan_result = await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id)
        )
        plan = plan_result.scalar_one_or_none()
        if plan:
            if plan.interval == "year":
                user.subscription_tier = "premium_yearly"
            else:
                user.subscription_tier = "premium"

    await db.commit()


async def _handle_subscription_updated(subscription: dict, db: AsyncSession):
    """Sync subscription status changes from Stripe."""
    stripe_sub_id = subscription.get("id")
    status = subscription.get("status")

    if not stripe_sub_id:
        return

    result = await db.execute(
        select(UserSubscription).where(UserSubscription.stripe_subscription_id == stripe_sub_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        logger.warning(f"Subscription {stripe_sub_id} not found locally")
        return

    sub.status = status
    if subscription.get("current_period_start"):
        sub.current_period_start = datetime.fromtimestamp(
            subscription["current_period_start"], tz=timezone.utc
        )
    if subscription.get("current_period_end"):
        sub.current_period_end = datetime.fromtimestamp(
            subscription["current_period_end"], tz=timezone.utc
        )
    if subscription.get("canceled_at"):
        sub.canceled_at = datetime.fromtimestamp(
            subscription["canceled_at"], tz=timezone.utc
        )
    if subscription.get("trial_end"):
        sub.trial_end = datetime.fromtimestamp(
            subscription["trial_end"], tz=timezone.utc
        )

    # Sync user tier
    user_result = await db.execute(select(User).where(User.id == sub.user_id))
    user = user_result.scalar_one_or_none()
    if user:
        if status == "active":
            user.subscription_tier = "premium"
        elif status in ("canceled", "past_due", "incomplete_expired", "unpaid"):
            user.subscription_tier = "free"

    await db.commit()


async def _handle_subscription_deleted(subscription: dict, db: AsyncSession):
    """Handle subscription cancellation/deletion."""
    stripe_sub_id = subscription.get("id")
    if not stripe_sub_id:
        return

    result = await db.execute(
        select(UserSubscription).where(UserSubscription.stripe_subscription_id == stripe_sub_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        return

    sub.status = "canceled"
    sub.canceled_at = datetime.now(timezone.utc)

    # Reset user tier
    user_result = await db.execute(select(User).where(User.id == sub.user_id))
    user = user_result.scalar_one_or_none()
    if user:
        user.subscription_tier = "free"

    await db.commit()


async def _handle_invoice_paid(invoice: dict, db: AsyncSession):
    """Record successful payment."""
    stripe_sub_id = invoice.get("subscription")
    customer_id = invoice.get("customer")
    stripe_invoice_id = invoice.get("id")
    amount_paid = invoice.get("amount_paid", 0)
    currency = invoice.get("currency", "usd")
    status = invoice.get("status", "paid")

    if not stripe_sub_id:
        return

    # Find the subscription
    sub_result = await db.execute(
        select(UserSubscription).where(UserSubscription.stripe_subscription_id == stripe_sub_id)
    )
    sub = sub_result.scalar_one_or_none()
    if not sub:
        return

    # Record payment
    payment = Payment(
        user_id=sub.user_id,
        subscription_id=sub.id,
        amount_cents=amount_paid,
        currency=currency,
        status="succeeded" if status == "paid" else status,
        description=f"Invoice {stripe_invoice_id}",
        stripe_invoice_id=stripe_invoice_id,
        stripe_payment_intent_id=invoice.get("payment_intent"),
    )
    db.add(payment)
    await db.commit()


async def _handle_invoice_failed(invoice: dict, db: AsyncSession):
    """Handle payment failure."""
    stripe_sub_id = invoice.get("subscription")
    if not stripe_sub_id:
        return

    sub_result = await db.execute(
        select(UserSubscription).where(UserSubscription.stripe_subscription_id == stripe_sub_id)
    )
    sub = sub_result.scalar_one_or_none()
    if not sub:
        return

    # Record failed payment
    payment = Payment(
        user_id=sub.user_id,
        subscription_id=sub.id,
        amount_cents=invoice.get("amount_due", 0),
        currency=invoice.get("currency", "usd"),
        status="failed",
        description=f"Failed invoice {invoice.get('id')}",
        stripe_invoice_id=invoice.get("id"),
    )
    db.add(payment)

    # Mark subscription as past_due
    sub.status = "past_due"
    await db.commit()


# ── Get My Subscription ─────────────────────────────────────────────

@router.get("/my")
async def get_my_subscription(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Get the current user's subscription status."""
    auth = request.headers.get("authorization", "")
    user = await get_token_user(auth, db)

    result = await db.execute(
        select(UserSubscription)
        .where(UserSubscription.user_id == user.id)
        .order_by(UserSubscription.created_at.desc())
        .limit(1)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        return SubscriptionStatus(
            has_active=False,
            subscription=None,
        )

    # Get plan details
    plan_info = None
    if sub.plan_id:
        plan_result = await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id)
        )
        plan = plan_result.scalar_one_or_none()
        if plan:
            plan_info = {
                "name": plan.name,
                "price_cents": plan.price_cents,
                "interval": plan.interval,
                "features": plan.features,
            }

    sub_data = {
        "id": sub.id,
        "status": sub.status,
        "plan": plan_info,
        "current_period_start": sub.current_period_start.isoformat() if sub.current_period_start else None,
        "current_period_end": sub.current_period_end.isoformat() if sub.current_period_end else None,
        "canceled_at": sub.canceled_at.isoformat() if sub.canceled_at else None,
        "trial_end": sub.trial_end.isoformat() if sub.trial_end else None,
    }

    return SubscriptionStatus(
        has_active=sub.status == "active" or sub.status == "trialing",
        subscription=sub_data,
    )


# ── Cancel My Subscription ──────────────────────────────────────────

@router.post("/cancel")
async def cancel_subscription(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Cancel the current user's active subscription."""
    auth = request.headers.get("authorization", "")
    user = await get_token_user(auth, db)

    result = await db.execute(
        select(UserSubscription)
        .where(
            UserSubscription.user_id == user.id,
            UserSubscription.status.in_(["active", "trialing"]),
        )
        .order_by(UserSubscription.created_at.desc())
        .limit(1)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        raise HTTPException(status_code=404, detail="No active subscription found")

    if _stripe_available() and sub.stripe_subscription_id:
        try:
            stripe = _get_stripe()
            stripe.Subscription.modify(
                sub.stripe_subscription_id,
                cancel_at_period_end=True,
            )
            sub.canceled_at = datetime.now(timezone.utc)
        except Exception as e:
            logger.error(f"Stripe cancel error: {e}")
            raise HTTPException(status_code=500, detail="Failed to cancel with payment processor")

    sub.canceled_at = datetime.now(timezone.utc)
    await db.commit()

    return {"status": "canceled", "message": "Subscription will end at the current billing period"}


@router.get("/payments", status_code=200)
async def get_payment_history(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Return payment history for the authenticated user."""
    auth = request.headers.get("authorization", "")
    user = await get_token_user(auth, db)

    result = await db.execute(
        select(Payment)
        .where(Payment.user_id == user.id)
        .order_by(Payment.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    payments = result.scalars().all()
    return [
        {
            "id": p.id,
            "user_id": p.user_id,
            "user_email": user.email,
            "user_name": user.display_name,
            "subscription_id": p.subscription_id,
            "amount_cents": p.amount_cents,
            "currency": p.currency,
            "status": p.status,
            "description": p.description,
            "stripe_invoice_id": p.stripe_invoice_id,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in payments
    ]
