from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey, Boolean, DateTime, func
from sqlalchemy.orm import relationship
from app.database import Base


class MLBInjury(Base):
    __tablename__ = "injuries"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, nullable=False, index=True)
    team_id = Column(
        Integer, ForeignKey("mlb.teams.id", ondelete="CASCADE"), nullable=True, index=True
    )
    injury_type = Column(String(100), nullable=True)
    injury_date = Column(Date, nullable=True)
    expected_return = Column(Date, nullable=True)
    status = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    is_active = Column(Boolean, nullable=True, default=True)

    # No SQLAlchemy FK for player_id (no constraint in DB)
    player = relationship("MLBPlayer", backref="injuries", foreign_keys=[player_id])
