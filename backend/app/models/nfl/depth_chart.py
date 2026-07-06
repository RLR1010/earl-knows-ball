from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text
from app.database import Base
from datetime import datetime, timezone


class DepthChart(Base):
    """Current depth chart for a team from Ourlads."""
    __tablename__ = "depth_charts"
    __table_args__ = {"schema": "nfl"}

    id = Column(Integer, primary_key=True)
    team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=False, index=True)
    position = Column(String(10), nullable=False)  # QB, RB, WR, LT, etc.
    slot = Column(Integer, default=1)  # 1st string, 2nd string, etc.
    player_id = Column(Integer, ForeignKey("nfl.players.id"), nullable=True)
    player_name = Column(String(100), nullable=False)
    jersey_number = Column(Integer, nullable=True)
    acquisition_info = Column(String(50), nullable=True)  # e.g., "23/3", "FA25", "SF25"
    status = Column(String(20), default="active")  # active, injured, rookie, fa_acq, udfa
    scraped_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Transaction(Base):
    """NFL transactions: free agent signings, trades, cuts, etc."""
    __tablename__ = "transactions"
    __table_args__ = {"schema": "nfl"}

    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("nfl.players.id"), nullable=True)
    player_name = Column(String(100), nullable=False)
    position = Column(String(10), nullable=True)
    transaction_type = Column(String(30), nullable=False)  # signed, traded, released, franchised, etc.
    from_team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=True)
    to_team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=True)
    from_team_name = Column(String(100), nullable=True)
    to_team_name = Column(String(100), nullable=True)
    details = Column(Text, nullable=True)  # contract terms, trade details
    source = Column(String(50), default="ourlads")
    source_url = Column(String(500), nullable=True)
    transaction_date = Column(DateTime(timezone=True), nullable=False)
    scraped_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
