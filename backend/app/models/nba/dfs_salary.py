"""NBA DFS salary model."""
from sqlalchemy import (
    Column, Integer, Float, String, DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone


class NBADfsSalary(Base):
    __tablename__ = "dfs_salaries"
    __table_args__ = (
        UniqueConstraint(
            "platform", "player_name", "season_id",
            name="uq_nba_dfs_player_season",
        ),
        {"schema": "nba"},
    )

    id = Column(Integer, primary_key=True)
    platform = Column(String(20), nullable=False, index=True)
    player_name = Column(String(100), nullable=False)
    player_id = Column(Integer, ForeignKey("nba.players.id"), nullable=True, index=True)
    position = Column(String(10), nullable=False)
    salary = Column(Integer, nullable=False)
    team_id = Column(Integer, ForeignKey("nba.teams.id"), nullable=True)
    opponent_id = Column(Integer, ForeignKey("nba.teams.id"), nullable=True)
    game_id = Column(Integer, ForeignKey("nba.games.id"), nullable=True, index=True)
    season_id = Column(Integer, ForeignKey("nba.seasons.id"), nullable=True)
    slate_type = Column(String(20), nullable=True)
    game_time = Column(DateTime(timezone=True), nullable=True)
    scraped_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    player = relationship("NBAPlayer", backref="dfs_salaries")
    team = relationship("NBATeam", foreign_keys=[team_id])
    opponent = relationship("NBATeam", foreign_keys=[opponent_id])
    game = relationship("NBAGame", backref="dfs_salaries")
    season = relationship("NBASeason")
