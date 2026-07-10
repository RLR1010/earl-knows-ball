"""Team-level bullpen statistics for MLB.

Aggregated pitching stats filtered to relief pitchers only,
providing a single row per team per season.
"""
from sqlalchemy import Column, Integer, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


class MLBBullpenStat(Base):
    """Season-long bullpen performance for an MLB team."""

    __tablename__ = "bullpen_stats"
    __table_args__ = (
        UniqueConstraint(
            "team_id", "season_id",
            name="uq_mlb_bullpen_team_season",
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

    # Core rate stats
    era = Column(Float, nullable=True)
    whip = Column(Float, nullable=True)
    fip = Column(Float, nullable=True)

    # Counting stats
    innings_pitched = Column(Float, nullable=False, default=0.0)
    strikeouts = Column(Integer, nullable=False, default=0)
    walks = Column(Integer, nullable=False, default=0)
    hits = Column(Integer, nullable=False, default=0)
    home_runs = Column(Integer, nullable=False, default=0)
    batters_faced = Column(Integer, nullable=False, default=0)

    # Saves & holds
    saves = Column(Integer, nullable=False, default=0)
    blown_saves = Column(Integer, nullable=False, default=0)
    hold = Column(Integer, nullable=False, default=0)
    save_opportunities = Column(Integer, nullable=False, default=0)

    # Platoon splits (nullable — may not be available in all data sources)
    left_avg = Column(Float, nullable=True)
    right_avg = Column(Float, nullable=True)
    left_ops = Column(Float, nullable=True)
    right_ops = Column(Float, nullable=True)

    # Relationships
    team = relationship("MLBTeam", backref="bullpen_stats")
    season = relationship("MLBSeason", backref="bullpen_stats")

    def __repr__(self) -> str:
        return (
            f"<MLBBullpenStat team_id={self.team_id} "
            f"season_id={self.season_id} era={self.era}>"
        )
