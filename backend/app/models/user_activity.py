"""User IP + daily usage activity tracking.

One row per (user, calendar day, ip_address) — day-level presence so the
table stays tiny. hit_count increments on repeat hits the same day from the
same IP; recording is fire-and-forget and never blocks the request path.
"""

from sqlalchemy import (
    Column, Integer, BigInteger, Date, DateTime, String,
    ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


class UserActivity(Base):
    """Tracks daily site usage per user + IP."""

    __tablename__ = "user_activity"

    id = Column(BigInteger, primary_key=True)
    user_id = Column(
        String(36), ForeignKey("public.users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    activity_date = Column(Date, nullable=False, index=True)  # calendar day
    ip_address = Column(String(45), nullable=False)           # IPv4 or IPv6
    first_seen = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    last_seen = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    hit_count = Column(Integer, nullable=False, default=1)

    user = relationship("User", back_populates="activity")

    __table_args__ = (
        UniqueConstraint(
            "user_id", "activity_date", "ip_address",
            name="uq_user_activity_date_ip",
        ),
        {"schema": "public"},
    )
