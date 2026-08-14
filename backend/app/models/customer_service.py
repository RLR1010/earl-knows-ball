"""Customer Service chat models.

Two tables (both in the public schema):
- cs_messages:  the actual support chat thread per user (user + assistant turns).
- cs_knowledge: the grounding knowledge base the support bot answers from
                (FAQ entries, Terms & Conditions, Privacy Statement).
"""

from sqlalchemy import (
    Column, Integer, BigInteger, DateTime, String, Text, Boolean,
    ForeignKey, Index,
)
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from app.database import Base


def _utcnow():
    return datetime.now(timezone.utc)


class CSMessage(Base):
    """A single message in a user's customer-service chat."""

    __tablename__ = "cs_messages"
    __table_args__ = (
        Index("ix_cs_messages_user_created", "user_id", "created_at"),
        {"schema": "public"},
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        String(36),
        ForeignKey("public.users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(16), nullable=False)  # "user" | "assistant"
    content = Column(Text, nullable=False)
    tokens_used = Column(Integer, nullable=False, default=0)
    model = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    user = relationship("User")


class CSKnowledge(Base):
    """A grounding document/entry the CS bot uses to answer (FAQ, ToS, Privacy)."""

    __tablename__ = "cs_knowledge"
    __table_args__ = {"schema": "public"}

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(40), nullable=False)  # "faq" | "terms" | "privacy"
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
