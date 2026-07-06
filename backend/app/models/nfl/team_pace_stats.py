"""
Team-level pace metrics for NFL.

Tracks how many plays each team runs (offense) and faces (defense) per game.
Derived from nflverse snap_counts data.

Pace correlates strongly with total points scored in games:
  - More offensive snaps = more scoring opportunities = higher totals
  - Faster pace teams push O/U toward the Over
  - Slow pace teams push O/U toward the Under
"""
from sqlalchemy import (
    Column, Integer, Float, String, DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone


class TeamPaceStats(Base):
    __tablename__ = "team_pace_stats"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("nfl.games.id"), nullable=False, index=True)
    team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=False, index=True)
    season_id = Column(Integer, ForeignKey("nfl.seasons.id"), nullable=False)
    week = Column(Integer, nullable=False)
    season_type = Column(String(10), nullable=True)  # REG, POST

    # Snaps/plays
    offensive_snaps = Column(Integer, nullable=False, default=0)
    defensive_snaps = Column(Integer, nullable=False, default=0)
    special_teams_snaps = Column(Integer, nullable=False, default=0)

    # Derived pace metrics
    total_snaps = Column(Integer, nullable=False, default=0)  # off + def + st
    offensive_players = Column(Integer, nullable=True)  # number of players who took offensive snaps
    defensive_players = Column(Integer, nullable=True)  # number of players who took defensive snaps

    # Metadata
    source = Column(String(50), nullable=True)  # 'nflverse_snap_counts'
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    game = relationship("Game", backref="team_pace_stats")
    team = relationship("Team")
    season = relationship("Season")

    __table_args__ = (
        UniqueConstraint(
            "game_id", "team_id",
            name="uq_team_pace_game_team",
        ),
        {"schema": "nfl"},
    )

    def __repr__(self):
        return (
            f"<TeamPaceStats team={self.team_id} game={self.game_id} "
            f"off={self.offensive_snaps} def={self.defensive_snaps}>"
        )
