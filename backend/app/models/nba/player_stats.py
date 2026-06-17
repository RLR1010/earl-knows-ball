from sqlalchemy import Column, Integer, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class NBAPlayerSeasonStats(Base):
    """Per-season NBA player statistics."""
    __tablename__ = "player_season_stats"
    __table_args__ = (
        UniqueConstraint("player_id", "season_id", name="uq_nba_player_season"),
        {"schema": "nba"},
    )

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("nba.players.id"), nullable=False, index=True)
    season_id = Column(Integer, ForeignKey("nba.seasons.id"), nullable=False, index=True)
    team_id = Column(Integer, ForeignKey("nba.teams.id"), nullable=True)

    # Games
    games_played = Column(Integer, default=0)
    games_started = Column(Integer, default=0)
    minutes_played = Column(Float, default=0.0)

    # Scoring
    points = Column(Integer, default=0)
    points_per_game = Column(Float, nullable=True)
    field_goals_made = Column(Integer, default=0)
    field_goals_attempted = Column(Integer, default=0)
    field_goal_pct = Column(Float, nullable=True)
    three_points_made = Column(Integer, default=0)
    three_points_attempted = Column(Integer, default=0)
    three_point_pct = Column(Float, nullable=True)
    free_throws_made = Column(Integer, default=0)
    free_throws_attempted = Column(Integer, default=0)
    free_throw_pct = Column(Float, nullable=True)

    # Rebounding
    rebounds = Column(Integer, default=0)
    offensive_rebounds = Column(Integer, default=0)
    defensive_rebounds = Column(Integer, default=0)
    rebounds_per_game = Column(Float, nullable=True)

    # Playmaking
    assists = Column(Integer, default=0)
    assists_per_game = Column(Float, nullable=True)
    turnovers = Column(Integer, default=0)
    assists_turnover_ratio = Column(Float, nullable=True)

    # Defense
    steals = Column(Integer, default=0)
    blocks = Column(Integer, default=0)
    personal_fouls = Column(Integer, default=0)

    # Advanced
    plus_minus = Column(Integer, nullable=True)
    efficiency = Column(Float, nullable=True)
    true_shooting_pct = Column(Float, nullable=True)
    usage_pct = Column(Float, nullable=True)

    # Fantasy
    fantasy_points = Column(Float, nullable=True)
    
    player = relationship("NBAPlayer", back_populates="season_stats")
    season = relationship("NBASeason")
    team = relationship("NBATeam")
