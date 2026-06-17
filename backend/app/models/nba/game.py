from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey, Enum
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class NBAGameStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class NBAGame(Base):
    __tablename__ = "games"
    __table_args__ = {"schema": "nba"}

    id = Column(Integer, primary_key=True)
    nba_game_id = Column(String(20), unique=True, nullable=True, index=True)  # NBA.com game ID
    season_id = Column(Integer, ForeignKey("nba.seasons.id"), nullable=False)
    game_type = Column(String(10), default="REG")  # REG, PRE, POST, AS
    home_team_id = Column(Integer, ForeignKey("nba.teams.id"), nullable=False)
    away_team_id = Column(Integer, ForeignKey("nba.teams.id"), nullable=False)
    date = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(Enum(NBAGameStatus), default=NBAGameStatus.SCHEDULED)
    home_score = Column(Integer, nullable=True)
    away_score = Column(Integer, nullable=True)
    venue = Column(String(200), nullable=True)
    attendance = Column(Integer, nullable=True)

    # Team stats (from game log)
    home_field_goals_made = Column(Integer, nullable=True)
    home_field_goals_attempted = Column(Integer, nullable=True)
    home_three_points_made = Column(Integer, nullable=True)
    home_three_points_attempted = Column(Integer, nullable=True)
    home_free_throws_made = Column(Integer, nullable=True)
    home_free_throws_attempted = Column(Integer, nullable=True)
    home_rebounds = Column(Integer, nullable=True)
    home_assists = Column(Integer, nullable=True)
    home_steals = Column(Integer, nullable=True)
    home_blocks = Column(Integer, nullable=True)
    home_turnovers = Column(Integer, nullable=True)
    home_fouls = Column(Integer, nullable=True)

    away_field_goals_made = Column(Integer, nullable=True)
    away_field_goals_attempted = Column(Integer, nullable=True)
    away_three_points_made = Column(Integer, nullable=True)
    away_three_points_attempted = Column(Integer, nullable=True)
    away_free_throws_made = Column(Integer, nullable=True)
    away_free_throws_attempted = Column(Integer, nullable=True)
    away_rebounds = Column(Integer, nullable=True)
    away_assists = Column(Integer, nullable=True)
    away_steals = Column(Integer, nullable=True)
    away_blocks = Column(Integer, nullable=True)
    away_turnovers = Column(Integer, nullable=True)
    away_fouls = Column(Integer, nullable=True)

    season = relationship("NBASeason", backref="games")
    home_team = relationship("NBATeam", foreign_keys=[home_team_id], backref="home_games")
    away_team = relationship("NBATeam", foreign_keys=[away_team_id], backref="away_games")
