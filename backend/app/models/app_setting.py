"""Global, admin-editable runtime settings (public.app_settings key/value store).

Used for feature toggles and defaults that admins flip at runtime without a
deploy — e.g. the free chat enable flag and the free tier's monthly token grant.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text

from app.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = ({"schema": "public"},)
