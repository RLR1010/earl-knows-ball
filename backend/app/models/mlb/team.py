from sqlalchemy import Column, Integer, String, Text
from app.database import Base


class MLBTeam(Base):
    __tablename__ = "teams"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True)
    abbreviation = Column(String(4), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    league = Column(String(4), nullable=False)  # AL / NL
    division = Column(String(10), nullable=False)  # East / Central / West
    logo_url = Column(Text, nullable=True)
