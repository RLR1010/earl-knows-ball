"""Per-game batting statistics for each player (from MLB Stats API box scores)."""

from sqlalchemy import Column, Integer, Float, String, ForeignKey, DateTime, Date, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base


class MLBBattingGameStats(Base):
    """Per-game batting line for a single player."""

    __tablename__ = "batting_game_stats"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_id = Column(Integer, ForeignKey("mlb.games.id", ondelete="CASCADE"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("mlb.players.id", ondelete="CASCADE"), nullable=False, index=True)
    team_side = Column(String(4), nullable=False)  # "home" or "away"

    # Batting line
    at_bats = Column(Integer, default=0)
    runs = Column(Integer, default=0)
    hits = Column(Integer, default=0)
    doubles = Column(Integer, default=0)
    triples = Column(Integer, default=0)
    home_runs = Column(Integer, default=0)
    runs_batted_in = Column(Integer, default=0)
    base_on_balls = Column(Integer, default=0)
    strikeouts = Column(Integer, default=0)
    stolen_bases = Column(Integer, default=0)
    caught_stealing = Column(Integer, default=0)
    hit_by_pitch = Column(Integer, default=0)
    sacrifice_flies = Column(Integer, default=0)
    sacrifice_bunts = Column(Integer, default=0)
    left_on_base = Column(Integer, default=0)
    plate_appearances = Column(Integer, default=0)

    # Derived
    total_bases = Column(Integer, default=0)
    avg = Column(Float, nullable=True)
    obp = Column(Float, nullable=True)
    slg = Column(Float, nullable=True)
    ops = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    game = relationship("MLBGame", backref="batting_game_stats")
    player = relationship("MLBPlayer", backref="batting_game_stats")

    __table_args__ = (
        UniqueConstraint("game_id", "player_id", name="uq_game_player_batting"),
        {"schema": "mlb"},
    )
