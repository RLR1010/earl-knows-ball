from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, func, case
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
    ui_mode: str = "hosted"  # "hosted" (redirect) or "embedded_page" (modal)


class TokenTopupRequest(BaseModel):
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None
    ui_mode: str = "hosted"  # "hosted" (redirect) or "embedded_page" (modal)


class CheckoutResponse(BaseModel):
    url: str | None = None
    client_secret: str | None = None
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
    user = await get_current_user(request, db)

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
        session_kwargs = {
            "customer": customer_id,
            "mode": "subscription",
            "line_items": [{"price": plan.stripe_price_id, "quantity": 1}],
            "metadata": {
                "user_id": user.id,
                "plan_id": plan.id,
            },
            "subscription_data": {
                "metadata": {
                    "user_id": user.id,
                    "plan_id": plan.id,
                },
                "trial_period_days": plan.trial_days or None,
            },
        }

        if req.ui_mode == "embedded_page":
            # Embedded Checkout — renders in-page modal.
            # Use the caller-supplied success_url (origin-correct) as the return
            # URL so post-payment redirect lands back on the SAME host the user
            # was browsing (www vs apex). Hardcoding settings.base_url here broke
            # sessions when the user browsed on a different subdomain than
            # BASE_URL, redirecting them to an origin with no logged-in session.
            session_kwargs["ui_mode"] = "embedded_page"
            session_kwargs["return_url"] = req.success_url or f"{settings.base_url}/profile?subscription=success"
            session = stripe.checkout.Session.create(**session_kwargs)
            return CheckoutResponse(client_secret=session.client_secret)
        else:
            # Hosted Checkout — redirect to Stripe
            session_kwargs["success_url"] = success_url
            session_kwargs["cancel_url"] = cancel_url
            session = stripe.checkout.Session.create(**session_kwargs)
            return CheckoutResponse(url=session.url)

    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        raise HTTPException(status_code=500, detail=f"Payment processing error: {str(e)}")


# ── Token Top-Up (one-time purchase) ─────────────────────────────

@router.post("/token-topup/checkout", response_model=CheckoutResponse)
async def create_token_topup_checkout(
    req: TokenTopupRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a one-time (non-recurring) Stripe Checkout Session for an
    additional token top-up. The purchased tokens are credited to the user's
    extra_token_balance via the checkout.session.completed webhook and roll
    over between billing periods (they are NOT part of the monthly allotment)."""
    user = await get_current_user(request, db)

    # Read the one-time token top-up plan from the DB so it's admin-configurable
    # (Stripe product id, price, token grant). Falls back to settings for back-compat.
    plan = (await db.execute(
        select(SubscriptionPlan)
        .where(SubscriptionPlan.kind == "token_topup", SubscriptionPlan.is_active == True)
        .order_by(SubscriptionPlan.sort_order)
    )).scalars().first()

    if plan:
        price_id = plan.stripe_price_id or plan.stripe_product_id
        grant = plan.token_amount or settings.token_topup_grant
        price_cents = plan.price_cents
    else:
        price_id = settings.stripe_token_topup_price_id
        grant = settings.token_topup_grant
        price_cents = int(round(grant / 1000 * 19.95)) if grant == settings.token_topup_grant else 1995

    if not price_id:
        raise HTTPException(status_code=503, detail="Token top-up is not configured")

    if not _stripe_available():
        return CheckoutResponse(
            url=None,
            mock=True,
            message=f"Stripe not configured. Would charge ${grant/1000:.2f} one-time for {grant:,} extra tokens.",
        )

    try:
        stripe = _get_stripe()
        customer_id = await _get_or_create_stripe_customer(user, stripe)
        # Origin-correct return URL (user browsed on www vs apex) — embedded mode.
        success_url = req.success_url or f"{settings.base_url}/profile?token_topup=success"
        cancel_url = req.cancel_url or f"{settings.base_url}/profile"

        session_kwargs = {
            "customer": customer_id,
            "mode": "payment",  # one-time, non-recurring
            "line_items": [{"price": price_id, "quantity": 1}],
            "metadata": {
                "user_id": user.id,
                "type": "token_topup",
                "tokens": str(grant),
            },
            "payment_intent_data": {
                "metadata": {
                    "user_id": user.id,
                    "type": "token_topup",
                    "tokens": str(grant),
                },
                "description": "Earl token top-up: additional chat tokens (one-time)",
            },
        }

        if req.ui_mode == "embedded_page":
            # Embedded Checkout — renders in-page (modal/panel), user never leaves
            # the site. Return client_secret for initEmbeddedCheckout to mount.
            session_kwargs.update({
                "ui_mode": "embedded_page",
                "return_url": success_url,
            })
            session = stripe.checkout.Session.create(**session_kwargs)
            return CheckoutResponse(url=None, client_secret=session.client_secret)

        # Hosted redirect (fallback)
        session_kwargs.update({"success_url": success_url, "cancel_url": cancel_url})
        session = stripe.checkout.Session.create(**session_kwargs)
        return CheckoutResponse(url=session.url, client_secret=session.client_secret)

    except Exception as e:
        logger.error(f"Stripe token top-up checkout error: {e}")
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

    # Normalize Stripe objects to plain dicts for uniform .get() access
    if hasattr(event, "to_dict"):
        raw = event.to_dict()
        event_type = raw.get("type", "")
        data_object = raw.get("data", {}).get("object", raw)
    else:
        event_type = event.get("type", "")
        data_object = event.get("data", {}).get("object", event)

    logger.info(f"Stripe webhook: {event_type}")

    try:
        if event_type == "checkout.session.completed":
            # Branch: one-time token top-up vs. subscription checkout
            role = (data_object.get("metadata") or {}).get("type")
            if role == "token_topup":
                await _handle_token_topup_completed(data_object, db)
            else:
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


def _subscription_period(sub) -> tuple:
    """Extract (current_period_start_ts, current_period_end_ts) from a Stripe
    subscription object, robust to API field-location changes.

    In current Stripe API versions these live on the SubscriptionItem
    (sub.items.data[0].current_period_start/end); in older versions they were
    top-level on the Subscription. Accepts a StripeObject or a dict.
    """
    def _get(obj, *keys):
        for k in keys:
            if isinstance(obj, dict):
                if k in obj and obj[k] is not None:
                    return obj[k]
            else:
                try:
                    v = getattr(obj, k)
                    if v is not None:
                        return v
                except Exception:
                    pass
        return None

    start = _get(sub, "current_period_start")
    end = _get(sub, "current_period_end")

    if start is None or end is None:
        item = _get(sub, "items")
        # items may be a dict {data: [...]}, a stripe ListObject, a list, or an item
        if item is not None and hasattr(item, "data"):
            items = item.data or []
        elif isinstance(item, dict):
            items = item.get("data") or []
        elif isinstance(item, (list, tuple)):
            items = item
        else:
            items = [item] if item else []
        if items:
            first = items[0]
            if start is None:
                start = _get(first, "current_period_start")
            if end is None:
                end = _get(first, "current_period_end")

    return (start, end)


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
            start_ts, end_ts = _subscription_period(stripe_sub)
            if start_ts:
                period_start = datetime.fromtimestamp(start_ts, tz=timezone.utc)
            if end_ts:
                period_end = datetime.fromtimestamp(end_ts, tz=timezone.utc)
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
            cancel_at_period_end=session.get("cancel_at_period_end", False),
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

            if plan.monthly_token_limit is not None:
                user.monthly_token_limit = plan.monthly_token_limit

    await db.commit()


async def _handle_token_topup_completed(checkout_session: dict, db: AsyncSession):
    """Handle a one-time token top-up checkout completing.

    Credits the purchased tokens to user.extra_token_balance. This is a
    one-time purchase (not a subscription) and is NOT tied to the monthly
    allotment, so it rolls over between billing periods.
    """
    metadata = checkout_session.get("metadata") or {}
    user_id = str(metadata.get("user_id") or "")
    try:
        grant = int(metadata.get("tokens") or 0)
    except (TypeError, ValueError):
        grant = 0
    payment_status = checkout_session.get("payment_status")

    if not user_id:
        logger.warning("Token top-up checkout missing user_id in metadata")
        return
    if grant <= 0:
        logger.warning(f"Token top-up checkout for user {user_id} had no token grant")
        return
    if payment_status not in ("paid", "no_payment_required"):
        logger.info(f"Token top-up checkout {checkout_session.get('id')} not paid yet ({payment_status}); ignoring")
        return

    user = await db.get(User, user_id)
    if not user:
        logger.warning(f"Token top-up for unknown user {user_id}")
        return

    user.extra_token_balance = (user.extra_token_balance or 0) + grant
    await db.commit()
    logger.info(
        f"Token top-up credited: user={user_id} += {grant} (now {user.extra_token_balance}). "
        f"Checkout={checkout_session.get('id')}"
    )

    # Record the one-time payment for the user's history (no subscription)
    try:
        # Prefer the admin-editable plan payment_description (set in /admin/plans);
        # fall back to a readable default.
        tokplan = (
            await db.execute(
                select(SubscriptionPlan).where(
                    SubscriptionPlan.kind == "token_topup",
                    SubscriptionPlan.is_active == True,  # noqa: E712
                ).order_by(SubscriptionPlan.sort_order)
            )
        ).scalars().first()
        if tokplan and (tokplan.payment_description or "").strip():
            desc = tokplan.payment_description.strip()
        else:
            desc = "Token top-up: additional chat tokens (one-time)"
        amount_cents = checkout_session.get("amount_total") or 0
        payment = Payment(
            user_id=user.id,
            subscription_id=None,
            amount_cents=int(amount_cents),
            currency=(checkout_session.get("currency") or "usd").lower(),
            status="succeeded",
            stripe_payment_intent_id=(checkout_session.get("payment_intent") or None),
            stripe_invoice_id=None,
            description=desc,
        )
        db.add(payment)
        await db.commit()
    except Exception as e:
        logger.warning(f"Could not record token top-up payment row: {e}")


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
    if "cancel_at_period_end" in subscription:
        sub.cancel_at_period_end = bool(subscription["cancel_at_period_end"])
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

    # Sync user tier — only downgrade if no other active subscriptions exist
    user_result = await db.execute(select(User).where(User.id == sub.user_id))
    user = user_result.scalar_one_or_none()
    if user:
        if status == "active":
            user.subscription_tier = "premium"
            # Sync monthly token limit from the plan
            if sub.plan_id:
                plan_result = await db.execute(
                    select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id)
                )
                plan = plan_result.scalar_one_or_none()
                if plan and plan.monthly_token_limit is not None:
                    user.monthly_token_limit = plan.monthly_token_limit
        elif status in ("canceled", "past_due", "incomplete_expired", "unpaid"):
            # Check if user has any other active subscription before downgrading
            active_count = await db.scalar(
                select(func.count()).select_from(UserSubscription).where(
                    UserSubscription.user_id == user.id,
                    UserSubscription.stripe_subscription_id != stripe_sub_id,
                    UserSubscription.status.in_(["active", "trialing", "incomplete"])
                )
            )
            if not active_count:
                user.subscription_tier = "free"
            else:
                logger.info(f"User {user.id} has {active_count} other active subscription(s); not downgrading")

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
    sub.cancel_at_period_end = False

    # Reset user tier — only downgrade if no other active subscriptions exist
    user_result = await db.execute(select(User).where(User.id == sub.user_id))
    user = user_result.scalar_one_or_none()
    if user:
        active_count = await db.scalar(
            select(func.count()).select_from(UserSubscription).where(
                UserSubscription.user_id == user.id,
                UserSubscription.stripe_subscription_id != stripe_sub_id,
                UserSubscription.status.in_(["active", "trialing", "incomplete"])
            )
        )
        if not active_count:
            user.subscription_tier = "free"
            user.monthly_token_limit = None
            logger.info(f"Downgraded user {user.id} to free (no remaining active subscriptions)")
        else:
            logger.info(f"User {user.id} has {active_count} other active subscription(s); keeping tier")

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
    # If the subscription's plan has a `payment_description`, use it as the
    # payment-history label (admin-editable from /admin/plans). Otherwise fall
    # back to a readable default rather than a bare Stripe invoice number.
    description = f"Invoice {stripe_invoice_id}"
    if sub.plan_id:
        plan_result = await db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id)
        )
        plan = plan_result.scalar_one_or_none()
        if plan:
            label = (plan.payment_description or "").strip()
            description = (
                label
                if label
                else (plan.name or "Subscription") + " — Membership"
            )

    payment = Payment(
        user_id=sub.user_id,
        subscription_id=sub.id,
        amount_cents=amount_paid,
        currency=currency,
        status="succeeded" if status == "paid" else status,
        description=description,
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
    user = await get_current_user(request, db)

    result = await db.execute(
        select(UserSubscription)
        .where(UserSubscription.user_id == user.id)
        .order_by(
            case(
                (UserSubscription.status.in_(["active", "trialing", "incomplete"]), 0),
                else_=1
            ),
            UserSubscription.created_at.desc()
        )
        .limit(1)
    )
    sub = result.scalar_one_or_none()

    if not sub:
        # Fall back to user's subscription_tier for accounts upgraded outside user_subscriptions
        if user.subscription_tier and user.subscription_tier != "free":
            return SubscriptionStatus(
                has_active=True,
                subscription={
                    "id": None,
                    "status": "active",
                    "plan": {
                        "name": user.subscription_tier.replace("_", " ").title(),
                        "price_cents": None,
                        "interval": None,
                        "features": [],
                    },
                    "current_period_start": None,
                    "current_period_end": None,
                    "canceled_at": None,
                    "cancel_at_period_end": False,
                    "trial_end": None,
                },
            )
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
        "cancel_at_period_end": sub.cancel_at_period_end,
        "trial_end": sub.trial_end.isoformat() if sub.trial_end else None,
    }

    has_active = sub.status in ("active", "trialing") or (
        sub.status == "incomplete" and sub.current_period_end and sub.current_period_end > datetime.now(timezone.utc)
    )

    return SubscriptionStatus(
        has_active=has_active,
        subscription=sub_data,
    )


# ── Cancel My Subscription ──────────────────────────────────────────

@router.post("/cancel")
async def cancel_subscription(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Cancel the current user's active subscription."""
    user = await get_current_user(request, db)

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
        # User may have subscription_tier set directly without user_subscriptions record
        if user.subscription_tier and user.subscription_tier != "free":
            user.subscription_tier = "free"
            await db.commit()
            return {"status": "canceled", "message": "Subscription canceled."}
        raise HTTPException(status_code=404, detail="No active subscription found")

    if _stripe_available() and sub.stripe_subscription_id:
        try:
            stripe = _get_stripe()
            stripe.Subscription.modify(
                sub.stripe_subscription_id,
                cancel_at_period_end=True,
            )
        except Exception as e:
            logger.error(f"Stripe cancel error: {e}")
            raise HTTPException(status_code=500, detail="Failed to cancel with payment processor")

    sub.cancel_at_period_end = True
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
    """Return payment history for the authenticated user (paginated).

    Body is a bare array (preserves the existing frontend contract).
    Total match count is exposed via the X-Total-Count response header so
    the client can render pagination controls.
    """
    user = await get_current_user(request, db)

    base_query = select(Payment).where(Payment.user_id == user.id)
    count_sel = select(func.count()).select_from(base_query.subquery())
    total = (await db.execute(count_sel)).scalar() or 0

    result = await db.execute(
        base_query.order_by(Payment.created_at.desc()).offset(offset).limit(limit)
    )
    payments = result.scalars().all()
    return JSONResponse(
        content=[
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
        ],
        headers={"X-Total-Count": str(total)},
    )
