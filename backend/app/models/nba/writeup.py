"""Game write-up model for NBA.

Stores both public (no picks) and premium (with picks) versions
of AI-generated game previews, along with research data and quality checks.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Float,
    Boolean, ForeignKey, UniqueConstraint, JSON
)
from sqlalchemy.orm import relationship

from app.database import Base


class NBAGameWriteup(Base):
    """AI-generated game write-up with public and premium versions."""

    __tablename__ = "game_writeups"
    __table_args__ = (
        UniqueConstraint("game_id", name="uq_nba_writeup_game"),
        {"schema": "nba"},
    )

    id = Column(Integer, primary_key=True)
    game_id = Column(
        Integer,
        ForeignKey("nba.games.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Content
    title = Column(String(300), nullable=False)
    public_content = Column(Text, nullable=False, default="")
    premium_content = Column(Text, nullable=False, default="")

    # Research brief — structured data the AI used to generate
    research_brief = Column(JSON, nullable=True)

    # Quality check results
    quality_checks = Column(JSON, nullable=True)

    # Status lifecycle: draft → review → published → archived
    STATUS_CHOICES = ("draft", "review", "published", "archived")
    status = Column(String(20), nullable=False, default="draft", index=True)

    # Version tracking
    version = Column(Integer, nullable=False, default=1)

    # Historical mode — for backfilled write-ups, the content must not
    # reference the actual game outcome.
    is_historical = Column(Boolean, nullable=False, default=False)

    # The date the game was (or will be) played — used to filter research
    # queries so the AI only sees data available before the game.
    historical_game_date = Column(DateTime(timezone=True), nullable=True)

    # Generation metadata
    generated_by = Column(String(100), nullable=True)  # model name
    total_tokens = Column(Integer, nullable=True)

    # Timestamps
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    game = relationship("NBAGame", backref="writeups")

    def __repr__(self) -> str:
        return (
            f"<NBAGameWriteup id={self.id} game_id={self.game_id} "
            f"status={self.status} v{self.version}>"
        )
