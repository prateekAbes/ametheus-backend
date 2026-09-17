from sqlalchemy import Column, String, Boolean, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid

class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(String(32), default="SYSTEM", nullable=False)  # SYSTEM, TOURNAMENT, MATCH, PAYMENT, DISPUTE
    title = Column(String(128), nullable=False)
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False)
    link = Column(String(256), nullable=True)
    metadata_json = Column(Text, default="{}", nullable=False)

    # Relationships
    user = relationship("User", foreign_keys=[user_id])
