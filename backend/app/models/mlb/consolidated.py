from sqlalchemy import Column, Integer, Float, String, DateTime, Boolean, Text
from app.database import Base


class MLBBettingLineConsolidated(Base):
    __tablename__ = "betting_lines_consolidated"
    __table_args__ = {"schema": "mlb"}

    game_id = Column(Integer, primary_key=True)
    game_time = Column(DateTime, nullable=True)
    home_team = Column(String(10), nullable=True)
    away_team = Column(String(10), nullable=True)
    year = Column(Integer, nullable=True)
    home_score = Column(Integer, nullable=True)
    away_score = Column(Integer, nullable=True)
    venue = Column(Text, nullable=True)
    status = Column(String(20), nullable=True)

    closing_spread = Column(Float, nullable=True)
    closing_spread_sportsbook = Column(String(100), nullable=True)
    closing_ou = Column(Float, nullable=True)
    closing_ou_sportsbook = Column(String(100), nullable=True)
    closing_home_ml = Column(Integer, nullable=True)
    closing_home_ml_sportsbook = Column(String(100), nullable=True)
    closing_away_ml = Column(Integer, nullable=True)
    closing_away_ml_sportsbook = Column(String(100), nullable=True)
    closing_over_odds = Column(Integer, nullable=True)
    closing_over_odds_sportsbook = Column(String(100), nullable=True)
    closing_under_odds = Column(Integer, nullable=True)
    closing_under_odds_sportsbook = Column(String(100), nullable=True)
    closing_spread_home_odds = Column(Integer, nullable=True)
    closing_spread_home_odds_sportsbook = Column(String(100), nullable=True)
    closing_spread_away_odds = Column(Integer, nullable=True)
    closing_spread_away_odds_sportsbook = Column(String(100), nullable=True)

    opening_spread = Column(Float, nullable=True)
    opening_spread_sportsbook = Column(String(100), nullable=True)
    opening_ou = Column(Float, nullable=True)
    opening_ou_sportsbook = Column(String(100), nullable=True)
    opening_home_ml = Column(Integer, nullable=True)
    opening_home_ml_sportsbook = Column(String(100), nullable=True)
    opening_away_ml = Column(Integer, nullable=True)
    opening_away_ml_sportsbook = Column(String(100), nullable=True)
    opening_over_odds = Column(Integer, nullable=True)
    opening_over_odds_sportsbook = Column(String(100), nullable=True)
    opening_under_odds = Column(Integer, nullable=True)
    opening_under_odds_sportsbook = Column(String(100), nullable=True)
    opening_spread_home_odds = Column(Integer, nullable=True)
    opening_spread_home_odds_sportsbook = Column(String(100), nullable=True)
    opening_spread_away_odds = Column(Integer, nullable=True)
    opening_spread_away_odds_sportsbook = Column(String(100), nullable=True)

    closing_home_implied_probability = Column(Float, nullable=True)
    closing_away_implied_probability = Column(Float, nullable=True)
    opening_home_implied_probability = Column(Float, nullable=True)
    opening_away_implied_probability = Column(Float, nullable=True)

    has_verified_ou = Column(Boolean, nullable=True, default=True)
