from sqlalchemy import Column, String, Text, Integer, Boolean, DateTime, ForeignKey, JSON, Numeric
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone
import uuid


class SubscriptionPlan(Base):
    """A subscription plan/tier that maps to a Stripe Price."""
    __tablename__ = "subscription_plans"
    __table_args__ = {"schema": "public"}

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)             # e.g. "Monthly Premium"
    slug = Column(String(50), unique=True, nullable=False)  # e.g. "premium_monthly"
    description = Column(Text, nullable=True)
    price_cents = Column(Integer, nullable=False)           # price in cents (e.g. 999 = $9.99)
    currency = Column(String(3), default="usd")
    interval = Column(String(10), nullable=False)            # "month" or "year"
    trial_days = Column(Integer, default=0)                 # free trial days
    features = Column(JSON, default=list)                    # ["AI Chat", "Advanced Stats", ...]
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    # Stripe
    stripe_price_id = Column(String(100), nullable=True)    # Stripe Price ID for this plan
    stripe_product_id = Column(String(100), nullable=True)  # Stripe Product ID

    # Relationships
    subscriptions = relationship("UserSubscription", back_populates="plan")


class UserSubscription(Base):
    """Tracks a user's subscription to a plan."""
    __tablename__ = "user_subscriptions"
    __table_args__ = {"schema": "public"}

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id = Column(String(36), ForeignKey("public.subscription_plans.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False, default="incomplete")  # incomplete, active, past_due, canceled, trialing, incomplete_expired
    current_period_start = Column(DateTime(timezone=True), nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    canceled_at = Column(DateTime(timezone=True), nullable=True)
    trial_end = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    # Stripe
    stripe_subscription_id = Column(String(100), nullable=True, unique=True)
    stripe_customer_id = Column(String(100), nullable=True)

    # Relationships
    user = relationship("User")
    plan = relationship("SubscriptionPlan", back_populates="subscriptions")
    payments = relationship("Payment", back_populates="subscription")


class Payment(Base):
    """Individual payment transactions (invoices from Stripe)."""
    __tablename__ = "payments"
    __table_args__ = {"schema": "public"}

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subscription_id = Column(String(36), ForeignKey("public.user_subscriptions.id", ondelete="SET NULL"), nullable=True)
    amount_cents = Column(Integer, nullable=False)
    currency = Column(String(3), default="usd")
    status = Column(String(20), nullable=False, default="pending")  # pending, succeeded, failed, refunded
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Stripe
    stripe_invoice_id = Column(String(100), nullable=True, unique=True)
    stripe_payment_intent_id = Column(String(100), nullable=True)

    # Relationships
    user = relationship("User")
    subscription = relationship("UserSubscription", back_populates="payments")
