"""Token usage tracking for chat API calls."""

from sqlalchemy import (
    Column, Integer, BigInteger, Date, DateTime, String,
    ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


class UserTokenUsage(Base):
    """Tracks monthly DeepSeek token usage per user."""

    __tablename__ = "user_token_usage"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(36), ForeignKey("public.users.id", ondelete="CASCADE"), nullable=False, index=True)
    month = Column(Date, nullable=False)  # First day of the month: 2026-07-01
    tokens_used = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="token_usage")

    __table_args__ = (
        UniqueConstraint("user_id", "month", name="uq_user_month_tokens"),
        {"schema": "public"},
    )
