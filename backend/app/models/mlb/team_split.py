"""Team-level situational splits for MLB.

Stores batting/pitching performance broken down by context:
vs LHP/RHP, day/night, home/away, grass/turf, etc.

These are aggregated from per-player stats during data ingestion.
"""
from sqlalchemy import Column, Integer, Float, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


class MLBTeamSplit(Base):
    """Team-level situational splits for a given season."""

    __tablename__ = "team_splits"
    __table_args__ = (
        UniqueConstraint(
            "team_id", "season_id", "split_type",
            name="uq_mlb_team_split",
        ),
        {"schema": "mlb"},
    )

    id = Column(Integer, primary_key=True)
    team_id = Column(
        Integer,
        ForeignKey("mlb.teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    season_id = Column(
        Integer,
        ForeignKey("mlb.seasons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Split dimension: vs_lhp, vs_rhp, day, night, grass, turf, home, away
    split_type = Column(String(20), nullable=False)

    # Games
    games = Column(Integer, nullable=False, default=0)

    # Run differential
    runs_scored = Column(Integer, nullable=False, default=0)
    runs_allowed = Column(Integer, nullable=False, default=0)

    # Results
    wins = Column(Integer, nullable=False, default=0)
    losses = Column(Integer, nullable=False, default=0)

    # Batting
    avg = Column(Float, nullable=True)  # .xxx
    obp = Column(Float, nullable=True)
    slg = Column(Float, nullable=True)
    ops = Column(Float, nullable=True)
    home_runs = Column(Integer, nullable=False, default=0)

    # Pitching
    era = Column(Float, nullable=True)
    whip = Column(Float, nullable=True)

    # Relationships
    team = relationship("MLBTeam", backref="splits")
    season = relationship("MLBSeason", backref="team_splits")

    def __repr__(self) -> str:
        return (
            f"<MLBTeamSplit team_id={self.team_id} "
            f"season_id={self.season_id} type={self.split_type}>"
        )
