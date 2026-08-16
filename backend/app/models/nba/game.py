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

    # ── Team box-score stats from ESPN core API team-statistics endpoint ──
    home_offensive_rebounds = Column(Integer, nullable=True)
    away_offensive_rebounds = Column(Integer, nullable=True)
    home_defensive_rebounds = Column(Integer, nullable=True)
    away_defensive_rebounds = Column(Integer, nullable=True)

    home_two_point_field_goals_made = Column(Integer, nullable=True)
    away_two_point_field_goals_made = Column(Integer, nullable=True)
    home_two_point_field_goals_attempted = Column(Integer, nullable=True)
    away_two_point_field_goals_attempted = Column(Integer, nullable=True)
    home_two_point_field_goal_pct = Column(Float, nullable=True)
    away_two_point_field_goal_pct = Column(Float, nullable=True)

    home_points_in_paint = Column(Integer, nullable=True)
    away_points_in_paint = Column(Integer, nullable=True)
    home_fast_break_points = Column(Integer, nullable=True)
    away_fast_break_points = Column(Integer, nullable=True)
    home_turnover_points = Column(Integer, nullable=True)
    away_turnover_points = Column(Integer, nullable=True)

    home_team_turnovers = Column(Integer, nullable=True)
    away_team_turnovers = Column(Integer, nullable=True)
    home_total_turnovers = Column(Integer, nullable=True)
    away_total_turnovers = Column(Integer, nullable=True)

    home_estimated_possessions = Column(Float, nullable=True)
    away_estimated_possessions = Column(Float, nullable=True)
    home_offensive_rebound_pct = Column(Float, nullable=True)
    away_offensive_rebound_pct = Column(Float, nullable=True)
    home_points_per_estimated_possessions = Column(Float, nullable=True)
    away_points_per_estimated_possessions = Column(Float, nullable=True)
    home_scoring_efficiency = Column(Float, nullable=True)
    away_scoring_efficiency = Column(Float, nullable=True)
    home_shooting_efficiency = Column(Float, nullable=True)
    away_shooting_efficiency = Column(Float, nullable=True)
    home_brick_index = Column(Float, nullable=True)
    away_brick_index = Column(Float, nullable=True)

    home_field_goals_that_made_possession = Column(Float, nullable=True)
    away_field_goals_that_made_possession = Column(Float, nullable=True)

    home_lead_changes = Column(Integer, nullable=True)
    away_lead_changes = Column(Integer, nullable=True)
    home_largest_lead = Column(Integer, nullable=True)
    away_largest_lead = Column(Integer, nullable=True)
    home_lead_percentage = Column(Float, nullable=True)
    away_lead_percentage = Column(Float, nullable=True)

    home_technical_fouls = Column(Integer, nullable=True)
    away_technical_fouls = Column(Integer, nullable=True)
    home_flagrant_fouls = Column(Integer, nullable=True)
    away_flagrant_fouls = Column(Integer, nullable=True)
    home_ejections = Column(Integer, nullable=True)
    away_ejections = Column(Integer, nullable=True)
    home_disqualifications = Column(Integer, nullable=True)
    away_disqualifications = Column(Integer, nullable=True)

    home_double_double = Column(Integer, nullable=True)
    away_double_double = Column(Integer, nullable=True)
    home_triple_double = Column(Integer, nullable=True)
    away_triple_double = Column(Integer, nullable=True)

    home_assist_turnover_ratio = Column(Float, nullable=True)
    away_assist_turnover_ratio = Column(Float, nullable=True)
    home_steal_turnover_ratio = Column(Float, nullable=True)
    away_steal_turnover_ratio = Column(Float, nullable=True)
    home_steal_foul_ratio = Column(Float, nullable=True)
    away_steal_foul_ratio = Column(Float, nullable=True)
    home_block_foul_ratio = Column(Float, nullable=True)
    away_block_foul_ratio = Column(Float, nullable=True)
    home_team_assist_turnover_ratio = Column(Float, nullable=True)
    away_team_assist_turnover_ratio = Column(Float, nullable=True)

    home_nba_rating = Column(Float, nullable=True)
    away_nba_rating = Column(Float, nullable=True)
    home_vorp = Column(Float, nullable=True)
    away_vorp = Column(Float, nullable=True)

    season = relationship("NBASeason", backref="games")
    home_team = relationship("NBATeam", foreign_keys=[home_team_id], backref="home_games")
    away_team = relationship("NBATeam", foreign_keys=[away_team_id], backref="away_games")
