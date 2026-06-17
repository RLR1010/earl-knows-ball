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


class MLBGames(Base):
    __tablename__ = "games"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True)
    mlb_game_id = Column(Integer, unique=True, nullable=True, index=True)  # MLB Stats API gamePk
    season_id = Column(Integer, ForeignKey("mlb.seasons.id"), nullable=False)
    game_type = Column(String(10), default="REG")  # REG, PRE, POST, AS (All-Star)
    game_number = Column(Integer, default=0)  # Doubleheader game number (0 or 1)
    home_team_id = Column(Integer, ForeignKey("mlb.teams.id"), nullable=False)
    away_team_id = Column(Integer, ForeignKey("mlb.teams.id"), nullable=False)
    date = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(Enum(GameStatus), default=GameStatus.SCHEDULED)
    home_score = Column(Integer, nullable=True)
    away_score = Column(Integer, nullable=True)
    venue = Column(String(200), nullable=True)
    venue_id = Column(Integer, nullable=True)

    # Weather / conditions
    roof_type = Column(String(20), nullable=True)  # Open, Closed, Retractable
    surface = Column(String(50), nullable=True)
    temperature = Column(Integer, nullable=True)
    wind_speed = Column(Integer, nullable=True)
    wind_direction = Column(String(20), nullable=True)  # out, in, l_to_r, r_to_l
    weather_condition = Column(String(100), nullable=True)

    # Game details
    scheduled_innings = Column(Integer, default=9)
    actual_innings = Column(Integer, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    attendance = Column(Integer, nullable=True)
    day_night = Column(String(6), nullable=True)  # Day, Night

    # Home/Away splits
    home_wins = Column(Integer, nullable=True)
    home_losses = Column(Integer, nullable=True)
    away_wins = Column(Integer, nullable=True)
    away_losses = Column(Integer, nullable=True)

    # Starting pitchers (from live pipeline)
    home_pitcher_name = Column(String(100), nullable=True)
    away_pitcher_name = Column(String(100), nullable=True)

    season = relationship("MLBSeason", backref="games")
    home_team = relationship("MLBTeam", foreign_keys=[home_team_id], backref="home_games")
    away_team = relationship("MLBTeam", foreign_keys=[away_team_id], backref="away_games")
