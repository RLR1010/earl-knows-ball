from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, BigInteger
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone
import uuid


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "public"}

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=True)  # nullable — no longer used, legacy only
    display_name = Column(String(100), nullable=True)
    subscription_tier = Column(String(20), default="free")  # free, premium, premium_yearly
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    email_verified = Column(Boolean, default=False)
    monthly_token_limit = Column(BigInteger, nullable=True)
    # Purchased token bank (one-time top-ups, NOT the monthly allotment).
    # Rolls over between billing periods; used only as a fallback once the
    # monthly allotment is exhausted.
    extra_token_balance = Column(BigInteger, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    stripe_customer_id = Column(String(100), nullable=True)

    # Passwordless login fields
    login_code_hash = Column(String(255), nullable=True)
    login_code_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    token_usage = relationship("UserTokenUsage", back_populates="user", cascade="all, delete-orphan")
    activity = relationship("UserActivity", back_populates="user", cascade="all, delete-orphan")
