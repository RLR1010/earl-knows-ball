"""
Stores NBA game predictions from the handicapping engine for tracking accuracy.

Mirrors MLBGamePrediction structure but adapted for NBA scoring (points not runs).
"""
from sqlalchemy import (
    Column, Integer, Float, String, Text, DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone


class NBAGamePrediction(Base):
    __tablename__ = "game_predictions"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("nba.games.id"), nullable=False, index=True)

    # Model outputs
    predicted_home_score = Column(Float, nullable=True)
    predicted_away_score = Column(Float, nullable=True)
    predicted_total = Column(Float, nullable=True)
    predicted_margin = Column(Float, nullable=True)

    # Model confidence (raw — used by Predictions page)
    margin_conf = Column(Float, nullable=True)
    ml_conf = Column(Float, nullable=True, comment="Raw ML confidence heuristic")
    ou_conf = Column(Float, nullable=True, comment="Raw OU confidence heuristic")
    # Calibrated confidence (used for EV calculation)
    ats_conf_cal = Column(Float, nullable=True, comment="Calibrated ATS confidence")
    ml_conf_cal = Column(Float, nullable=True, comment="Calibrated ML confidence")
    ou_conf_cal = Column(Float, nullable=True, comment="Calibrated OU confidence")

    # Picks
    ou_pick = Column(String(20), nullable=True)          # "Over", "Under"
    spread_pick = Column(String(100), nullable=True)     # e.g. "BOS -5.5"
    ml_pick = Column(String(10), nullable=True)          # "home" or "away"

    # Actual results (filled in after game)
    actual_home_score = Column(Integer, nullable=True)
    actual_away_score = Column(Integer, nullable=True)
    actual_total = Column(Integer, nullable=True)
    actual_margin = Column(Integer, nullable=True)

    # Results
    ats_result = Column(String(10), nullable=True)    # "Win", "Loss", "Push"
    ou_result = Column(String(10), nullable=True)        # "Win", "Loss", "Push"
    ml_result = Column(String(10), nullable=True)        # "Win", "Loss"

    # PnL tracking
    ats_odds = Column(Integer, nullable=True)
    ou_odds = Column(Integer, nullable=True)
    ml_odds = Column(Integer, nullable=True)
    ats_profit = Column(Float, nullable=True)
    ou_profit = Column(Float, nullable=True)
    ml_profit = Column(Float, nullable=True)
    # Expected value (calibrated conf × odds)
    ats_ev = Column(Float, nullable=True, comment="Expected value for $100 ATS bet")
    ou_ev = Column(Float, nullable=True, comment="Expected value for $100 OU bet")
    ml_ev = Column(Float, nullable=True, comment="Expected value for $100 ML bet")

    # Metadata
    source = Column(String(50), nullable=True)  # "backtest" or "api"

    # Enriched metadata for display
    home_stats_json = Column(Text, nullable=True, comment="JSON — NBATeamStats.to_dict() for home team")
    away_stats_json = Column(Text, nullable=True, comment="JSON — NBATeamStats.to_dict() for away team")
    situational_json = Column(Text, nullable=True, comment="JSON — NBASituationalAnalyzer.to_dict()")
    splits_json = Column(Text, nullable=True, comment="JSON — NBASplitAnalyzer.to_dict()")
    features_json = Column(Text, nullable=True, comment="JSON — pick_card feature values at prediction time")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    game = relationship("NBAGame", backref="game_predictions")

    __table_args__ = (
        UniqueConstraint("game_id", "source", name="uq_nba_prediction_game_source"),
        {"schema": "nba"},
    )

    def __repr__(self):
        return f"<NBAGamePrediction game={self.game_id} pred={self.predicted_home_score}-{self.predicted_away_score}>"
