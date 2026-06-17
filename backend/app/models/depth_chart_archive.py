"""Historical depth chart snapshots from Ourlads archives."""
from sqlalchemy import Column, Integer, String, DateTime, Date, ForeignKey
from app.database import Base
from datetime import datetime, timezone


class DepthChartArchive(Base):
    """Historical depth chart snapshot from Ourlads archives (2007-2025)."""
    __tablename__ = "depth_charts_archive"
    __table_args__ = {"schema": "nfl"}

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, nullable=False, index=True)  # Ourlads archive ID
    snapshot_date = Column(Date, nullable=False, index=True)  # Date of snapshot
    team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=False, index=True)
    position = Column(String(10), nullable=False)
    slot = Column(Integer, default=1)
    player_name = Column(String(100), nullable=False)
    jersey_number = Column(Integer, nullable=True)
    acquisition_info = Column(String(50), nullable=True)
    scraped_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
