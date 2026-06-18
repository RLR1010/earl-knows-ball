from sqlalchemy import Column, Integer, Float, String, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class NBAPlayerGameStats(Base):
    __tablename__ = "player_game_stats"
    __table_args__ = {"schema": "nba"}

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("nba.games.id"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("nba.players.id"), nullable=False, index=True)
    team_id = Column(Integer, ForeignKey("nba.teams.id"), nullable=False)

    # Player info at game time
    position = Column(String(10), nullable=True)
    jersey_number = Column(String(5), nullable=True)
    is_starter = Column(Boolean, default=False)

    # Game stats
    minutes = Column(String(10), nullable=True)  # "29:55" format
    field_goals_made = Column(Integer, nullable=True)
    field_goals_attempted = Column(Integer, nullable=True)
    field_goal_pct = Column(Float, nullable=True)
    three_pointers_made = Column(Integer, nullable=True)
    three_pointers_attempted = Column(Integer, nullable=True)
    three_pointer_pct = Column(Float, nullable=True)
    free_throws_made = Column(Integer, nullable=True)
    free_throws_attempted = Column(Integer, nullable=True)
    free_throw_pct = Column(Float, nullable=True)
    rebounds_offensive = Column(Integer, nullable=True)
    rebounds_defensive = Column(Integer, nullable=True)
    rebounds_total = Column(Integer, nullable=True)
    assists = Column(Integer, nullable=True)
    steals = Column(Integer, nullable=True)
    blocks = Column(Integer, nullable=True)
    turnovers = Column(Integer, nullable=True)
    fouls_personal = Column(Integer, nullable=True)
    points = Column(Integer, nullable=True)
    plus_minus = Column(Float, nullable=True)
    fantasy_points = Column(Float, nullable=True)

    # NBA API game ID (0022501053 format) for reference
    nba_game_id = Column(String(20), nullable=True, index=True)
    nba_player_id = Column(Integer, nullable=True)

    scraped_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    game = relationship("NBAGame", backref="player_stats")
    player = relationship("NBAPlayer", backref="game_stats")
    team = relationship("NBATeam", backref="player_game_stats")
