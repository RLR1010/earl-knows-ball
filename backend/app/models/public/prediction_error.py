from sqlalchemy import Column, Integer, String, Text, DateTime, func
from app.database import Base


class PredictionError(Base):
    """Log of failed prediction attempts, persisted across restarts."""

    __tablename__ = "prediction_errors"
    __table_args__ = {"schema": "public"}

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, nullable=False, index=True)
    sport = Column(String(10), nullable=False, default="mlb")
    error_type = Column(String(50), nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
