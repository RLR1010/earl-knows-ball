from sqlalchemy import Column, Integer, Float, String, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone


class BettingLine(Base):
    __tablename__ = "betting_lines"
    __table_args__ = {"schema": "nfl"}

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("nfl.games.id"), nullable=False, index=True)
    source = Column(String(50), default="the_odds_api")
    sportsbook = Column(String(100), nullable=True)
    spread = Column(Float, nullable=True)
    spread_home_odds = Column(Integer, nullable=True)
    spread_away_odds = Column(Integer, nullable=True)
    over_under = Column(Float, nullable=True)
    over_odds = Column(Integer, nullable=True)
    under_odds = Column(Integer, nullable=True)
    home_moneyline = Column(Integer, nullable=True)
    away_moneyline = Column(Integer, nullable=True)
    home_implied_probability = Column(Float, nullable=True)
    away_implied_probability = Column(Float, nullable=True)
    is_opening = Column(Boolean, default=False)
    api_last_update = Column(DateTime(timezone=True), nullable=True)
    recorded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    game = relationship("Game", backref="betting_lines")
