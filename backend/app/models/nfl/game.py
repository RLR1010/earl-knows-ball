from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey, Enum
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class GameStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class Game(Base):
    __tablename__ = "games"
    __table_args__ = {"schema": "nfl"}

    id = Column(Integer, primary_key=True)
    season_id = Column(Integer, ForeignKey("nfl.seasons.id"), nullable=False)
    week = Column(Integer, nullable=False, index=True)
    game_type = Column(String(10), default="REG")  # REG, PRE, POST
    home_team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=False)
    away_team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=False)
    date = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(Enum(GameStatus), default=GameStatus.SCHEDULED)
    home_score = Column(Integer, nullable=True)
    away_score = Column(Integer, nullable=True)
    venue = Column(String(200), nullable=True)
    roof_type = Column(String(20), nullable=True)  # dome, outdoor, retractable
    surface = Column(String(50), nullable=True)
    temperature = Column(Integer, nullable=True)
    wind_speed = Column(Integer, nullable=True)
    weather_condition = Column(String(100), nullable=True)

    season = relationship("Season", backref="games")
    home_team = relationship("Team", foreign_keys=[home_team_id], backref="home_games")
    away_team = relationship("Team", foreign_keys=[away_team_id], backref="away_games")
