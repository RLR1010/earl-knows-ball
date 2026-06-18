from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone
import uuid


class ChatHistory(Base):
    __tablename__ = "chat_history"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(36), ForeignKey("public.users.id"), nullable=False, index=True)
    conversation_id = Column(String(36), nullable=False, index=True, default=lambda: str(uuid.uuid4()))
    sport = Column(String(10), nullable=False, default="nfl", index=True)  # nfl, nba, mlb
    role = Column(String(20), nullable=False)  # user, assistant
    message = Column(Text, nullable=False)
    model = Column(String(50), nullable=True)
    tokens_used = Column(Integer, nullable=True)
    context_data = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user = relationship("User", backref="chat_messages")
