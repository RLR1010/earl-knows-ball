from sqlalchemy import Column, Integer
from app.database import Base


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = {"schema": "nfl"}

    id = Column(Integer, primary_key=True)
    year = Column(Integer, unique=True, nullable=False, index=True)
