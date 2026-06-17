"""
DFS (Daily Fantasy Sports) salary model.

Tracks per-player, per-week salaries across DraftKings and FanDuel.
"""
from sqlalchemy import (
    Column, Integer, Float, String, DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone


class DfsSalary(Base):
    __tablename__ = "dfs_salaries"

    id = Column(Integer, primary_key=True)
    platform = Column(String(20), nullable=False, index=True)  # 'draftkings' or 'fanduel'
    player_name = Column(String(100), nullable=False)
    player_id = Column(Integer, ForeignKey("nfl.players.id"), nullable=True, index=True)
    position = Column(String(4), nullable=False)
    salary = Column(Integer, nullable=False)
    team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=True)
    opponent_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=True)
    game_id = Column(Integer, ForeignKey("nfl.games.id"), nullable=True, index=True)
    week = Column(Integer, nullable=True)
    season_id = Column(Integer, ForeignKey("nfl.seasons.id"), nullable=True)
    slate_type = Column(String(20), nullable=True)  # 'main', 'afternoon', 'primetime', 'showdown'
    game_time = Column(DateTime(timezone=True), nullable=True)
    scraped_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    player = relationship("Player", backref="dfs_salaries")
    team = relationship("Team", foreign_keys=[team_id])
    opponent = relationship("Team", foreign_keys=[opponent_id])
    game = relationship("Game", backref="dfs_salaries")
    season = relationship("Season")

    __table_args__ = (
        UniqueConstraint(
            "platform", "player_name", "week", "season_id", "slate_type",
            name="uq_dfs_salary_platform_player_week",
        ),
        {"schema": "nfl"},
    )

    def __repr__(self):
        return f"<DfsSalary {self.platform} {self.player_name} ${self.salary} W{self.week}>"
