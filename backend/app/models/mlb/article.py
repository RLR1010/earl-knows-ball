"""
MLB Article model — stores full-text articles scraped from SB Nation MLB blogs.
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from app.database import Base
from datetime import datetime, timezone


class MLBArticle(Base):
    __tablename__ = "articles"
    __table_args__ = {"schema": "mlb"}

    id = Column(Integer, primary_key=True)
    title = Column(String(300), nullable=False)
    slug = Column(String(300), unique=True, nullable=False, index=True)
    body = Column(Text, nullable=False)
    excerpt = Column(String(500), nullable=True)
    category = Column(String(50), nullable=False, index=True)
    tier = Column(String(20), default="free")
    published = Column(Boolean, default=False)
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))
    author = Column(String(100), default="Earl Knows Ball")
    source_url = Column(String(500), nullable=True, index=True)
    source_name = Column(String(100), nullable=True)
    source_type = Column(String(20), default="original")
    metadata_json = Column(Text, nullable=True)
    embedded_at = Column(DateTime(timezone=True), nullable=True)
