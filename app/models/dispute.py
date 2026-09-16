from sqlalchemy import Column, String, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.core.database import Base
from app.models.base import TimestampMixin, generate_uuid

class Dispute(Base, TimestampMixin):
    __tablename__ = "disputes"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    tournament_id = Column(String(36), ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False)
    match_id = Column(String(36), ForeignKey("matches.id", ondelete="CASCADE"), nullable=False)
    submitted_by = Column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reason = Column(String(128), nullable=False)
    description = Column(Text, nullable=False)
    evidence_url = Column(String(512), nullable=True)
    status = Column(String(32), default="OPEN", nullable=False)  # OPEN, UNDER_REVIEW, RESOLVED, ESCALATED, CLOSED
    resolution_notes = Column(Text, nullable=True)
    resolved_by = Column(String(64), ForeignKey("users.id"), nullable=True)

    # Relationships
    tournament = relationship("Tournament")
    match = relationship("Match")
    submitter = relationship("User", foreign_keys=[submitted_by])
    resolver = relationship("User", foreign_keys=[resolved_by])
