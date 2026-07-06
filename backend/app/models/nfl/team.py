from sqlalchemy import Column, Integer, String, Text
from app.database import Base


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = {"schema": "nfl"}

    id = Column(Integer, primary_key=True)
    abbreviation = Column(String(4), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    conference = Column(String(4), nullable=False)  # AFC / NFC
    division = Column(String(20), nullable=False)   # North, South, East, West
    logo_url = Column(Text, nullable=True)
    byeweek = Column(Integer, nullable=True)
