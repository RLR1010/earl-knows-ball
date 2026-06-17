from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey, Float
from sqlalchemy.orm import relationship
from app.database import Base


class NBAPlayer(Base):
    __tablename__ = "players"
    __table_args__ = {"schema": "nba"}

    id = Column(Integer, primary_key=True)
    nba_id = Column(Integer, unique=True, nullable=True, index=True)  # stats.nba.com person ID
    name = Column(String(100), nullable=False, index=True)
    position = Column(String(4), nullable=False, index=True)  # PG, SG, SF, PF, C, G, F
    team_id = Column(Integer, ForeignKey("nba.teams.id"), nullable=True)
    jersey_number = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)      # in inches
    weight = Column(Integer, nullable=True)      # in lbs
    birth_date = Column(Date, nullable=True)
    birth_city = Column(String(100), nullable=True)
    birth_state = Column(String(100), nullable=True)
    birth_country = Column(String(100), nullable=True)
    college = Column(String(100), nullable=True)
    years_exp = Column(Integer, nullable=True)
    headshot_url = Column(Text, nullable=True)
    status = Column(String(50), nullable=True)   # Active, IR, etc.
    active = Column(Integer, default=1)

    team = relationship("NBATeam", backref="players")
    season_stats = relationship("NBAPlayerSeasonStats", back_populates="player", cascade="all, delete-orphan")
