from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class Player(Base):
    __tablename__ = "players"
    __table_args__ = {"schema": "nfl"}

    id = Column(Integer, primary_key=True)
    sleeper_id = Column(String(20), unique=True, nullable=True)
    nflverse_id = Column(String(20), nullable=True)
    espn_id = Column(Integer, unique=True, nullable=True)

    name = Column(String(100), nullable=False, index=True)
    position = Column(String(4), nullable=False, index=True)  # QB, RB, WR, TE, K, DST
    team_id = Column(Integer, ForeignKey("nfl.teams.id"), nullable=True)
    status = Column(String(50), default="Active")  # Active, IR, Suspended, Retired, PUP
    jersey_number = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)  # in inches
    weight = Column(Integer, nullable=True)  # in lbs
    birth_date = Column(Date, nullable=True)
    college = Column(String(100), nullable=True)
    years_exp = Column(Integer, nullable=True)
    headshot_url = Column(Text, nullable=True)

    # Draft info (from nflverse)
    draft_year = Column(Integer, nullable=True)
    draft_round = Column(Integer, nullable=True)
    draft_pick = Column(Integer, nullable=True)
    draft_team = Column(String(4), nullable=True)  # team abbreviation
    profile_writeup = Column(Text, nullable=True)  # cached DeepSeek-generated narrative

    team = relationship("Team", backref="players")
