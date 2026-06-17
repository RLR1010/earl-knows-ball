"""MLB Lineup model — batting order per game."""
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import timezone, datetime
from app.database import Base


class MLBLineup(Base):
    __tablename__ = "lineups"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_id = Column(Integer, ForeignKey("mlb.games.id", ondelete="CASCADE"), nullable=False)
    team_side = Column(String(10), nullable=False)  # "home" or "away"
    batting_order = Column(Integer, nullable=False)  # 1-9
    player_id = Column(Integer, ForeignKey("mlb.players.id"))
    player_name = Column(String(255), nullable=False)
    position = Column(String(10))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("game_id", "team_side", "batting_order", name="uq_lineup_spot"),
        {"schema": "mlb"},
    )
