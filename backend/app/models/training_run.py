"""TrainingRun model — one table per schema (nfl, nba, mlb).

Each training run records the model type, a unique ID (the same ID used for
the .pkl filename), a timestamp, the full backtest results JSON, and a flag
for whether this row is the "current" (production) model shown in the admin UI.
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Boolean, Text, Integer
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.database import Base


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model_type = Column(String(64), nullable=False)          # e.g. "ou", "ats", "ml"
    training_id = Column(UUID(as_uuid=True), unique=True, nullable=False,
                         default=uuid.uuid4)
    trained_at = Column(DateTime(timezone=True),
                        default=datetime.utcnow, nullable=False)
    results_json = Column(JSONB, nullable=True)
    is_current = Column(Boolean, default=False, nullable=False)
    pkl_filename = Column(String(256), nullable=True)
    algorithm = Column(String(64), nullable=True)
    description = Column(Text, nullable=True)
    test_year = Column(Integer, nullable=True)
    train_years = Column(Text, nullable=True)  # comma-separated years

    # __table_args__ is set by the schema-specific subclasses or dynamically

    def to_dict(self):
        return {
            "id": str(self.id),
            "model_type": self.model_type,
            "training_id": str(self.training_id),
            "trained_at": self.trained_at.isoformat() if self.trained_at else None,
            "is_current": self.is_current,
            "pkl_filename": self.pkl_filename,
            "algorithm": self.algorithm,
            "description": self.description,
            "test_year": self.test_year,
            "train_years": self.train_years,
        }
