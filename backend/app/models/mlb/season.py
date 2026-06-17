from sqlalchemy import Column, Integer
from app.database import Base


class MLBSeason(Base):
    __tablename__ = "seasons"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True)
    year = Column(Integer, unique=True, nullable=False, index=True)
