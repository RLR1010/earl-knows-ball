"""NBA InactivePlayer model — players NOT dressed for a game (injured / inactive)."""
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, UniqueConstraint
from datetime import timezone, datetime
from app.database import Base


class NBAInactivePlayer(Base):
    __tablename__ = "inactive_players"
    __table_args__ = (
        UniqueConstraint("game_id", "player_id", name="uq_inactive_player_game"),
        {"schema": "nba",
         "info": {"comment": "NBA players NOT dressed/active for a game (injured, inactive, or left off the active list)"}},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_id = Column(Integer, ForeignKey("nba.games.id", ondelete="CASCADE"), nullable=False)
    team_id = Column(Integer, ForeignKey("nba.teams.id"), nullable=False)
    player_id = Column(Integer, ForeignKey("nba.players.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(12), nullable=False, default="INACTIVE")  # e.g. INACTIVE, OUT, QUESTIONABLE, DTD
    reason = Column(String(140))  # injury / reason if known
    src = Column(String(16), nullable=False, default="postgame")  # 'pregame' | 'postgame'
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
