from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.database import Base


class MLBInjury(Base):
    __tablename__ = "injuries"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True)
    player_id = Column(
        Integer, ForeignKey("mlb.players.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id = Column(
        Integer, ForeignKey("mlb.teams.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status = Column(String(100), nullable=True)  # e.g. "10-Day IL", "60-Day IL"
    description = Column(Text, nullable=True)  # e.g. "Right elbow inflammation"
    date_added = Column(Date, nullable=True, index=True)  # When they went on IL
    is_active = Column(Boolean, nullable=True, default=True)  # True = still injured

    player = relationship("MLBPlayer", backref="injuries")
