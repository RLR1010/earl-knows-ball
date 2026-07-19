from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
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
    subscription_tier = Column(String(20), default="free")  # free, premium, ultimate
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    email_verified = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    stripe_customer_id = Column(String(100), nullable=True)

    # Passwordless login fields
    login_code_hash = Column(String(255), nullable=True)
    login_code_expires_at = Column(DateTime(timezone=True), nullable=True)
