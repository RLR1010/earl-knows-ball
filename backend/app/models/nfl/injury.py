from sqlalchemy import Column, Integer, String, ForeignKey, Date
from sqlalchemy.orm import relationship
from app.database import Base


class Injury(Base):
    __tablename__ = "injuries"
    __table_args__ = {"schema": "nfl"}

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("nfl.players.id"), nullable=False, index=True)
    week = Column(Integer, nullable=False)
    season_id = Column(Integer, ForeignKey("nfl.seasons.id"), nullable=False)
    injury_type = Column(String(100), nullable=True)  # hamstring, ankle, etc.
    practice_status = Column(String(20), nullable=True)  # DNP, Limited, Full
    game_status = Column(String(20), nullable=True)  # Out, Doubtful, Questionable, Active
    date_reported = Column(Date, nullable=True)

    player = relationship("Player", backref="injuries")
