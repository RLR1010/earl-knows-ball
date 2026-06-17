from sqlalchemy import Column, Integer, Float, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class MLBBattingStats(Base):
    """Per-season batting statistics for MLB players."""
    __tablename__ = "batting_stats"
    __table_args__ = (
        UniqueConstraint("player_id", "season_id", name="uq_mlb_batting_player_season"),
        {"schema": "mlb"},
    )

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("mlb.players.id"), nullable=False, index=True)
    season_id = Column(Integer, ForeignKey("mlb.seasons.id"), nullable=False, index=True)
    team_id = Column(Integer, ForeignKey("mlb.teams.id"), nullable=True)

    # Core counting stats
    games_played = Column(Integer, default=0)
    plate_appearances = Column(Integer, default=0)
    at_bats = Column(Integer, default=0)
    runs = Column(Integer, default=0)
    hits = Column(Integer, default=0)
    doubles = Column(Integer, default=0)
    triples = Column(Integer, default=0)
    home_runs = Column(Integer, default=0)
    runs_batted_in = Column(Integer, default=0)
    stolen_bases = Column(Integer, default=0)
    caught_stealing = Column(Integer, default=0)

    # Walks & strikeouts
    base_on_balls = Column(Integer, default=0)
    intentional_walks = Column(Integer, default=0)
    strikeouts = Column(Integer, default=0)
    hit_by_pitch = Column(Integer, default=0)

    # Sacrifices
    sacrifice_flies = Column(Integer, default=0)
    sacrifice_bunts = Column(Integer, default=0)

    # Outs
    ground_outs = Column(Integer, default=0)
    air_outs = Column(Integer, default=0)
    ground_into_double_play = Column(Integer, default=0)

    # Rate stats
    avg = Column(Float, nullable=True)  # Batting average
    obp = Column(Float, nullable=True)  # On-base percentage
    slg = Column(Float, nullable=True)  # Slugging percentage
    ops = Column(Float, nullable=True)  # On-base + slugging
    babip = Column(Float, nullable=True)  # Batting average on balls in play

    # Derived
    total_bases = Column(Integer, default=0)
    at_bats_per_home_run = Column(Float, nullable=True)
    stolen_base_percentage = Column(Float, nullable=True)

    player = relationship("MLBPlayer", back_populates="batting_stats")
    season = relationship("MLBSeason")
    team = relationship("MLBTeam")
