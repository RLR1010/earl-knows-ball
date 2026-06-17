"""
Canonical game lines — one row per game with pre-populated opening and closing lines.
All code should read from this table, not from betting_lines directly.
"""
from sqlalchemy import Column, Integer, Float, String, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class GameLines(Base):
    __tablename__ = "game_lines"
    __table_args__ = {"schema": "nfl"}

    game_id = Column(Integer, ForeignKey("nfl.games.id"), primary_key=True)
    spread = Column(Float, nullable=True)
    over_under = Column(Float, nullable=True)
    opening_spread = Column(Float, nullable=True)
    opening_ou = Column(Float, nullable=True)
    home_moneyline = Column(Integer, nullable=True)
    away_moneyline = Column(Integer, nullable=True)
    source_opening = Column(String(50), nullable=True)
    source_closing = Column(String(50), nullable=True)

    game = relationship("Game", backref="game_lines")

    def __repr__(self):
        return f"<GameLines game={self.game_id} line={self.spread}>"
