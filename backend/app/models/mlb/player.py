from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey, Float
from sqlalchemy.orm import relationship
from app.database import Base


class MLBPlayer(Base):
    __tablename__ = "players"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True)
    mlb_id = Column(Integer, unique=True, nullable=True, index=True)  # MLB Stats API person ID
    name = Column(String(100), nullable=False, index=True)
    position = Column(String(4), nullable=False, index=True)  # P, C, 1B, 2B, 3B, SS, LF, CF, RF, DH, OF, IF, UT
    team_id = Column(Integer, ForeignKey("mlb.teams.id"), nullable=True)
    jersey_number = Column(Integer, nullable=True)
    bats = Column(String(4), nullable=True)  # L, R, S (Switch)
    throws = Column(String(4), nullable=True)  # L, R
    height = Column(Integer, nullable=True)  # in inches
    weight = Column(Integer, nullable=True)  # in lbs
    birth_date = Column(Date, nullable=True)
    birth_city = Column(String(100), nullable=True)
    birth_state_province = Column(String(100), nullable=True)
    birth_country = Column(String(100), nullable=True)
    college = Column(String(200), nullable=True)
    years_exp = Column(Integer, nullable=True)
    headshot_url = Column(Text, nullable=True)
    status = Column(String(50), nullable=True)  # Active, Injured, Retired, etc.
    primary_number = Column(Integer, nullable=True)
    active = Column(Integer, default=1)  # Whether currently active in MLB

    team = relationship("MLBTeam", backref="players")
    batting_stats = relationship("MLBBattingStats", back_populates="player", cascade="all, delete-orphan")
    pitching_stats = relationship("MLBPitchingStats", back_populates="player", cascade="all, delete-orphan")
