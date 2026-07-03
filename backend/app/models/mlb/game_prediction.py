"""
Stores MLB game predictions from the handicapping engine for tracking accuracy.
"""
from sqlalchemy import (
    Column, Integer, Float, String, Text, DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base
from datetime import datetime, timezone


class MLBGamePrediction(Base):
    __tablename__ = "game_predictions"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("mlb.games.id"), nullable=False, index=True)

    # Model outputs
    predicted_home_runs = Column(Float, nullable=True)
    predicted_away_runs = Column(Float, nullable=True)
    predicted_total = Column(Float, nullable=True)
    predicted_margin = Column(Float, nullable=True)

    # Model confidence (raw — used by Predictions page)
    rl_conf = Column(Float, nullable=True, comment="Raw RL confidence heuristic")
    ml_conf = Column(Float, nullable=True, comment="Raw ML confidence heuristic")
    ou_conf = Column(Float, nullable=True, comment="Raw OU confidence heuristic")
    # Calibrated confidence (used for EV calculation)
    rl_conf_cal = Column(Float, nullable=True, comment="Calibrated RL confidence")
    ml_conf_cal = Column(Float, nullable=True, comment="Calibrated ML confidence")
    ou_conf_cal = Column(Float, nullable=True, comment="Calibrated OU confidence")

    # Picks
    ou_pick = Column(String(20), nullable=True)         # "Over", "Under", "Push / No edge"
    run_line_pick = Column(String(100), nullable=True)  # e.g. "CHC -1.5"
    ml_pick = Column(String(10), nullable=True)  # "home" or "away"
    ml_odds = Column(Integer, nullable=True)  # moneyline odds for the pick

    # Actual results (filled in after game)
    actual_home_runs = Column(Integer, nullable=True)
    actual_away_runs = Column(Integer, nullable=True)
    actual_total = Column(Integer, nullable=True)
    actual_margin = Column(Integer, nullable=True)

    # Results
    run_line_result = Column(String(10), nullable=True)   # "Win", "Loss", "Push"
    ou_result = Column(String(10), nullable=True)          # "Win", "Loss", "Push"
    ml_result = Column(String(10), nullable=True)          # "Win", "Loss"

    # PnL tracking
    ats_odds = Column(Integer, nullable=True)
    ou_odds = Column(Integer, nullable=True)
    ml_odds = Column(Integer, nullable=True)
    ats_profit = Column(Float, nullable=True)
    ou_profit = Column(Float, nullable=True)
    ml_profit = Column(Float, nullable=True)

    # Calibrated EV scores
    ats_ev = Column(Float, nullable=True)
    ou_ev = Column(Float, nullable=True)
    ml_ev = Column(Float, nullable=True)

    # Metadata
    source = Column(String(50), nullable=True)  # "backtest" or "api"

    # Enriched metadata for display / article use
    home_stats_json = Column(Text, nullable=True, comment="JSON — MLBTeamStats.to_dict() for home team")
    away_stats_json = Column(Text, nullable=True, comment="JSON — MLBTeamStats.to_dict() for away team")
    situational_json = Column(Text, nullable=True, comment="JSON — MLBSituationalAnalyzer.to_dict()")
    splits_json = Column(Text, nullable=True, comment="JSON — MLBSplitAnalyzer.to_dict()")
    features_json = Column(Text, nullable=True, comment="JSON — pick_card feature values at prediction time")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    game = relationship("MLBGames", backref="game_predictions")

    __table_args__ = (
        UniqueConstraint("game_id", "source", name="uq_mlb_prediction_game_source"),
        {"schema": "mlb"},
    )

    def __repr__(self):
        return f"<MLBGamePrediction game={self.game_id} pred={self.predicted_home_runs}-{self.predicted_away_runs}>"
