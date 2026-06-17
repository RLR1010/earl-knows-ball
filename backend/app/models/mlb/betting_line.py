from sqlalchemy import Column, Integer, Float, String, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone


class MLBBettingLine(Base):
    __tablename__ = "betting_lines"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("mlb.games.id"), nullable=False, index=True)
    source = Column(String(50), default="mlb_odds_dataset")
    sportsbook = Column(String(50), nullable=True)  # DraftKings, FanDuel, etc.

    # Opening lines
    opening_spread = Column(Float, nullable=True)
    opening_spread_home_odds = Column(Integer, nullable=True)
    opening_spread_away_odds = Column(Integer, nullable=True)
    opening_total = Column(Float, nullable=True)
    opening_total_over_odds = Column(Integer, nullable=True)
    opening_total_under_odds = Column(Integer, nullable=True)
    opening_home_moneyline = Column(Integer, nullable=True)
    opening_away_moneyline = Column(Integer, nullable=True)

    # Closing/current lines
    spread = Column(Float, nullable=True)  # negative = favorite
    spread_home_odds = Column(Integer, nullable=True)
    spread_away_odds = Column(Integer, nullable=True)
    over_under = Column(Float, nullable=True)
    over_odds = Column(Integer, nullable=True)
    under_odds = Column(Integer, nullable=True)
    home_moneyline = Column(Integer, nullable=True)
    away_moneyline = Column(Integer, nullable=True)

    # Derived
    home_implied_probability = Column(Float, nullable=True)
    away_implied_probability = Column(Float, nullable=True)
    is_opening = Column(String(5), default="false")  # "true" if this is an opening line snapshot

    recorded_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    game = relationship("MLBGames", backref="betting_lines")
