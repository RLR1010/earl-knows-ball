"""NBA ActivePlayer model — active roster per game (mirrors MLB lineups)."""
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, UniqueConstraint
from datetime import timezone, datetime
from app.database import Base


class NBAActivePlayer(Base):
    __tablename__ = "active_players"
    __table_args__ = (
        UniqueConstraint("game_id", "player_id", name="uq_active_player_game"),
        {"schema": "nba",
         "info": {"comment": "NBA active roster per game (pregame-filled; postgame backfill for training)"}},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_id = Column(Integer, ForeignKey("nba.games.id", ondelete="CASCADE"), nullable=False)
    team_id = Column(Integer, ForeignKey("nba.teams.id"), nullable=False)
    player_id = Column(Integer, ForeignKey("nba.players.id", ondelete="CASCADE"), nullable=False)
    is_starter = Column(Boolean, nullable=False, default=False)  # starting-five flag
    # accuracy: 'PLAYED' | 'DNP_CD' (dressed/active, healthy scratch) | 'INACTIVE' (not dressed)
    status = Column(String(12), nullable=False, default="PLAYED")
    reason = Column(String(140))  # e.g. "COACH'S DECISION" or an injury
    src = Column(String(16), nullable=False, default="pregame")  # 'pregame' | 'postgame'
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
