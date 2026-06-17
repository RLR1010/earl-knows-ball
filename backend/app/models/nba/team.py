from sqlalchemy import Column, Integer, String, Text
from app.database import Base


class NBATeam(Base):
    __tablename__ = "teams"
    __table_args__ = {"schema": "nba"}

    id = Column(Integer, primary_key=True)
    abbreviation = Column(String(4), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    conference = Column(String(4), nullable=False)  # East / West
    division = Column(String(20), nullable=False)   # Atlantic, Central, Southeast, Northwest, Pacific, Southwest
    logo_url = Column(Text, nullable=True)
