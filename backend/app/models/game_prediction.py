"""
Stores game predictions from the handicapping engine for tracking accuracy.
"""
import json
from sqlalchemy import (
    Column, Integer, Float, String, Text, DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone


class GamePrediction(Base):
    __tablename__ = "game_predictions"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("nfl.games.id"), nullable=False, index=True)

    # Model outputs
    predicted_home_score = Column(Float, nullable=True)
    predicted_away_score = Column(Float, nullable=True)
    predicted_total = Column(Float, nullable=True)
    predicted_margin = Column(Float, nullable=True)

    # Model confidence
    margin_conf = Column(Float, nullable=True)
    ou_conf = Column(Float, nullable=True)
    ml_conf = Column(Float, nullable=True)

    # O/U pick
    ou_pick = Column(String(10), nullable=True)  # "Over", "Under", "Push"

    # Pace adjustment applied (deprecated — kept for historical data)
    pace_adjustment_pts = Column(Float, nullable=True)

    # Enriched metadata for display / article use
    home_stats_json = Column(Text, nullable=True, comment="JSON — TeamStats.to_dict() for home team")
    away_stats_json = Column(Text, nullable=True, comment="JSON — TeamStats.to_dict() for away team")
    situational_json = Column(Text, nullable=True, comment="JSON — SituationalAnalyzer.to_dict()")
    splits_json = Column(Text, nullable=True, comment="JSON — SplitAnalyzer.to_dict()")

    # Actual results (filled in after game)
    actual_home_score = Column(Integer, nullable=True)
    actual_away_score = Column(Integer, nullable=True)
    actual_total = Column(Integer, nullable=True)
    actual_margin = Column(Integer, nullable=True)

    # Results
    ats_result = Column(String(10), nullable=True)   # "Win", "Loss", "Push"
    ou_result = Column(String(10), nullable=True)    # "Win", "Loss", "Push"
    ml_result = Column(String(10), nullable=True)    # "Win", "Loss"
    spread_pick = Column(String(50), nullable=True)  # e.g. "BAL -6.0"
    ml_pick = Column(String(50), nullable=True)      # e.g. "BAL"

    # Expected value
    ats_ev = Column(Float, nullable=True)
    ou_ev = Column(Float, nullable=True)
    ml_ev = Column(Float, nullable=True)

    # PnL tracking
    ats_odds = Column(Integer, nullable=True)
    ou_odds = Column(Integer, nullable=True)
    ml_odds = Column(Integer, nullable=True)
    ats_profit = Column(Float, nullable=True)
    ou_profit = Column(Float, nullable=True)
    ml_profit = Column(Float, nullable=True)

    # Metadata
    source = Column(String(50), nullable=True)  # "backtest" or "api"
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    game = relationship("Game", backref="game_predictions")

    __table_args__ = (
        UniqueConstraint("game_id", "source", name="uq_prediction_game_source"),
        {"schema": "nfl"},
    )

    def __repr__(self):
        return f"<GamePrediction game={self.game_id} pred={self.predicted_home_score}-{self.predicted_away_score}>"
