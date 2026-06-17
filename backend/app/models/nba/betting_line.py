"""NBA betting lines model."""
from sqlalchemy import Column, Integer, Float, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone


class NBABettingLine(Base):
    __tablename__ = "betting_lines"
    __table_args__ = {"schema": "nba"}

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("nba.games.id"), nullable=False, index=True)
    source = Column(String(50), default="the_odds_api")
    sportsbook = Column(String(50), nullable=True)

    opening_spread = Column(Float, nullable=True)
    opening_spread_home_odds = Column(Integer, nullable=True)
    opening_spread_away_odds = Column(Integer, nullable=True)
    opening_total = Column(Float, nullable=True)
    opening_total_over_odds = Column(Integer, nullable=True)
    opening_total_under_odds = Column(Integer, nullable=True)
    opening_home_moneyline = Column(Integer, nullable=True)
    opening_away_moneyline = Column(Integer, nullable=True)

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
    is_opening = Column(String(5), default="false")

    recorded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    game = relationship("NBAGame", backref="betting_lines")
