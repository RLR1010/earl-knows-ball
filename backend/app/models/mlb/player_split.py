"""Per-player split statistics for MLB (platoon / situational research).

Splits stored: batter vs LHP/vs RHP, home/away, day/night, grass/turf, and
city splits (derived from the game's home-team city). ``season_id IS NULL``
means the CAREER aggregate.

Used by Earl's chat research (``get_player_split_stats``) and the premium
Prop Bets writeup (``writeups/props_mlb.py``) so Earl can quote things like
"Ramirez hits .322 vs LHP, .289 at home" from real data.
"""

from datetime import datetime

from sqlalchemy import Column, Integer, Float, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


class MLBPlayerSplit(Base):
    """Per-player, per-split-type batting stats for a season or career."""

    __tablename__ = "player_splits"
    __table_args__ = (
        UniqueConstraint("player_id", "split_type", "season_id", name="uq_mlb_player_split"),
        {"schema": "mlb"},
    )

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("mlb.players.id"), nullable=False, index=True)
    season_id = Column(Integer, ForeignKey("mlb.seasons.id"), nullable=True, index=True)
    split_type = Column(String(50), nullable=False)  # vs_lhp, vs_rhp, home, away, day, night, grass, turf, city_*

    # Context metadata
    split_label = Column(Text, nullable=True)  # human label e.g. "vs LHP", "Home", "City: Cleveland"
    city = Column(Text, nullable=True)  # normalized city slug when split_type starts with city_

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
    base_on_balls = Column(Integer, default=0)
    strikeouts = Column(Integer, default=0)
    hit_by_pitch = Column(Integer, default=0)
    sacrifice_flies = Column(Integer, default=0)

    # Rate stats
    avg = Column(Float, nullable=True)
    obp = Column(Float, nullable=True)
    slg = Column(Float, nullable=True)
    ops = Column(Float, nullable=True)
    woba = Column(Float, nullable=True)
    babip = Column(Float, nullable=True)
    iso = Column(Float, nullable=True)

    # Derived
    total_bases = Column(Integer, default=0)

    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    player = relationship("MLBPlayer", backref="player_splits")
    season = relationship("MLBSeason")
