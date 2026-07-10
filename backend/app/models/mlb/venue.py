"""Venue model for MLB stadiums.

Stores static venue characteristics used in write-ups
(stadium name, capacity, dimensions, surface, altitude, etc.).

The games table already holds per-game conditions (weather, roof status)
via its own columns; this table captures the fixed venue profile.
"""
from sqlalchemy import Column, Integer, String, Float, Text

from app.database import Base


class MLBVenue(Base):
    """MLB stadium/venue profile."""

    __tablename__ = "venues"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True)
    mlb_venue_id = Column(Integer, unique=True, nullable=True, index=True)
    name = Column(String(150), nullable=False)
    city = Column(String(100), nullable=False)
    state = Column(String(50), nullable=True)
    capacity = Column(Integer, nullable=True)
    surface = Column(String(50), nullable=True)  # Grass, AstroTurf, etc.
    roof_type = Column(String(20), nullable=True)  # Open, Retractable, Closed

    # Dimensions in feet
    left_field = Column(Integer, nullable=True)
    left_center = Column(Integer, nullable=True)
    center_field = Column(Integer, nullable=True)
    right_center = Column(Integer, nullable=True)
    right_field = Column(Integer, nullable=True)

    # Wall heights in feet
    wall_height_left = Column(Float, nullable=True)
    wall_height_center = Column(Float, nullable=True)
    wall_height_right = Column(Float, nullable=True)

    # Altitude in feet (relevant for Coors, Chase, etc.)
    altitude = Column(Integer, nullable=True)

    # Ballpark factors (100 = neutral, >100 favors hitters)
    park_factor_overall = Column(Float, nullable=True)
    park_factor_home_runs = Column(Float, nullable=True)

    # Optional history/blurb for write-ups
    description = Column(Text, nullable=True)
    year_opened = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<MLBVenue id={self.id} name={self.name}>"

